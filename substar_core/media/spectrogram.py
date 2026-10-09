"""Bounded, windowed PCM spectrogram preview (independent implementation)."""
from functools import lru_cache
from pathlib import Path
import struct
import wave
import zlib

import numpy as np

from .waveform_cache import pcm_wave_frame_count


def spectral_levels(audio: Path, start: float, end: float, width: int, height: int):
    """Linear 0–Nyquist frequency, high frequencies at the top; fixed -80–0 dBFS."""
    with wave.open(str(audio), "rb") as source:
        if source.getsampwidth() != 2:
            raise ValueError("声谱图只支持 16-bit PCM")
        rate, channels = source.getframerate(), source.getnchannels()
        count = pcm_wave_frame_count(audio, source)
        first = max(0, int(start * rate) - 256)
        last = min(count, int(end * rate) + 256)
        if first >= count:
            return np.zeros((height, width), dtype=np.uint8)
        source.setpos(first)
        raw = source.readframes(max(0, last - first))
    samples = np.frombuffer(raw, dtype="<i2").reshape(-1, channels).mean(axis=1) / 32768
    # Sample every 10 ms, then max-pool time columns to retain short transients.
    centers = np.arange(int(start * rate), int(end * rate), max(1, rate // 100))
    centers = np.unique(np.concatenate((centers, (start * rate + (np.arange(width) + .5) * (end-start)*rate/width).astype(int))))
    window = np.hanning(512)
    output = np.zeros((height, width), dtype=np.float32)
    for offset in range(0, len(centers), 256):
        batch = centers[offset:offset+256]
        indices = batch[:, None] + np.arange(512) - 256 - first
        valid = (indices >= 0) & (indices < len(samples))
        frames = samples[np.clip(indices, 0, max(0, len(samples)-1))] * valid if len(samples) else np.zeros(indices.shape)
        amplitude = np.abs(np.fft.rfft(frames * window, axis=1)) * 2 / window.sum()
        db = np.clip((20 * np.log10(np.maximum(amplitude, 1e-8)) + 80) / 80, 0, 1)
        bands = np.stack([db[:, lo:max(lo+1, hi)].max(axis=1) for lo, hi in zip(np.linspace(0,257,height+1).astype(int)[:-1], np.linspace(0,257,height+1).astype(int)[1:])], axis=1)
        columns = np.clip(((batch/rate-start)/(end-start)*width).astype(int), 0, width-1)
        np.maximum.at(output.T, columns, bands)
    return (output[::-1] * 255).astype(np.uint8)


@lru_cache(maxsize=24)
def _render(path: str, stamp: int, size: int, start: float, end: float, width: int, height: int) -> bytes:
    levels = spectral_levels(Path(path), start, end, width, height)
    stops = np.array([[14,20,25], [39,32,89], [116,47,137], [216,92,85], [255,216,108]], dtype=float)
    palette = np.stack([np.interp(np.arange(256), np.linspace(0,255,len(stops)), stops[:, channel]) for channel in range(3)], axis=1).astype(np.uint8)
    pixels = palette[levels]
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
    raw = b"".join(b"\0" + row.tobytes() for row in pixels)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", width,height,8,2,0,0,0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def spectrogram_png(audio: Path, start: float, end: float, width: int = 1200, height: int = 192) -> bytes:
    if not (0 <= start < end <= start + 60 and 128 <= width <= 1600 and 32 <= height <= 256):
        raise ValueError("声谱图窗口或尺寸无效")
    stat = audio.stat()
    return _render(str(audio), stat.st_mtime_ns, stat.st_size, start, end, width, height)

