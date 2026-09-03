from __future__ import annotations

import hashlib
import os
import sys
import threading
from pathlib import Path
from typing import Any

from substar_core.editor.translation.artifacts import (
    TRANSLATION_INPUT_SCHEMA,
    TRANSLATION_MANIFEST_FILENAME,
    TRANSLATION_PROGRESS_FILENAME,
    TRANSLATION_PROGRESS_SCHEMA,
    TRANSLATION_REVISION_FILENAME,
    TRANSLATION_SUBTITLE_FILENAME,
)
from substar_core.editor.translation.runner import execute_translation
from substar_core.runtime.worker_protocol import (
    WorkerCommand,
    WorkerMessage,
    WorkerMessageType,
    WorkerTaskType,
    credential_environment_key,
    parse_command_line,
    utc_now,
)


def _configure_stdio_utf8() -> None:
    """Keep the JSONL worker protocol independent of the Windows code page."""

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command: WorkerCommand) -> int:
    if command.task_type is not WorkerTaskType.TRANSLATION:
        return 90
    payload = dict(command.input)
    if command.input_schema != TRANSLATION_INPUT_SCHEMA:
        raise ValueError("unsupported translation input schema")
    project_root = Path(command.paths.project_root or "").resolve()
    artifact_directory = Path(command.paths.artifact_directory).resolve()
    settings = dict(payload["settings"])
    credential_ref = str(payload["credential_ref"])
    api_key = os.environ.pop(credential_environment_key(credential_ref), "").strip()
    if not api_key:
        raise ValueError("translation provider credential is unavailable")
    settings["translation_api_key"] = api_key
    sequence = 0
    lock = threading.Lock()

    def emit(kind: WorkerMessageType, data: dict[str, Any], *, progress=None, step=None):
        nonlocal sequence
        with lock:
            sequence += 1
            sys.stdout.write(WorkerMessage(
                task_id=command.task_id,
                attempt=command.attempt,
                sequence=sequence,
                message_type=kind,
                occurred_at=utc_now(),
                progress=progress,
                step=step,
                data=data,
            ).to_json_line())
            sys.stdout.flush()

    emit(WorkerMessageType.READY, {"worker": "translation"})
    try:
        def progress(ai_progress: dict[str, Any]):
            phase = str(ai_progress.get("phase") or "executing")
            units = dict(ai_progress.get("units") or {})
            active_completed = int(
                units.get("repair_completed" if phase == "repair" else "completed", 0) or 0
            )
            active_total = int(
                units.get("repair_planned" if phase == "repair" else "planned", 0) or 0
            )
            fraction = (active_completed / active_total) if active_total else 0.0
            base = {
                "planning": 0.02,
                "executing": 0.05 + 0.70 * fraction,
                "repair": 0.75 + 0.15 * fraction,
                "validating": 0.91,
                "materializing": 0.95,
                "publishing": 0.98,
                "completed": 1.0,
            }.get(phase, 0.02)
            emit(
                WorkerMessageType.PROGRESS,
                {
                    "phase": phase,
                    "completed": active_completed,
                    "total": active_total,
                    "ai_progress": ai_progress,
                },
                progress=min(1.0, base),
                step=f"translation.{phase}",
            )

        summary = execute_translation(
            project_root=project_root,
            artifact_directory=artifact_directory,
            expected_revision_id=str(payload["expected_revision_id"]),
            settings=settings,
            progress_callback=progress,
        )
        contracts = {
            TRANSLATION_PROGRESS_FILENAME: ("translation_progress", TRANSLATION_PROGRESS_SCHEMA),
            TRANSLATION_REVISION_FILENAME: ("translation_revision", "substar.translation-revision.v2"),
            TRANSLATION_SUBTITLE_FILENAME: ("translation_subtitle", "substar.srt.v1"),
            TRANSLATION_MANIFEST_FILENAME: ("translation_manifest", "substar.translation-manifest.v2"),
            "result.json": ("translation_result", "substar.translation-result.v2"),
        }
        artifacts = []
        for name, (artifact_type, schema_version) in contracts.items():
            path = artifact_directory / name
            row = {
                "artifact_type": artifact_type,
                "relative_path": name,
                "schema_version": schema_version,
                "sha256": _sha256(path),
                "byte_size": path.stat().st_size,
            }
            artifacts.append(row)
            emit(WorkerMessageType.ARTIFACT, row)
        emit(WorkerMessageType.RESULT, {
            "schema_version": "substar.translation-result.v2",
            "artifacts": artifacts,
            "summary": summary,
        })
        return 0
    except BaseException as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        emit(WorkerMessageType.ERROR, {
            "code": "translation_worker_failed",
            "public_message": str(exc)[:1600],
        })
        return 1


def main() -> int:
    _configure_stdio_utf8()
    command = parse_command_line(sys.stdin.readline())
    if not isinstance(command, WorkerCommand):
        return 90
    return run(command)
