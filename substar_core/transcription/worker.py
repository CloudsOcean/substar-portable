from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

from substar_core.artifacts import atomic_write_json
from substar_core.transcription.cloud_pipeline import run_cloud_pipeline
from substar_core.runtime.worker_protocol import (
    WorkerCommand,
    WorkerControl,
    WorkerControlType,
    WorkerMessage,
    WorkerMessageType,
    WorkerTaskType,
    credential_environment_key,
    parse_command_line,
    utc_now,
)
from substar_core.credential_store import ASR_GENERIC, ASR_QWEN
from substar_core.segmentation.input_contract import SEGMENTATION_MATERIAL_SCHEMA

from .contracts import (
    RECOGNITION_EVIDENCE_SCHEMA,
    TRANSCRIPTION_INPUT_SCHEMA,
    TRANSCRIPTION_RESULT_SCHEMA,
    recognition_evidence_from_alignment,
    sha256_file,
    validate_transcription_request,
)


_ARTIFACT_CONTRACTS = {
    "recognition_evidence.json": ("recognition_evidence", RECOGNITION_EVIDENCE_SCHEMA),
    "master_transcript.txt": ("recognition_transcript", None),
    "alignment.tsv": ("recognition_alignment_table", None),
    "segmentation_material.json": (
        "segmentation_material",
        SEGMENTATION_MATERIAL_SCHEMA,
    ),
    "audio_16k_mono.wav": ("prepared_audio", None),
    "recognition_request.json": ("recognition_request", TRANSCRIPTION_INPUT_SCHEMA),
    "run_manifest.json": ("recognition_manifest", "substar.run.v1"),
    "provider_submission_audit.json": (
        "provider_submission_audit",
        "substar.provider-submission-audit.v1",
    ),
    "asr_ingest_report.json": ("recognition_audit", "substar.asr-ingest-report.v1"),
    "provider_response.json": ("provider_response_private", None),
}
_PRIVATE_CONFIGURATION_FRAGMENTS = (
    "api_key",
    "secret",
    "token",
    "password",
    "credential",
    "path",
    "directory",
    "cache_dir",
)


class TranscriptionCancelled(InterruptedError):
    pass


def _configure_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _public_manifest_configuration(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    public: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).lower()
        if any(fragment in normalized for fragment in _PRIVATE_CONFIGURATION_FRAGMENTS):
            continue
        if item is None or isinstance(item, (str, int, float, bool)):
            public[str(key)] = item
    return public


