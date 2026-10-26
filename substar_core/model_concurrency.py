"""Cross-process API slots, shared by endpoint/account/model across all stages.

OS file locks release automatically on worker termination. The short registry
lock serializes admission only; network I/O and backoff never hold it.
"""
from __future__ import annotations
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


_cancel_event = None

def set_cancel_event(event):
    global _cancel_event
    _cancel_event = event

def connection_url(value):
    parts=urlsplit(str(value).strip())
    return urlunsplit((parts.scheme.lower(),parts.netloc.lower(),parts.path.rstrip('/'),'',''))


def normalize_limits(default, overrides):
    if isinstance(default,bool): raise ValueError('默认 API 并发必须是 1–256 的整数')
    try: limit=int(default)
    except (ValueError,TypeError): raise ValueError('默认 API 并发必须是 1–256 的整数')
    if limit!=float(default) or not 1<=limit<=256: raise ValueError('默认 API 并发必须是 1–256 的整数')
    if not isinstance(overrides,list) or len(overrides)>100: raise ValueError('模型并发覆盖最多 100 条')
    result=[];seen=set()
    for row in overrides:
        if not isinstance(row,dict): raise ValueError('模型并发覆盖格式无效')
        url=connection_url(row.get('base_url',''));model=str(row.get('model','')).strip()
        parts=urlsplit(url)
        if parts.scheme not in ('http','https') or not parts.netloc or parts.username or parts.password or not model: raise ValueError('请填写有效的服务地址和模型 ID，地址不得包含密钥')
        value,_=normalize_limits(row.get('limit'),[])
        if (url,model) in seen: raise ValueError('同一服务地址和模型不能重复配置并发')
        seen.add((url,model));result.append({'base_url':url,'model':model,'limit':value})
    return limit,result


def current_limit(base_url,model):
    from .config import SETTINGS_FILE
    try: value=json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    except (OSError,ValueError): value={}
    default,rows=normalize_limits(value.get('model_api_concurrency',50),value.get('model_api_concurrency_overrides',[]))
    return next((r['limit'] for r in rows if r['base_url']==connection_url(base_url) and r['model']==model),default)


def _try_lock(handle):
    handle.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        return True
    except (BlockingIOError,OSError): return False


def _unlock(handle):
    handle.seek(0)
    if os.name=='nt':
        import msvcrt
        msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(),fcntl.LOCK_UN)


def _open_lock(path):
    h=path.open('a+b',buffering=0)
    if path.stat().st_size==0: h.write(b'0')
    return h


@contextmanager
def model_slot(base_url,model,api_key='',*,cancelled=None,root=None,limit_reader=None):
    from .config import APP_DATA_DIR
    # Never persist the account credential. Separate keys are separate connections.
    scope=hashlib.sha256(json.dumps([connection_url(base_url),str(model).strip(),api_key],ensure_ascii=False).encode()).hexdigest()
    folder=Path(root or APP_DATA_DIR/'model-api-slots')/scope;folder.mkdir(parents=True,exist_ok=True)
    cancelled = cancelled or (_cancel_event.is_set if _cancel_event is not None else None)
    slot=None;slot_path=None
    while slot is None:
        if cancelled and cancelled(): raise InterruptedError('已取消等待模型 API 额度')
        guard=_open_lock(folder/'registry.lock')
        try:
            if _try_lock(guard):
                try:
                    active=0
                    for path in folder.glob('*.slot'):
                        probe=_open_lock(path)
                        try:
                            if _try_lock(probe): _unlock(probe);probe.close();path.unlink(missing_ok=True)
                            else: active+=1
                        finally:
                            if not probe.closed: probe.close()
                    limit=(limit_reader or current_limit)(base_url,model)
                    if active<limit:
                        slot_path=folder/(uuid.uuid4().hex+'.slot');slot=_open_lock(slot_path)
                        if not _try_lock(slot): slot.close();slot=None
                finally: _unlock(guard)
        finally: guard.close()
        if slot is None: time.sleep(.05)
    try: yield
    finally:
        # Leave a closed file for the next admission to reclaim under registry lock.
        _unlock(slot);slot.close()


def retry_after_seconds(response,attempt):
    from email.utils import parsedate_to_datetime
    from datetime import datetime,timezone
    raw=getattr(response,'headers',{}).get('Retry-After','') if response is not None else ''
    try: return max(0,float(raw))
    except (ValueError,TypeError):
        try: return max(0,(parsedate_to_datetime(raw)-datetime.now(timezone.utc)).total_seconds())
        except (ValueError,TypeError,OverflowError): return min(60,2**max(0,attempt-1))
