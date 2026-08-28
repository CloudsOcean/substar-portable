from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
from typing import Any

from substar_core.environment_doctor import _find_tool


PACKET_SEPARATION_LIMIT = 16 * 1024 * 1024
_PROXY_LOCK = threading.Lock()


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


def _stream_position_gap(payload: dict[str, Any]) -> int:
    positions: dict[int, list[int]] = {}
    for packet in payload.get("packets", []):
        try:
            stream_index = int(packet["stream_index"])
            position = int(packet["pos"])
        except (KeyError, TypeError, ValueError):
            continue
        positions.setdefault(stream_index, []).append(position)
    if len(positions) < 2:
        return 0
    centers = [sum(values) // len(values) for values in positions.values() if values]
    return max(centers) - min(centers) if len(centers) >= 2 else 0


def needs_interleaved_proxy(source: Path) -> bool:
    if source.suffix.lower() not in {".mp4", ".mov", ".m4v"}:
        return False
    metadata = _run_json(
        [
            _find_tool("ffprobe") or "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_type",
            "-of",
            "json",
            str(source),
        ]
    )
    stream_types = {
        int(stream["index"]): str(stream.get("codec_type", ""))
        for stream in metadata.get("streams", [])
        if "index" in stream
    }
    if "video" not in stream_types.values() or "audio" not in stream_types.values():
        return False
    try:
        duration = float(metadata["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return False
    for fraction in (0.2, 0.65):
        start = max(0.0, min(duration - 2.0, duration * fraction))
        packets = _run_json(
            [
                _find_tool("ffprobe") or "ffprobe",
                "-v",
                "error",
                "-read_intervals",
                f"{start:.3f}%+1.5",
                "-show_packets",
                "-show_entries",
                "packet=stream_index,pos",
                "-of",
                "json",
                str(source),
            ]
        )
        if _stream_position_gap(packets) > PACKET_SEPARATION_LIMIT:
            return True
    return False


def _remux(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.stem}.building.mp4")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        subprocess.run(
            [
                _find_tool("ffmpeg") or "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(source),
                "-map",
                "0:v?",
                "-map",
                "0:a?",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-max_interleave_delta",
                "0",
                str(temporary),
            ],
            check=True,
            capture_output=True,
            timeout=900,
            creationflags=creationflags,
        )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_playback_media(source: Path, cache_root: Path) -> Path:
    """Return a browser-friendly cached remux only for poorly interleaved media."""

    source = source.resolve()
    fingerprint = _source_fingerprint(source)
    metadata_path = cache_root / "playback_source.json"
    proxy_path = cache_root / f"{source.stem}.interleaved.mp4"
    with _PROXY_LOCK:
        metadata: dict[str, Any] = {}
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            pass
        if metadata.get("source") == fingerprint:
            if not metadata.get("needs_proxy", False):
                return source
            if proxy_path.is_file() and proxy_path.stat().st_size > 0:
                return proxy_path
        try:
            needs_proxy = needs_interleaved_proxy(source)
            if needs_proxy:
                _remux(source, proxy_path)
            cache_root.mkdir(parents=True, exist_ok=True)
            metadata_path.write_text(
                json.dumps(
                    {"source": fingerprint, "needs_proxy": needs_proxy},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return proxy_path if needs_proxy else source
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
            return source
