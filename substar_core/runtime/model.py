from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


TASK_SCHEMA_VERSION = "substar.task.v1"
TASK_EVENT_SCHEMA_VERSION = "substar.task-event.v1"
API_ERROR_SCHEMA_VERSION = "substar.api-error.v1"
API_ERROR_CATEGORIES = frozenset(
    {
        "validation",
        "configuration",
        "authentication",
        "not_found",
        "conflict",
        "provider_unavailable",
        "provider_rate_limited",
        "provider_timeout",
        "media_invalid",
        "process_failed",
        "artifact_invalid",
        "revision_conflict",
        "cancel_failed",
        "internal",
    }
)


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TASK_TYPES = frozenset(
    {
        "transcription",
        "reference_matching",
        "segmentation",
        "translation",
        "calibration",
        "review",
        "model_download",
        "export",
        "dubbing",
    }
)

TERMINAL_STATES = frozenset({TaskState.SUCCEEDED, TaskState.CANCELLED})
ACTIVE_STATES = frozenset({TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLING})
RETRYABLE_STATES = frozenset({TaskState.FAILED, TaskState.INTERRUPTED})

ALLOWED_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.QUEUED: frozenset(
        {TaskState.RUNNING, TaskState.CANCELLING, TaskState.CANCELLED}
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLING,
            TaskState.INTERRUPTED,
        }
    ),
    TaskState.CANCELLING: frozenset(
        {
            TaskState.CANCELLED,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.INTERRUPTED,
        }
    ),
    TaskState.FAILED: frozenset({TaskState.QUEUED}),
    TaskState.INTERRUPTED: frozenset({TaskState.QUEUED}),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


EVENT_TYPES = frozenset(
    {
        "task.created",
        "task.started",
        "task.progress",
        "task.waiting",
        "task.cancel_requested",
        "task.cancelled",
        "task.succeeded",
        "task.failed",
        "task.interrupted",
        "task.artifact_registered",
        "project.created",
        "project.updated",
        "project.revision_created",
        "stream.reset_required",
    }
)


class TaskRuntimeError(RuntimeError):
    """Base class for stable task-runtime failures."""


class InvalidTaskError(TaskRuntimeError):
    pass


class TaskNotFoundError(TaskRuntimeError):
    pass


class TaskStateConflictError(TaskRuntimeError):
    pass


class TaskOwnershipError(TaskStateConflictError):
    pass


class IdempotencyConflictError(TaskRuntimeError):
    pass


class WriterProcessError(TaskRuntimeError):
    pass


def coerce_state(value: str | TaskState) -> TaskState:
    try:
        return value if isinstance(value, TaskState) else TaskState(str(value))
    except ValueError as exc:
        raise InvalidTaskError(f"unknown task state: {value!r}") from exc


def require_transition(source: str | TaskState, target: str | TaskState) -> None:
    source_state = coerce_state(source)
    target_state = coerce_state(target)
    if target_state not in ALLOWED_TRANSITIONS[source_state]:
        raise TaskStateConflictError(
            f"task cannot transition from {source_state.value} to {target_state.value}"
        )


