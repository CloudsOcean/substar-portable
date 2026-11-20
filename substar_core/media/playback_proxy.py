from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
from typing import Any

from substar_core.environment_doctor import _find_tool


def _run_json(command: list[str], *, timeout: int = 30) -> dict[str, Any]:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=creationflags,
    )
    value = json.loads(completed.stdout)
    return value if isinstance(value, dict) else {}


def _source_fingerprint(source: Path) -> dict[str, int]:
    stat = source.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


_PREVIEW_LOCK = threading.Lock()
_PREVIEW_JOBS: dict[Path, dict[str, Any]] = {}
_PREVIEW_WORKER = threading.Semaphore(1)


def preview_file(source: Path, cache_root: Path) -> Path:
    import hashlib

    identity = f"{source.resolve()}|{_source_fingerprint(source)}|h264-aac-720p-v1"
    key = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return cache_root / f"{key}.compatible.mp4"


def request_compatible_preview(source: Path, cache_root: Path, *, start=False) -> dict[str, Any]:
    destination = preview_file(source, cache_root)
    with _PREVIEW_LOCK:
        if destination.is_file() and destination.stat().st_size:
            return {"state": "ready", "progress": 1, "asset": destination.stem}
        current = _PREVIEW_JOBS.get(destination, {"state": "idle", "progress": 0})
        if start and current["state"] in {"idle", "error"}:
            current = {"state": "queued", "progress": 0}
            _PREVIEW_JOBS[destination] = current
            threading.Thread(
                target=_build_compatible_preview, args=(source, destination),
                daemon=True, name="compatible-preview",
            ).start()
        return dict(current)


def _build_compatible_preview(source: Path, destination: Path) -> None:
    from substar_core.media.video_encoding import encode_video

    def update(progress, encoder, reason):
        with _PREVIEW_LOCK:
            _PREVIEW_JOBS[destination] = {
                "state": "running", "progress": progress, "encoder": encoder,
            }

    temporary = destination.with_suffix(".building.mp4")
    try:
        with _PREVIEW_WORKER:
            info = _run_json([
                _find_tool("ffprobe") or "ffprobe", "-v", "error",
                "-show_streams", "-show_format", "-of", "json", str(source),
            ])
            video = next((s for s in info["streams"] if s["codec_type"] == "video"
                          and not s.get("disposition", {}).get("attached_pic")), None)
            if video is None:
                raise ValueError("此素材没有视频画面")
            duration = float(info["format"]["duration"])
            audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
            destination.parent.mkdir(parents=True, exist_ok=True)

            def args(options):
                command = [
                    "-copyts", "-start_at_zero", "-i", str(source),
                    "-map", f"0:{video['index']}",
                    "-vf", "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2:reset_sar=1",
                    "-fps_mode", "passthrough", *options,
                ]
                if audio is not None:
                    command += ["-map", f"0:{audio['index']}", "-c:a", "copy" if audio["codec_name"] == "aac" else "aac"]
                return command + ["-movflags", "+faststart"]

            encode_video(args, temporary, codec="h264", duration=duration,
                         quality=25, fast=True, update=update)
            if preview_file(source, destination.parent) != destination:
                raise ValueError("原素材已更新，请重新生成预览")
            os.replace(temporary, destination)
            with _PREVIEW_LOCK:
                _PREVIEW_JOBS.pop(destination, None)
    except Exception as exc:
        reason = str(exc).splitlines()
        with _PREVIEW_LOCK:
            _PREVIEW_JOBS[destination] = {
                "state": "error", "progress": 0,
                "message": reason[0] if reason else "无法生成兼容预览",
            }
    finally:
        temporary.unlink(missing_ok=True)
