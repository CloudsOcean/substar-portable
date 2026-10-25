"""Fixed 10 ms min/max source peaks for the imported MAW waveform and gate."""
from __future__ import annotations
import base64
import math
import sys
import wave
from array import array
from fastapi import APIRouter, HTTPException
from substar_core.artifacts import atomic_write_json
from substar_core.media.waveform_cache import pcm_wave_frame_count

router = APIRouter()

def encode_waveform(audio):
    with wave.open(str(audio), 'rb') as source:
        if source.getsampwidth() != 2:
            raise ValueError('波形预览只支持 16-bit PCM')
        rate, channels = source.getframerate(), source.getnchannels()
        division = max(1, rate // 100)
        count = pcm_wave_frame_count(audio, source)
        peaks = array('b')
        while True:
            raw = source.readframes(division * 4096)
            if not raw:
                break
            samples = array('h')
            samples.frombytes(raw)
            if sys.byteorder != 'little':
                samples.byteswap()
            for offset in range(0, len(samples), division * channels):
                chunk = samples[offset:offset + division * channels]
                low = max(-127, min(0, math.floor(min(chunk) * 127 / 32768)))
                high = min(127, max(0, math.ceil(max(chunk) * 127 / 32768)))
                peaks.extend((low, high))
    return {'schema':'moy.asr.waveform.v1', 'encoding':'i8-minmax-base64',
            'sample_rate':rate, 'division':division, 'peaks_per_second':rate/division,
            'duration_ms':round(count * 1000 / rate), 'peak_count':len(peaks)//2,
            'data':base64.b64encode(peaks.tobytes()).decode('ascii')}

@router.get('/projects/{project_id}/waveform-maw')
def get_maw_waveform(project_id: str):
    import json
    from .http_api import project_job_path, project_store_path
    root = project_job_path(project_id).resolve()
    audio = (root / 'audio_16k_mono.wav').resolve()
    if root not in audio.parents or not audio.is_file():
        raise HTTPException(404, '项目没有可用的波形音频')
    stat = audio.stat()
    identity = {'size':stat.st_size, 'mtime_ns':stat.st_mtime_ns, 'version':1}
    cache = project_store_path(project_id) / 'waveform_maw.json'
    try:
        saved = json.loads(cache.read_text(encoding='utf-8'))
        if saved.get('source') == identity:
            return saved['payload']
    except (OSError, ValueError, KeyError):
        pass
    try:
        payload = encode_waveform(audio)
    except (ValueError, wave.Error) as exc:
        raise HTTPException(422, str(exc)) from exc
    atomic_write_json(cache, {'source':identity, 'payload':payload})
    return payload


@router.get('/projects/{project_id}/waveform-maw-detail')
def get_maw_detail_waveform(project_id: str):
    from .maw_detail_peaks import get_detail_waveform
    return get_detail_waveform(project_id)
