from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from substar_core.runtime import RuntimeStore, TaskService
from substar_core.runtime.worker_protocol import (
    WorkerCommand,
    WorkerMessage,
    WorkerMessageType,
    WorkerPaths,
    WorkerTaskType,
    utc_now,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "architecture" / "target" / "contracts"


def schema(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def validate(instance: dict, contract: dict, *dependencies: dict) -> None:
    registry = Registry()
    for dependency in dependencies:
        registry = registry.with_resource(
            dependency["$id"], Resource.from_contents(dependency)
        )
    Draft202012Validator(
        contract,
        registry=registry,
        format_checker=FormatChecker(),
    ).validate(instance)


class RuntimeContractSchemaTests(unittest.TestCase):
    def test_runtime_task_and_event_match_the_frozen_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = TaskService(
                RuntimeStore(Path(temporary) / "runtime.sqlite3"), "schema-instance"
            )
            task = service.create_task(
                task_type="export",
                input_schema="substar.export-input.v1",
                input_payload={"track": "source"},
                project_id="project-1",
                request_id="req-schema",
            )
            event = service.events(task_id=task["task_id"])[0]

        validate(task, schema("task.schema.json"), schema("api-error.schema.json"))
        validate(event, schema("task-event.schema.json"))

    def test_worker_records_match_the_frozen_contracts(self) -> None:
        command = WorkerCommand(
            task_id="task-1",
            attempt=1,
            task_type=WorkerTaskType.EXPORT,
            project_id="project-1",
            input_schema="substar.export-input.v1",
            input={"track": "source"},
            paths=WorkerPaths(
                project_root="project",
                work_directory="work",
                artifact_directory="artifacts",
            ),
            credential_refs=(),
        )
        message = WorkerMessage(
            task_id="task-1",
            attempt=1,
            sequence=1,
            message_type=WorkerMessageType.PROGRESS,
            occurred_at=utc_now(),
            progress=0.5,
            step="export.render",
            data={"message": "Rendering"},
        )

        validate(command.to_dict(), schema("worker-command.schema.json"))
        validate(message.to_dict(), schema("worker-message.schema.json"))


if __name__ == "__main__":
    unittest.main()
