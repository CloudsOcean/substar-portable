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

def waveform_path(project):
    project = Path(project)
    ref = read_reference(project) or {}
    audio = ref.get("waveform")
    if audio and Path(audio).name == audio and audio.startswith("relinked-") and audio.endswith(".wav"):
        return project / audio
    return project / "audio_16k_mono.wav"


def relink_media(project, path):
    import tempfile
    from uuid import uuid4
    from .transcription.cloud_pipeline import _extract_audio, _probe_media
    path = Path(path).resolve()
    old_audio = waveform_path(project)
    with tempfile.TemporaryDirectory(prefix=".relink-", dir=project) as stage:
        audio = Path(stage) / ("relinked-" + uuid4().hex + ".wav")
        try:
            media = _probe_media(path)
            _extract_audio(path, audio, "off", media_duration_seconds=media["duration_seconds"])
        except Exception as exc:
            raise ValueError("新媒体解码失败，无法重新生成波形") from exc
        final = Path(project) / audio.name
        audio.replace(final)
        try:
            write_reference(project, path, waveform=final.name, media=media)
        except Exception:
            final.unlink(missing_ok=True)
            raise
    if old_audio.name.startswith("relinked-"):
        try:
            old_audio.unlink(missing_ok=True)
        except PermissionError:
            pass


def write_reference(project,path, *, waveform=None, media=None):
    path=Path(path).resolve()
    if not path.is_file(): raise FileNotFoundError('原媒体不存在，请重新链接')
    atomic_write_json(Path(project)/'media_reference.json',{'schema_version':'substar.media-reference.v1','path':str(path),'relative_path':'input/'+path.name, **({'waveform': waveform, 'media': media} if waveform else {})})
