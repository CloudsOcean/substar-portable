import threading
import time
import pytest
from substar_core.editor.application import review_chat as review

ROUTE=dict(base_url='https://example.test',api_key='secret',auth_mode='bearer',model='review-model',thinking_mode='enabled',reasoning_effort='low',max_tokens=1000,temperature=0)


def wait(project, id):
    end=time.monotonic()+3
    while time.monotonic()<end:
        value=review.get_request(project,id)
        if value['state']!='running':return value
        time.sleep(.01)
    pytest.fail('review did not finish')


def test_single_response_no_persistence_or_conversation(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls=[]
    def generate(**kwargs):calls.append(kwargs);return 'Review Cue 1',{}
    value=review.start_request('one',material='Cue 1 snapshot',route=ROUTE,generate=generate)
    result=wait('one',value['id'])
    assert result['answer']=='Review Cue 1'
    assert calls[0]['user_text']=='Cue 1 snapshot'
    assert 'conversation_tail' not in calls[0]
    assert calls[0]['reasoning_effort']=='low'
    assert list(tmp_path.iterdir())==[]
    assert 'secret' not in str(result)
    review.discard_request('one',value['id'])
    with pytest.raises(ValueError):review.get_request('one',value['id'])


def test_discard_cancels_and_late_reply_cannot_restore():
    started=threading.Event();finish=threading.Event();finished=threading.Event();captured=[]
    def generate(**kwargs):
        captured.append(kwargs['cancel_event']);started.set();finish.wait(2);finished.set();return 'late',{}
    value=review.start_request('one',material='snapshot',route=ROUTE,generate=generate)
    assert started.wait(1)
    review.discard_request('one',value['id'])
    assert captured[0].is_set()
    finish.set();assert finished.wait(1)
    with pytest.raises(ValueError):review.get_request('one',value['id'])
    review.discard_request('one',value['id'])


def test_project_isolation_and_safe_errors():
    def generate(**kwargs):raise RuntimeError('secret provider payload')
    value=review.start_request('one',material='snapshot',route=ROUTE,generate=generate)
    result=wait('one',value['id'])
    assert result['state']=='error'
    assert 'secret' not in str(result)
    with pytest.raises(ValueError):review.get_request('two',value['id'])
    review.discard_request('two',value['id'])
    assert review.get_request('one',value['id'])==result
    review.discard_request('one',value['id'])


def test_abandoned_requests_expire(monkeypatch):
    value=review.start_request('expiry',material='snapshot',route=ROUTE,generate=lambda **kw:('ok',{}))
    wait('expiry',value['id'])
    monkeypatch.setattr(review,'TTL_SECONDS',-1)
    with pytest.raises(ValueError):review.get_request('expiry',value['id'])
