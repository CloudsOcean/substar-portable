from __future__ import annotations

import hashlib
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any, Mapping

from substar_core.editor.calibration.handler import (
    CALIBRATION_INPUT_SCHEMA,
    CALIBRATION_RESULT_SCHEMA,
)
from substar_core.runtime.worker_protocol import (
    WorkerCommand, WorkerMessage, WorkerMessageType, WorkerTaskType,
    credential_environment_key, parse_command_line, utc_now,
)


def _configure_stdio_utf8() -> None:
    """Keep the JSONL worker protocol independent of the Windows code page."""

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _active_progress_units(value: Mapping[str, Any]) -> tuple[int, int]:
    """Project the common AI progress contract onto Runtime's active counter."""

    units = value.get("units")
    units = units if isinstance(units, Mapping) else {}
    phase = str(value.get("phase") or "executing")
    if phase == "repair":
        return (
            int(units.get("repair_completed", 0) or 0),
            int(units.get("repair_planned", 0) or 0),
        )
    return (
        int(units.get("completed", 0) or 0),
        int(units.get("planned", 0) or 0),
    )


def run(command: WorkerCommand) -> int:
    if command.task_type is not WorkerTaskType.CALIBRATION or command.input_schema != CALIBRATION_INPUT_SCHEMA:
        return 90
    payload = dict(command.input)
    project_root = Path(command.paths.project_root or "").resolve()
    artifacts = Path(command.paths.artifact_directory).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    settings = dict(payload["settings"])
    credential = os.environ.pop(
        credential_environment_key(str(payload["credential_ref"])), ""
    ).strip()
    if not credential:
        raise ValueError("calibration provider credential is unavailable")
    settings["translation_api_key"] = credential
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

    emit(WorkerMessageType.READY, {"worker": "calibration"})
    try:
        # The worker protocol is the only lifecycle/progress authority.
        from substar_core.editor import http_api

        def project_progress(value: Any) -> None:
            value = dict(value or {})
            phase = str(value.get("phase") or "executing")
            completed, total = _active_progress_units(value)
            fraction = completed / total if total else 0.0
            progress_value = {
                "executing": 0.05 + 0.70 * fraction,
                "repair": 0.75 + 0.15 * fraction,
                "validating": 0.91,
                "materializing": 0.95,
                "publishing": 0.98,
                "completed": 1.0,
            }.get(phase, float(value.get("progress", 0.02)))
            emit(
                WorkerMessageType.PROGRESS,
                {
                    "phase": phase,
                    "completed": completed,
                    "total": total,
                    "ai_progress": value,
                },
                progress=min(1.0, progress_value),
                step=f"calibration.{phase}",
            )

        request = http_api.AiCalibrationRequest(
            expected_revision_id=str(payload["expected_revision_id"]),
            instruction=str(payload["instruction"]),
        )
        result = http_api._ai_calibrate_project(
            str(command.project_id),
            request,
            command.task_id,
            settings_snapshot=settings,
            progress_sink=project_progress,
        )

        project_artifacts = project_root / "calibration"
        contracts = {
            "latest.json": ("calibration_result", "substar.calibration-result.v2"),
            "audit.json": ("calibration_audit", "substar.calibration-audit.v2"),
        }
        rows = []
        for name, (artifact_type, schema_version) in contracts.items():
            source = project_artifacts / name
            destination = artifacts / name
            shutil.copy2(source, destination)
            row = {
                "artifact_type": artifact_type,
                "relative_path": name,
                "schema_version": schema_version,
                "sha256": _sha256(destination),
                "byte_size": destination.stat().st_size,
            }
            rows.append(row)
            emit(WorkerMessageType.ARTIFACT, row)
        revision = result["revision"]
        # Internal editor saves return the serialized revision payload while
        # storage APIs return a DocumentRevision.  Publish one canonical id to
        # the Runtime regardless of that representation boundary.
        revision_id = http_api._revision_id(revision)
        emit(WorkerMessageType.RESULT, {
            "schema_version": CALIBRATION_RESULT_SCHEMA,
            "artifacts": rows,
            "summary": {
                "result_revision_id": revision_id,
                "failed_blocks": list(result.get("failed_blocks") or []),
                "problem_cue_ids": list(result.get("problem_cue_ids") or []),
                "ai_progress": dict(result.get("ai_progress") or {}),
            },
        })
        return 0
    except BaseException as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        emit(WorkerMessageType.ERROR, {
            "code": "calibration_worker_failed",
            "public_message": str(exc)[:1600],
        })
        return 1


def main() -> int:
    _configure_stdio_utf8()
    command = parse_command_line(sys.stdin.readline())
    if not isinstance(command, WorkerCommand):
        return 90
    return run(command)
