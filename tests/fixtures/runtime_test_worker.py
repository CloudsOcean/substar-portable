from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from substar_core.runtime.worker_protocol import (  # noqa: E402
    WorkerCommand,
    WorkerControl,
    WorkerControlType,
    WorkerMessage,
    WorkerMessageType,
    credential_environment_key,
    parse_command_line,
    utc_now,
)


def main() -> int:
    first = sys.stdin.readline()
    command = parse_command_line(first)
    if not isinstance(command, WorkerCommand):
        return 90
    sequence = 0

    def emit(
        message_type: WorkerMessageType,
        data: dict[str, object] | None = None,
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

    mode = str(command.input.get("mode", "success"))
    emit(WorkerMessageType.READY, {"pid": 1})
    if mode == "success":
        print("fixture diagnostic", file=sys.stderr, flush=True)
        emit(
            WorkerMessageType.PROGRESS,
            {"message": "halfway"},
            progress=0.5,
            step="fixture.work",
        )
        emit(WorkerMessageType.RESULT, {"value": 42})
        return 0
    if mode in {
        "artifact_success",
        "artifact_bad_digest",
        "artifact_mutated",
        "artifact_then_error",
        "artifact_result_without_event",
    }:
        artifact_directory = Path(command.paths.artifact_directory)
        artifact_directory.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_directory / "result.json"
        artifact_path.write_bytes(b'{"value":42}\n')
        digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        artifact_row = {
            "artifact_type": "test_result",
            "relative_path": "result.json",
            "sha256": ("0" * 64 if mode == "artifact_bad_digest" else digest),
            "byte_size": artifact_path.stat().st_size,
            "schema_version": "substar.test-result.v1",
        }
        if mode != "artifact_result_without_event":
            emit(WorkerMessageType.ARTIFACT, artifact_row)
        if mode == "artifact_mutated":
            time.sleep(0.2)
            artifact_path.write_bytes(b'{"value":"changed-after-registration"}\n')
        if mode == "artifact_then_error":
            emit(
                WorkerMessageType.ERROR,
                {
                    "code": "fixture_failure_after_artifact",
                    "message": "fixture failed after publishing a valid artifact",
                },
            )
            return 1
        emit(
            WorkerMessageType.RESULT,
            (
                {"value": 42, "artifacts": [artifact_row]}
                if mode == "artifact_result_without_event"
                else {"value": 42}
            ),
        )
        return 0
    if mode == "credential_scope":
        emit(
            WorkerMessageType.RESULT,
            {
                "granted": bool(
                    os.environ.get(credential_environment_key("qwen_cloud"))
                ),
                "unrelated_present": bool(
                    os.environ.get(credential_environment_key("sentence"))
                ),
            },
        )
        return 0
    if mode == "fail":
        print("fixture failed", file=sys.stderr, flush=True)
        emit(WorkerMessageType.ERROR, {"code": "fixture_failure"})
        return 7
    if mode == "fail_sensitive":
        emit(
            WorkerMessageType.ERROR,
            {
                "code": "provider_failed_secret_token",
                "message": "C:/private/path api_key=must-not-be-public",
            },
        )
        return 7
    if mode == "fail_public":
        emit(
            WorkerMessageType.ERROR,
            {
                "code": "algorithm_failed",
                "public_message": "error: unrecognized arguments: segmentation_material.md",
            },
        )
        return 7
    if mode == "invalid_stdout":
        print("this is not JSON", flush=True)
        emit(WorkerMessageType.RESULT, {"value": "invalid"})
        return 0
    if mode == "wait_cancel":
        for line in sys.stdin:
            control = parse_command_line(line)
            if (
                isinstance(control, WorkerControl)
                and control.control_type is WorkerControlType.CANCEL
            ):
                emit(WorkerMessageType.CANCELLED, {"reason": "cooperative"})
                return 0
        return 91
    if mode == "ignore_cancel":
        time.sleep(60)
        emit(WorkerMessageType.RESULT, {"unexpected": True})
        return 0
    if mode == "spawn_descendant":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
        )
        emit(WorkerMessageType.RESULT, {"child_pid": child.pid})
        return 0
    if mode == "spawn_descendant_redirected":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        emit(WorkerMessageType.RESULT, {"child_pid": child.pid})
        return 0
    emit(WorkerMessageType.ERROR, {"code": "unknown_fixture_mode"})
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
