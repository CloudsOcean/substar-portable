"""Native quapeaks generation and source-validated fine waveform caches."""
from __future__ import annotations
import base64
import json
import math
import os
import struct
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from fastapi import HTTPException
from substar_core.artifacts import atomic_write_json

_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()
PEAKS_PER_SECOND = 2000


def decode_container(data: bytes) -> dict:
    if len(data) < 18 or data[:4] not in (b'QPK1', b'RPKM', b'RPKN', b'RPKL'):
        raise ValueError('不支持的峰值缓存格式')
    magic = data[:4]
    channels, levels = data[4:6]
    rate = struct.unpack_from('<I', data, 6)[0]
    if not 0 < channels <= 64 or not 0 < levels <= 64 or not 0 < rate <= 768000 or len(data) < 18 + levels * 8:
        raise ValueError('峰值缓存头部无效')
    headers = [struct.unpack_from('<ii', data, 18 + i * 8) for i in range(levels)]
    offset = 18 + levels * 8
    finest = None
    for division, count in headers:
        if count < 0:
            raise ValueError('峰值数量无效')
        size = count * channels * (2 if magic == b'RPKM' else 4)
        if division == -ord('m'):
            size = 8 + count * 2
        elif division == -ord('g'):
            size = count * channels * 192
        elif division < 0:
            if division not in (-ord('s'), -ord('r'), -ord('l')):
                raise ValueError('未知峰值缓存层')
            size = count * channels * 4
        if offset + size > len(data):
            raise ValueError('峰值缓存文件不完整')
        if division > 0 and (finest is None or division < finest[0]):
            finest = (division, count, offset)
        offset += size
    if finest is None:
        raise ValueError('缓存没有波形层')
    division, count, offset = finest
    def quantize(v):
        if magic == b'RPKL':
            v = v / 24576 if abs(v) <= 24576 else math.copysign(2 ** ((abs(v) - 24576) / 1024), v)
        else:
            v /= 32768
        return max(-127, min(127, round(v * 127)))
    peaks = bytearray()
    for _ in range(count):
        low, high = 0, 0
        for _ in range(channels):
            maximum = struct.unpack_from('<h', data, offset)[0]
            offset += 2
            minimum = -maximum if magic == b'RPKM' else struct.unpack_from('<h', data, offset)[0]
            if magic != b'RPKM':
                offset += 2
            low, high = min(low, quantize(minimum)), max(high, quantize(maximum))
        peaks.extend((low & 255, high & 255))
    return {'schema': 'moy.asr.waveform.v1', 'encoding': 'i8-minmax-base64',
            'sample_rate': rate, 'division': division, 'peaks_per_second': rate / division,
            'peak_count': count, 'duration_ms': round(count * division * 1000 / rate),
            'data': base64.b64encode(peaks).decode('ascii')}


def generate_container(source: Path, target: Path):
    from substar_core.vendor.quapeaks import _native
    from .cut_api import executable
    # Decode the original media at its native rate, with every channel preserved.
    # Temporary PCM keeps memory bounded and permits reliable WAV-header parsing.
    with tempfile.TemporaryDirectory(prefix='substar-peaks-', dir=target.parent) as tmp:
        pcm = Path(tmp) / 'source.wav'
        process = subprocess.run([executable('ffmpeg'), '-nostdin', '-v', 'error', '-i', str(source),
                                  '-map', '0:a:0', '-vn', '-c:a', 'pcm_s16le', '-rf64', 'never', str(pcm)],
                                 capture_output=True, timeout=600,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if process.returncode:
            raise ValueError('音频解码失败：' + process.stderr.decode('utf-8', errors='replace')[-600:])
        with wave.open(str(pcm), 'rb') as audio:
            rate, channels = audio.getframerate(), audio.getnchannels()
            divs = sorted(set(max(1, round(rate / pps)) for pps in (PEAKS_PER_SECOND, 300, 20)))
            streamer = _native.ReapeaksStreamer(rate, channels, divs=divs, features=['wave'],
                                                mipmap_levels=len(divs), flavor='quapeaks')
            while chunk := audio.readframes(65536):
                streamer.feed(chunk)
            duration = round(audio.getnframes() * 1000 / rate)
        stat = source.stat()
        data = bytes(streamer.finish(src_timestamp=int(stat.st_mtime), src_filesize=stat.st_size & 0xffffffff))
        decode_container(data)  # Never publish a partial or unusable binary cache.
        temporary = Path(tmp) / 'wave.quapeaks'
        temporary.write_bytes(data)
        os.replace(temporary, target)
        return duration


def get_detail_waveform(project_id: str):
    from .http_api import _project_media_source, project_store_path
    source, _ = _project_media_source(project_id)
    source = Path(source).resolve()
    stat = source.stat()
    identity = {'path': str(source), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                'version': 1, 'density': PEAKS_PER_SECOND, 'audio_track': 0}
    identity['sidecars'] = {str(p): p.stat().st_mtime_ns for suffix in ('.quapeaks', '.ReaPeaks', '.reapeaks', '.REAPEAKS') if (p := Path(str(source) + suffix)).is_file()}
    folder = project_store_path(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    cache = folder / 'waveform_detail.json'
    with _guard:
        lock = _locks.setdefault(str(cache), threading.Lock())
    with lock:
        try:
            saved = json.loads(cache.read_text(encoding='utf-8'))
            if saved['source'] == identity:
                return {**saved['payload'], 'cache_hit': True}
        except (OSError, ValueError, KeyError):
            pass
        try:
            # Use an existing source-matching sidecar when available.
            payload = None
            for suffix in ('.quapeaks', '.ReaPeaks', '.reapeaks', '.REAPEAKS'):
                sidecar = Path(str(source) + suffix)
                if not sidecar.is_file():
                    continue
                try:
                    data = sidecar.read_bytes()
                    timestamp, size = struct.unpack_from('<II', data, 10)
                    if size != stat.st_size & 0xffffffff or abs(timestamp - int(stat.st_mtime)) > 5:
                        continue
                    payload = decode_container(data)
                    payload['source_kind'] = suffix.lstrip('.')
                    break
                except (OSError, ValueError, struct.error):
                    continue
            if payload is None:
                duration = generate_container(source, folder / 'waveform.quapeaks')
                payload = decode_container((folder / 'waveform.quapeaks').read_bytes())
                payload.update(duration_ms=duration, source_kind='quapeaks')
            if source.stat().st_mtime_ns != stat.st_mtime_ns or source.stat().st_size != stat.st_size:
                raise ValueError('生成期间素材已变化，请重试')
            atomic_write_json(cache, {'source': identity, 'payload': payload})
            return {**payload, 'cache_hit': False}
        except (ImportError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(422, f'精细波形加载失败：{exc}') from exc
