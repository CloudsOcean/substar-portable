import concurrent.futures
import multiprocessing
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from substar_core.model_concurrency import model_slot, normalize_limits, current_limit, retry_after_seconds


def hold_process(root,ready):
    with model_slot('https://example.test/v1','m','key',root=root,limit_reader=lambda *_:1):
        ready.set()
        time.sleep(30)


class ModelConcurrencyTests(unittest.TestCase):
    def test_same_model_shared_across_threads_and_other_models_independent(self):
        with tempfile.TemporaryDirectory() as root:
            lock=threading.Lock();active=0;peak=0;entered=threading.Event()
            def request():
                nonlocal active,peak
                with model_slot('https://example.test/v1/','m','key',root=root,limit_reader=lambda *_:2):
                    with lock: active+=1;peak=max(peak,active)
                    entered.set();time.sleep(.04)
                    with lock: active-=1
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                futures=[pool.submit(request) for _ in range(10)]
                self.assertTrue(entered.wait(2))
                with model_slot('https://example.test/v1','other','key',root=root,limit_reader=lambda *_:1): pass
                for f in futures:f.result(timeout=5)
            self.assertEqual(peak,2)

    def test_cancelled_waiter_and_exception_release_slot(self):
        with tempfile.TemporaryDirectory() as root:
            cancel=threading.Event();entered=threading.Event()
            def wait():
                with model_slot('https://example.test','m',root=root,limit_reader=lambda *_:1,cancelled=cancel.is_set):entered.set()
            with model_slot('https://example.test','m',root=root,limit_reader=lambda *_:1):
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future=pool.submit(wait);time.sleep(.1);cancel.set()
                    with self.assertRaises(InterruptedError):future.result(timeout=2)
            self.assertFalse(entered.is_set())
            with self.assertRaises(RuntimeError):
                with model_slot('https://example.test','m',root=root,limit_reader=lambda *_:1):raise RuntimeError()
            with model_slot('https://example.test','m',root=root,limit_reader=lambda *_:1):pass

    def test_cross_process_and_killed_worker_reclaims_slot(self):
        with tempfile.TemporaryDirectory() as root:
            ctx=multiprocessing.get_context('spawn');ready=ctx.Event();worker=ctx.Process(target=hold_process,args=(root,ready));worker.start()
            try:
                self.assertTrue(ready.wait(10))
                cancel=threading.Event();cancel.set()
                with self.assertRaises(InterruptedError):
                    with model_slot('https://example.test/v1','m','key',root=root,limit_reader=lambda *_:1,cancelled=cancel.is_set):pass
                acquired=threading.Event()
                def acquire():
                    with model_slot('https://example.test/v1','m','key',root=root,limit_reader=lambda *_:1):acquired.set()
                thread=threading.Thread(target=acquire);thread.start();self.assertFalse(acquired.wait(.2))
                worker.terminate();worker.join(5);thread.join(5);self.assertTrue(acquired.is_set())
            finally:
                if worker.is_alive():worker.terminate();worker.join(5)

    def test_hot_reload_and_override_validation(self):
        import json
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'settings.json'
            with patch('substar_core.config.SETTINGS_FILE',path):
                self.assertEqual(current_limit('https://example.test/v1','m'),50)
                path.write_text(json.dumps({'model_api_concurrency':7,'model_api_concurrency_overrides':[{'base_url':'https://example.test/v1/','model':'m','limit':3}]}))
                self.assertEqual(current_limit('https://example.test/v1','m'),3)
                self.assertEqual(current_limit('https://example.test/v1','other'),7)
                path.write_text('{"model_api_concurrency":2}')
                self.assertEqual(current_limit('https://example.test/v1','m'),2)
        for default,rows in [(0,[]),(257,[]),(True,[]),(50,[{'base_url':'ftp://bad','model':'m','limit':1}])]:
            with self.assertRaises(ValueError):normalize_limits(default,rows)

    def test_lowering_limit_waits_for_existing_requests(self):
        with tempfile.TemporaryDirectory() as root:
            limit=[2];done=threading.Event()
            def acquire():
                with model_slot('https://example.test','m',root=root,limit_reader=lambda *_:limit[0]):done.set()
            with model_slot('https://example.test','m',root=root,limit_reader=lambda *_:2):
                with model_slot('https://example.test','m',root=root,limit_reader=lambda *_:2):
                    limit[0]=1;thread=threading.Thread(target=acquire);thread.start();self.assertFalse(done.wait(.1))
                self.assertFalse(done.wait(.1))
            thread.join(3);self.assertTrue(done.is_set())

    def test_retry_after_and_exponential_fallback(self):
        class Response:headers={'Retry-After':'7'}
        self.assertEqual(retry_after_seconds(Response(),1),7)
        self.assertEqual(retry_after_seconds(None,4),8)

    def test_gateway_releases_slot_before_429_backoff_and_reacquires(self):
        from contextlib import contextmanager
        import requests
        from substar_core.model_gateway.gateway import call_text_model
        active=[];attempts=[]
        @contextmanager
        def slot(*args):
            active.append(True)
            try:yield
            finally:active.pop()
        class Response:
            headers={'Retry-After':'3'}
            def __init__(self,status):self.status_code=status;self.content=b'{"choices":[{"message":{"content":"ok"}}]}'
            def raise_for_status(self):
                if self.status_code==429:raise requests.HTTPError('rate limited',response=self)
        def post(*args,**kwargs):
            self.assertEqual(len(active),1);attempts.append(1)
            return Response(429 if len(attempts)==1 else 200)
        def sleep(delay):self.assertEqual(delay,3);self.assertEqual(active,[])
        with patch('substar_core.model_gateway.gateway.model_slot',slot),patch('substar_core.model_gateway.gateway.requests.post',post),patch('substar_core.model_gateway.gateway.time.sleep',sleep):
            text,_=call_text_model(base_url='https://example.test/v1',api_key='key',model='m',system_prompt='test',user_text='test',timeout=1,thinking_mode='disabled',reasoning_effort='low')
        self.assertEqual(text,'ok');self.assertEqual(len(attempts),2);self.assertEqual(active,[])

    def test_old_settings_payload_preserves_limits(self):
        import app
        with patch.object(app,'save_settings',return_value={}) as save:
            app.put_settings(app.SettingsPayload(output_dir="."))
            values=save.call_args.args[0]
            self.assertNotIn('model_api_concurrency',values)
            self.assertNotIn('model_api_concurrency_overrides',values)
            app.put_settings(app.SettingsPayload(output_dir=".",model_api_concurrency=12,model_api_concurrency_overrides=[]))
            self.assertEqual(save.call_args.args[0]['model_api_concurrency'],12)
