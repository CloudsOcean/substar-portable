"""External media is referenced locally; never imported from untrusted packages."""
import json
from pathlib import Path
from substar_core.artifacts import atomic_write_json

def read_reference(project):
    path=Path(project)/'media_reference.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

def resolve_reference(project):
    value=read_reference(project)
    if not value: return None
    path=Path(value['path'])
    if not path.is_absolute() or not path.is_file():
        raise FileNotFoundError('原媒体不存在，请重新链接')
    return path

def write_reference(project,path):
    path=Path(path).resolve()
    if not path.is_file(): raise FileNotFoundError('原媒体不存在，请重新链接')
    atomic_write_json(Path(project)/'media_reference.json',{'schema_version':'substar.media-reference.v1','path':str(path),'relative_path':'input/'+path.name})
