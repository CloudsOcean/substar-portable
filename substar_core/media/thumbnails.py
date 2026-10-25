"""Project-local filmstrip generation, independent of subtitle revisions."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from substar_core.artifacts import atomic_write_json
from substar_core.environment_doctor import _find_tool

WIDTH, HEIGHT, CELLS = 160, 90, 64
MAX_FRAMES = 14400
MAX_BYTES = 96 * 1024 * 1024
_lock = threading.Lock()
_decode_slot = threading.Semaphore(1)
_running: set[str] = set()


def identity(source: Path) -> str:
    stat = source.stat()
    return hashlib.sha256(f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()[:24]


def cache_directory(project: Path, source: Path) -> Path:
    return project / "thumbnail_cache" / identity(source)


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def status(project: Path, source: Path) -> dict:
    directory = cache_directory(project, source)
    result = _read(directory / "manifest.json")
    if not result:
        result = {"state": "idle", "progress": 0}
    result["revision"] = directory.name
    result["requested"] = (project / "thumbnail_request.json").is_file()
    return result


def request_generation(project: Path, source: Path, *, retry: bool = False) -> dict:
    directory = cache_directory(project, source)
    key = str(directory.resolve())
    with _lock:
        current = status(project, source)
        if key in _running or current["state"] in {"ready", "unavailable"}:
            return current
        if current["state"] == "error" and not retry:
            return current
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_json(project / "thumbnail_request.json", {"enabled": True})
        atomic_write_json(directory / "manifest.json", {"state": "queued", "progress": 0})
        _running.add(key)
        threading.Thread(target=_worker, args=(project, source, directory, key), daemon=True,
                         name="video-thumbnails").start()
    return status(project, source)


def _worker(project: Path, source: Path, directory: Path, key: str) -> None:
    try:
        with _decode_slot:
            _generate(source, directory)
    except Exception:
        atomic_write_json(directory / "manifest.json", {
            "state": "error", "progress": 0,
            "message": "画面预览图生成失败，可重试；字幕编辑不受影响",
        })
    finally:
        with _lock:
            _running.discard(key)


def _generate(source: Path, directory: Path) -> None:
    ffmpeg, ffprobe = _find_tool("ffmpeg"), _find_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg unavailable")
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS) if os.name == "nt" else 0
    probe = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)],
                           capture_output=True, check=True, timeout=30, creationflags=flags)
    info = json.loads(probe.stdout)
    video = next((stream for stream in info.get("streams", []) if stream.get("codec_type") == "video"
                  and not stream.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        atomic_write_json(directory / "manifest.json", {"state": "unavailable", "progress": 1,
                                                       "message": "此素材没有视频画面"})
        return
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Invalid duration")
    # Half-second sampling for ordinary clips; cap work and storage for long media.
    interval = max(.5, math.ceil(duration / MAX_FRAMES * 2) / 2)
    count = math.ceil(duration / interval)
    expected = math.ceil(count / CELLS)
    base = {"state": "generating", "progress": 0, "interval": interval, "count": count,
            "duration": duration, "width": WIDTH, "height": HEIGHT, "columns": 8, "cells": CELLS}
    atomic_write_json(directory / "manifest.json", base)
    origin = float(info.get("format", {}).get("start_time") or video.get("start_time") or 0)
    # Keep delayed video aligned with source time; do not stretch it back to zero.
    offset = max(0, float(video.get("start_time") or origin) - origin)
    filters = (f"setpts=PTS-STARTPTS+{offset}/TB,fps=1/{interval}:start_time=0,"
               "scale=160:90:force_original_aspect_ratio=decrease:force_divisible_by=2,"
               "setsar=1,pad=160:90:(ow-iw)/2:(oh-ih)/2,tile=8x8:padding=0:margin=0")
    # Stage all images before publishing: readers only see complete, immutable assets.
    with tempfile.TemporaryDirectory(prefix="build-", dir=directory) as staging:
        output = Path(staging)
        with tempfile.TemporaryFile() as errors:
            command = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-threads", "2", "-i", str(source),
                       "-map", f"0:{video['index']}", "-an", "-sn", "-dn", "-filter_threads", "1",
                       "-vf", filters, "-frames:v", str(expected), "-q:v", "5", "-threads", "2",
                       "-y", str(output / "sprite-%05d.jpg")]
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors, creationflags=flags)
            deadline, last_progress = time.monotonic() + 3600, -1
            try:
                while process.poll() is None:
                    files = list(output.glob("sprite-*.jpg"))
                    if sum(file.stat().st_size for file in files) > MAX_BYTES or time.monotonic() > deadline:
                        raise RuntimeError("Thumbnail budget exceeded")
                    progress = min(.99, len(files) / expected)
                    if progress != last_progress:
                        atomic_write_json(directory / "manifest.json", {**base, "progress": progress})
                        last_progress = progress
                    time.sleep(.5)
                if process.returncode:
                    raise RuntimeError("Thumbnail decode failed")
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
        files = sorted(output.glob("sprite-*.jpg"))
        if not files or sum(file.stat().st_size for file in files) > MAX_BYTES:
            raise RuntimeError("Invalid thumbnail output")
        for file in files:
            os.replace(file, directory / file.name)
        atomic_write_json(directory / "manifest.json", {**base, "state": "ready", "progress": 1,
                                                       "sprites": len(files)})
