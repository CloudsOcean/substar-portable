from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from substar_core.artifacts import atomic_write_json, atomic_write_text
from substar_core.environment_doctor import _find_tool
from substar_core.qwen_cloud_asr import DEFAULT_MODEL, run_qwen_cloud_asr
from substar_core.recognition.registry import profile_settings

from .artifacts import alignment_tsv, segmentation_material


Progress = Callable[[str, float], None]


def _run(
    command: list[str], *, timeout_seconds: float | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
    )


def _probe_media(path: Path) -> dict[str, Any]:
    result = _run(
        [
            _find_tool("ffprobe") or "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name:stream=index,codec_type,codec_name,r_frame_rate,avg_frame_rate,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ]
    )
    data = json.loads(result.stdout)
    video = next(
        (stream for stream in data.get("streams", []) if stream.get("codec_type") == "video"),
        {},
    )
    fps_raw = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    try:
        numerator, denominator = (int(part) for part in fps_raw.split("/", 1))
        fps = numerator / denominator if denominator else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "duration_seconds": round(float(data.get("format", {}).get("duration", 0.0)), 3),
        "format": data.get("format", {}).get("format_name", ""),
        "fps": round(fps, 6),
        "fps_rational": fps_raw,
        "streams": data.get("streams", []),
    }


def _extract_audio(
    media_path: Path,
    wav_path: Path,
    denoise_mode: str,
    *,
    media_duration_seconds: float,
) -> None:
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    if wav_path.is_file() and wav_path.stat().st_size > 44:
        return
    command = [
        _find_tool("ffmpeg") or "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(media_path),
        "-vn", "-ac", "1", "-ar", "16000",
    ]
    if denoise_mode == "light":
        command.extend(["-af", "afftdn=nr=8:nf=-35"])
    command.extend(["-c:a", "pcm_s16le", str(wav_path)])
    timeout_seconds = max(300.0, min(3600.0, media_duration_seconds * 2.0 + 120.0))
    try:
        _run(command, timeout_seconds=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        try:
            wav_path.unlink()
        except FileNotFoundError:
            pass
        raise RuntimeError(
            f"FFmpeg audio extraction exceeded {int(timeout_seconds)} seconds"
        ) from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_cloud_pipeline(
    media_path: Path,
    artifact_directory: Path,
    settings: dict[str, Any],
    progress: Progress,
) -> dict[str, Any]:
    """Produce immutable word-level evidence from the sole Beta ASR provider."""

    artifact_directory.mkdir(parents=True, exist_ok=True)
    progress("读取媒体信息", 0.05)
    media = _probe_media(media_path)
    wav_path = artifact_directory / "audio_16k_mono.wav"
    progress("提取 16kHz 单声道音频", 0.12)
    _extract_audio(
        media_path,
        wav_path,
        str(settings.get("audio_denoise_mode", "off")),
        media_duration_seconds=float(media.get("duration_seconds", 0.0)),
    )

    resolved = profile_settings(settings)
    resolved["_checkpoint_dir"] = str(artifact_directory / "ingest_chunks")
    result = run_qwen_cloud_asr(wav_path, resolved, progress)
    progress("写入听写证据", 0.84)

    master = str(result["text"]).strip()
    transcript_engine = str(resolved.get("qwen_cloud_model") or DEFAULT_MODEL)
    source = {
        "schema_version": "substar.recognition-source.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "media": {"original_name": media_path.name, "sha256": _sha256(media_path), **media},
        "engines": {
            "profile_id": "qwen_cloud",
            "transcript": transcript_engine,
            "alignment": "qwen-cloud:native-word-timestamps",
            "diarization": resolved.get("recognition_diarization_adapter"),
        },
        "language": result.get("language", ""),
        "master_text": master,
        "chunks": result.get("chunks", []),
        "units": result["units"],
    }
    atomic_write_text(artifact_directory / "master_transcript.txt", master + "\n")
    atomic_write_text(artifact_directory / "alignment.tsv", alignment_tsv(result["units"]))
    atomic_write_json(
        artifact_directory / "segmentation_material.json",
        segmentation_material(master, source),
    )
    if result.get("audit"):
        atomic_write_json(artifact_directory / "asr_ingest_report.json", result["audit"])

    public_configuration = {
        key: value
        for key, value in resolved.items()
        if not key.startswith("_")
        and "key" not in key.lower()
        and isinstance(value, (str, int, float, bool, type(None)))
    }
    atomic_write_json(
        artifact_directory / "run_manifest.json",
        {
            "schema_version": "substar.run.v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_file": media_path.name,
            "source_path": str(media_path.resolve()),
            "transcript_engine": transcript_engine,
            "alignment_engine": "qwen-cloud:native-word-timestamps",
            "language": result.get("language", ""),
            "master_character_count": len(master),
            "alignment_unit_count": len(result["units"]),
            "media": media,
            "configuration": public_configuration,
        },
    )
    progress("校验听写产物", 0.99)
    return source
