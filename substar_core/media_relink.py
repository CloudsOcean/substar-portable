"""Explicit native media selection; staged until task-info Save."""
import hashlib
import os
from pathlib import Path
import subprocess
import time
import uuid

_pending = {}

def fingerprint(path):
    path=Path(path)
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        digest.update(stream.read(1024*1024))
        stream.seek(max(0,path.stat().st_size-1024*1024))
        digest.update(stream.read(1024*1024))
    return [path.stat().st_size,digest.hexdigest()]

def select_media(project_id, previous):
    if os.name != 'nt':
        raise ValueError('当前平台不支持 Windows 文件选择窗口')
    script = '''Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = '重新链接媒体'
$dialog.Filter = '媒体文件|*.mp4;*.mov;*.mkv;*.avi;*.webm;*.m4v;*.mp3;*.wav;*.m4a;*.aac;*.flac;*.ogg'
$dialog.CheckFileExists = $true
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Write($dialog.FileName) }
$dialog.Dispose()
'''
    try:
        result=subprocess.run(['powershell.exe','-NoProfile','-STA','-Command',script],capture_output=True,timeout=300,creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('文件选择超时，请重试') from exc
    if result.returncode: raise ValueError('无法打开媒体选择窗口')
    selected=result.stdout.decode('utf-8-sig').strip()
    if not selected: return {'cancelled':True}
    path=Path(selected).resolve()
    signature=fingerprint(path)
    same=bool(previous and Path(previous).is_file() and fingerprint(previous)==signature)
    token=uuid.uuid4().hex
    for key,value in list(_pending.items()):
        if time.time()-value[3]>600: _pending.pop(key,None)
    _pending[token]=(project_id,str(path),signature,time.time())
    return {'token':token,'path':str(path),'needs_confirmation':not same}

def resolve_selection(project_id,token):
    item=_pending.get(token)
    if not item or item[0]!=project_id or time.time()-item[3]>600:
        raise ValueError('媒体选择已过期，请重新选择')
    if fingerprint(item[1])!=item[2]: raise ValueError('选中的媒体已变化，请重新选择')
    return item[1]
