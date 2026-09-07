import json
from pathlib import Path
import pytest
from substar_core.media_reference import write_reference,resolve_reference
from substar_core.project_exchange import _portable_media_entries

def test_reference_export_rewrites_media_path_without_copying(tmp_path):
    job=tmp_path/'project'; job.mkdir()
    media=tmp_path/'原片.mp4'; media.write_bytes(b'original')
    write_reference(job,media)
    assert resolve_reference(job)==media
    assert not (job/'input').exists()
    entries=_portable_media_entries([],job)
    assert ('project/input/原片.mp4',media,None) in entries
    manifest=json.loads(next(content for name,path,content in entries if name=='project/run_manifest.json'))
    assert manifest['source_path']=='input/原片.mp4'
    assert not any('media_reference' in name for name,_,_ in entries)
    media.unlink()
    with pytest.raises(FileNotFoundError): _portable_media_entries([],job)