def decode_json_object(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise InvalidTaskError("stored task JSON is not an object")
    return value


def canonical_api_error(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy the only error shape allowed in public task state."""

    if not isinstance(value, Mapping):
        raise InvalidTaskError("task error must be a JSON object")
    required = {
        "schema_version",
        "code",
        "category",
        "message",
        "retryable",
        "request_id",
        "details",
    }
    if set(value) != required:
        raise InvalidTaskError("task error does not match substar.api-error.v1")
    if value.get("schema_version") != API_ERROR_SCHEMA_VERSION:
        raise InvalidTaskError("task error schema_version is unsupported")
    for field_name in ("code", "message", "request_id"):
        if not isinstance(value.get(field_name), str) or not value[field_name]:
            raise InvalidTaskError(f"task error {field_name} must be non-empty text")
    if value.get("category") not in API_ERROR_CATEGORIES:
        raise InvalidTaskError("task error category is unsupported")
    if not isinstance(value.get("retryable"), bool):
        raise InvalidTaskError("task error retryable must be boolean")
    if not isinstance(value.get("details"), Mapping):
        raise InvalidTaskError("task error details must be a JSON object")
    return {
        "schema_version": API_ERROR_SCHEMA_VERSION,
        "code": value["code"],
        "category": value["category"],
        "message": value["message"],
        "retryable": value["retryable"],
        "request_id": value["request_id"],
        "details": dict(value["details"]),
    }


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    project_id: str | None
    parent_task_id: str | None
    task_type: str
    state: TaskState
    attempt: int
    progress: float
    progress_message: str | None
    step: str | None
    wait_reason: str | None
    input_schema: str
    input_payload: dict[str, Any]
    idempotency_key: str | None
    expected_revision_id: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    cancel_requested_at: str | None
    owner_instance_id: str | None
    lease_expires_at: str | None
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    row_version: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "TaskRecord":
        return cls(
            task_id=str(row["task_id"]),
            project_id=row["project_id"],
            parent_task_id=row["parent_task_id"],
            task_type=str(row["task_type"]),
            state=coerce_state(row["state"]),
            attempt=int(row["attempt"]),
            progress=float(row["progress"]),
            progress_message=row["progress_message"],
            step=row["step"],
            wait_reason=row["wait_reason"],
            input_schema=str(row["input_schema"]),
            input_payload=decode_json_object(row["input_json"]) or {},
            idempotency_key=row["idempotency_key"],
            expected_revision_id=row["expected_revision_id"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            cancel_requested_at=row["cancel_requested_at"],
            owner_instance_id=row["owner_instance_id"],
            lease_expires_at=row["lease_expires_at"],
            result=decode_json_object(row["result_json"]),
            error=decode_json_object(row["error_json"]),
            row_version=int(row["row_version"]),
        )

    def public(self) -> dict[str, Any]:
        retryable = self.state in RETRYABLE_STATES
        cancellable = self.state in ACTIVE_STATES
        project_link = f"/api/projects/{self.project_id}" if self.project_id else None
        return {
            "schema_version": TASK_SCHEMA_VERSION,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "parent_task_id": self.parent_task_id,
            "task_type": self.task_type,
            "state": self.state.value,
            "attempt": self.attempt,
            "progress": self.progress,
            "progress_message": self.progress_message,
            "step": self.step,
            "wait_reason": self.wait_reason,
            "input_schema": self.input_schema,
            "expected_revision_id": self.expected_revision_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cancel_requested_at": self.cancel_requested_at,
            "result": self.result,
            "error": self.error,
            "links": {
                "self": f"/api/tasks/{self.task_id}",
                "events": f"/api/tasks/{self.task_id}/events",
                "project": project_link,
                "cancel": (
                    f"/api/tasks/{self.task_id}/cancel" if cancellable else None
                ),
                "retry": f"/api/tasks/{self.task_id}/retry" if retryable else None,
            },
        }


@dataclass(frozen=True)
class TaskEvent:
    event_id: int
    task_id: str | None
    project_id: str | None
    attempt: int | None
    event_type: str
    occurred_at: str
    request_id: str | None
    data: dict[str, Any]

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "TaskEvent":
        return cls(
            event_id=int(row["event_id"]),
            task_id=row["task_id"],
            project_id=row["project_id"],
            attempt=int(row["attempt"]) if row["attempt"] is not None else None,
            event_type=str(row["event_type"]),
            occurred_at=str(row["occurred_at"]),
            request_id=row["request_id"],
            data=decode_json_object(row["payload_json"]) or {},
        )

    def public(self) -> dict[str, Any]:
        return {
            "schema_version": TASK_EVENT_SCHEMA_VERSION,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "attempt": self.attempt,
            "occurred_at": self.occurred_at,
            "request_id": self.request_id,
            "data": self.data,
        }


@dataclass(frozen=True)
class TaskArtifact:
    artifact_id: str
    task_id: str
    project_id: str | None
    attempt: int
    artifact_type: str
    schema_version: str | None
    relative_path: str
    sha256: str
    byte_size: int
    created_at: str
    metadata: dict[str, Any] | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "TaskArtifact":
        return cls(
            artifact_id=str(row["artifact_id"]),
            task_id=str(row["task_id"]),
            project_id=row["project_id"],
            attempt=int(row["attempt"]),
            artifact_type=str(row["artifact_type"]),
            schema_version=row["schema_version"],
            relative_path=str(row["relative_path"]),
            sha256=str(row["sha256"]),
            byte_size=int(row["byte_size"]),
            created_at=str(row["created_at"]),
            metadata=decode_json_object(row["metadata_json"]),
        )

    def public(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "attempt": self.attempt,
            "artifact_type": self.artifact_type,
            "schema_version": self.schema_version,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }
