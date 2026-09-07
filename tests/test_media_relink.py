import time
import pytest
from substar_core import media_relink

def test_staged_selection_is_project_bound_and_checks_file(tmp_path):
    path=tmp_path/'video.mp4'
    path.write_bytes(b'media')
    token='test-selection'
    media_relink._pending[token]=('p',str(path),media_relink.fingerprint(path),time.time())
    assert media_relink.resolve_selection('p',token)==str(path)
    with pytest.raises(ValueError): media_relink.resolve_selection('other',token)
    path.write_bytes(b'changed')
    with pytest.raises(ValueError): media_relink.resolve_selection('p',token)
    media_relink._pending.pop(token)

def test_expired_selection_rejected(tmp_path):
    media_relink._pending['expired']=('p','unused',[],0)
    with pytest.raises(ValueError): media_relink.resolve_selection('p','expired')
    media_relink._pending.pop('expired')

def test_form_has_readonly_path_and_explicit_relink():
    from pathlib import Path
    html=(Path(__file__).resolve().parents[1]/'web/editor.html').read_text(encoding='utf-8')
    assert '这些设置只影响当前任务' not in html
    assert 'id="taskInfoMediaPath" readonly' in html
    assert 'id="relinkTaskMedia" type="button"' in html