def _seed_retry(
    source_directory: str | None,
    destination: Path,
    input_fingerprint: str,
    cancelled: threading.Event,
) -> None:
    if not source_directory:
        return
    source = Path(source_directory).resolve()
    try:
        snapshot = json.loads((source / "recognition_request.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if snapshot.get("input_fingerprint") != input_fingerprint:
        return
    if cancelled.is_set():
        raise TranscriptionCancelled()
    audio = source / "audio_16k_mono.wav"
    if audio.is_file() and audio.stat().st_size > 44:
        shutil.copy2(audio, destination / audio.name)
    checkpoint = source / "ingest_chunks"
    if checkpoint.is_dir():
        shutil.copytree(checkpoint, destination / checkpoint.name, dirs_exist_ok=True)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _local_submission_audit(
    request: dict[str, Any], settings: dict[str, Any]
) -> dict[str, Any]:
    profile_id = request["profile_id"]
    model = str(
        settings.get("whisper_model")
        or settings.get("parakeet_model")
        or settings.get("qwen_asr_model")
        or profile_id
    )
    return {
        "schema_version": "substar.provider-submission-audit.v1",
        "provider": "local_runtime",
        "model": model,
        "input_fingerprint": request["input_fingerprint"],
        "resumed_remote_task": False,
        "submitted_body_sha256": _canonical_sha256(
            {"profile_id": profile_id, "model": model}
        ),
        "public_body": {"profile_id": profile_id, "model": model},
        "compilation": {
            "requested_prompt_sha256": hashlib.sha256(
                request["prompt"].encode("utf-8")
            ).hexdigest(),
            "submitted_context_sha256": hashlib.sha256(b"").hexdigest(),
            "requested_prompt_characters": len(request["prompt"]),
            "hotwords_sha256": _canonical_sha256(
                {item["text"]: item["weight"] for item in request["hotwords"]}
            ),
            "submitted_vocabulary": {},
        },
    }


def run(command: WorkerCommand) -> int:
    if command.task_type is not WorkerTaskType.TRANSCRIPTION:
        raise ValueError("worker command task_type must be transcription")
    payload = command.input
    if not isinstance(payload, dict) or set(payload) != {
        "request", "resolved_media_path", "resume_artifact_directory"
    }:
        raise ValueError("transcription worker input is invalid")
    request = validate_transcription_request(payload["request"])
    media_path = Path(str(payload["resolved_media_path"])).resolve()
    artifact_directory = Path(command.paths.artifact_directory).resolve()
    artifact_directory.mkdir(parents=True, exist_ok=True)
    cancelled = threading.Event()
    sequence = 0
    last_progress = 0.0

    def emit(
        message_type: WorkerMessageType,
        data: dict[str, Any] | None = None,
        *,
        progress: float | None = None,
        step: str | None = None,
    ) -> None:
        nonlocal sequence
        sequence += 1
        sys.stdout.write(
            WorkerMessage(
                task_id=command.task_id,
                attempt=command.attempt,
                sequence=sequence,
                message_type=message_type,
                occurred_at=utc_now(),
                progress=progress,
                step=step,
                data=data or {},
            ).to_json_line()
        )
        sys.stdout.flush()

    def read_controls() -> None:
        for line in sys.stdin:
            try:
                control = parse_command_line(line)
            except Exception:
                continue
            if (
                isinstance(control, WorkerControl)
                and control.task_id == command.task_id
                and control.attempt == command.attempt
                and control.control_type in {WorkerControlType.CANCEL, WorkerControlType.SHUTDOWN}
            ):
                cancelled.set()
                return

    threading.Thread(target=read_controls, daemon=True).start()
    emit(WorkerMessageType.READY, {"worker": "transcription"})

    try:
        # Persist the non-secret request before any media/provider work so a
        # killed attempt can prove that its private checkpoint belongs to the
        # exact immutable input on retry.
        atomic_write_json(artifact_directory / "recognition_request.json", request)
        _seed_retry(
            payload.get("resume_artifact_directory"),
            artifact_directory,
            request["input_fingerprint"],
            cancelled,
        )
        # The scheduler grants only the named provider capability.  Pop the
        # temporary environment slot immediately so ffmpeg or other children
        # cannot inherit it; the worker never opens the application's secret
        # store and never receives unrelated role credentials.
        credentials: dict[str, str] = {}
        for reference in command.credential_refs:
            secret = os.environ.pop(credential_environment_key(reference), "").strip()
            if not secret:
                raise ValueError(f"worker credential is unavailable: {reference}")
            credentials[reference] = secret

        settings = dict(request["options"])
        settings["recognition_profile_id"] = request["profile_id"]
        settings["language"] = request["language"]
        settings["context"] = request["prompt"]
        settings["hotwords"] = {
            item["text"]: item["weight"] for item in request["hotwords"]
        }
        if request["profile_id"] == "qwen_cloud":
            settings["api_key"] = credentials.pop(ASR_QWEN, "")
        elif request["profile_id"] == "api":
            settings["api_key"] = credentials.pop(ASR_GENERIC, "")
        if credentials:
            raise ValueError("worker received an unused credential authority")
        settings["_cancel_requested"] = cancelled.is_set
        settings["_transcription_input_fingerprint"] = request["input_fingerprint"]

        def progress(message: str, fraction: float) -> None:
            nonlocal last_progress
            if cancelled.is_set():
                raise TranscriptionCancelled()
            value = max(0.0, min(1.0, float(fraction)))
            # Provider adapters may expose status transitions whose display
            # percentages are not naturally ordered. The worker protocol is
            # stricter: a task attempt is a single monotonic progress stream.
            # Drop a regressive presentation update at this boundary instead
            # of poisoning an otherwise valid, fully downloaded result.
            if value < last_progress:
                return
            last_progress = value
            if value < 0.08:
                step = "transcription.media_probe"
            elif value < 0.20:
                step = "transcription.audio_prepare"
            elif value < 0.26:
                step = "transcription.provider_audio_encode"
            elif value < 0.34:
                step = "transcription.provider_upload"
            elif value < 0.80:
                step = "transcription.provider_run"
            elif value < 0.98:
                step = "transcription.evidence_normalize"
            else:
                step = "transcription.artifact_finalize"
            emit(
                WorkerMessageType.PROGRESS,
                {"message": str(message)},
                progress=value,
                step=step,
            )

        recognition_source = run_cloud_pipeline(
            media_path, artifact_directory, settings, progress
        )
        if cancelled.is_set():
            raise TranscriptionCancelled()

        audit_path = artifact_directory / "provider_submission_audit.json"
        if not audit_path.is_file():
            atomic_write_json(
                audit_path, _local_submission_audit(request, settings)
            )
        response_source = artifact_directory / "ingest_chunks" / "qwen_cloud_result.json"
        response_path = artifact_directory / "provider_response.json"
        if response_source.is_file():
            shutil.copy2(response_source, response_path)

        provider_submission = {
            "relative_path": audit_path.name,
            "sha256": sha256_file(audit_path),
        }
        provider_response = (
            {
                "relative_path": response_path.name,
                "sha256": sha256_file(response_path),
            }
            if response_path.is_file()
            else None
        )

        evidence = recognition_evidence_from_alignment(
            recognition_source,
            request,
            provider_submission=provider_submission,
            provider_response=provider_response,
        )
        atomic_write_json(artifact_directory / "recognition_evidence.json", evidence)
        manifest_path = artifact_directory / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source_path"] = request["media"]["relative_path"]
        # The generic media pipeline records diagnostic configuration.  This
        # canonical artifact is public, so strip credentials and host-specific
        # paths before it is registered or copied into the project.
        manifest["configuration"] = _public_manifest_configuration(
            manifest.get("configuration")
        )
        manifest["recognition_evidence"] = {
            "schema_version": RECOGNITION_EVIDENCE_SCHEMA,
            "relative_path": "recognition_evidence.json",
            "input_fingerprint": request["input_fingerprint"],
        }
        atomic_write_json(manifest_path, manifest)

        artifact_rows: list[dict[str, Any]] = []
        for name, (artifact_type, schema_version) in _ARTIFACT_CONTRACTS.items():
            path = artifact_directory / name
            if not path.is_file():
                if name in {"asr_ingest_report.json", "provider_response.json"}:
                    continue
                raise ValueError(f"required transcription artifact is missing: {name}")
            row = {
                "artifact_type": artifact_type,
                "relative_path": name,
                "schema_version": schema_version,
                "sha256": sha256_file(path),
                "byte_size": path.stat().st_size,
            }
            artifact_rows.append(row)
            emit(WorkerMessageType.ARTIFACT, row)
        emit(
            WorkerMessageType.RESULT,
            {
                "schema_version": TRANSCRIPTION_RESULT_SCHEMA,
                "artifacts": artifact_rows,
                "summary": {
                    "profile_id": request["profile_id"],
                    "language": evidence["language"],
                    "duration_seconds": float(evidence["media"].get("duration_seconds", 0)),
                    "unit_count": len(evidence["units"]),
                    "sentence_count": len(evidence["chunks"]),
                    "media_sha256": evidence["media"]["sha256"],
                    "input_fingerprint": request["input_fingerprint"],
                },
            },
        )
        return 0
    except (TranscriptionCancelled, InterruptedError):
        emit(WorkerMessageType.CANCELLED, {"reason": "requested"})
        return 0
    except BaseException as exc:
        # Public runtime state receives only the stable code. Full diagnostics
        # remain on stderr in the attempt-owned private log.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        emit(WorkerMessageType.ERROR, {"code": "transcription_worker_failed"})
        return 1


def main() -> int:
    _configure_stdio()
    first = sys.stdin.readline()
    command = parse_command_line(first)
    if not isinstance(command, WorkerCommand):
        return 90
    return run(command)
