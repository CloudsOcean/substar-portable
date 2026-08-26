from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


WORKER_COMMAND_SCHEMA = "substar.worker-command.v1"
WORKER_MESSAGE_SCHEMA = "substar.worker-message.v1"
WORKER_CONTROL_SCHEMA = "substar.worker-control.v1"
# The frozen contract is strict JSON Lines without a textual prefix.
CONTROL_PREFIX = ""
_CREDENTIAL_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CREDENTIAL_ENV_PREFIX = "SUBSTAR_WORKER_CREDENTIAL_"


class WorkerProtocolError(ValueError):
    """Raised when a worker record violates the frozen wire contract."""


class WorkerTaskType(str, Enum):
    TRANSCRIPTION = "transcription"
    REFERENCE_MATCHING = "reference_matching"
    SEGMENTATION = "segmentation"
    TRANSLATION = "translation"
    CALIBRATION = "calibration"
    REVIEW = "review"
    MODEL_DOWNLOAD = "model_download"
    EXPORT = "export"
    DUBBING = "dubbing"


class WorkerMessageType(str, Enum):
    READY = "ready"
    PROGRESS = "progress"
    NOTICE = "notice"
    ARTIFACT = "artifact"
    RESULT = "result"
    ERROR = "error"
    CANCELLED = "cancelled"


class WorkerControlType(str, Enum):
    CANCEL = "cancel"
    SHUTDOWN = "shutdown"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _mapping(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkerProtocolError(f"{field_name} must be a JSON object")
    return {str(key): child for key, child in value.items()}


def _text(
    value: object,
    field_name: str,
    *,
    nullable: bool = False,
    min_length: int = 1,
) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise WorkerProtocolError(f"{field_name} must be text")
    if len(value) < min_length:
        raise WorkerProtocolError(
            f"{field_name} must contain at least {min_length} character(s)"
        )
    return value


def _positive_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WorkerProtocolError(f"{field_name} must be an integer >= 1")
    return value


def _date_time(value: object, field_name: str, *, nullable: bool = False) -> str | None:
    rendered = _text(value, field_name, nullable=nullable)
    if rendered is None:
        return None
    candidate = rendered[:-1] + "+00:00" if rendered.endswith("Z") else rendered
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise WorkerProtocolError(f"{field_name} must be an ISO 8601 date-time") from exc
    if parsed.tzinfo is None:
        raise WorkerProtocolError(f"{field_name} must include a timezone")
    return rendered


def _json_object(line: str) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except (TypeError, json.JSONDecodeError) as exc:
        raise WorkerProtocolError(f"invalid worker JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkerProtocolError("worker JSON record must be an object")
    return value


def _reject_extra(value: Mapping[str, Any], allowed: set[str]) -> None:
    extras = set(value) - allowed
    if extras:
        raise WorkerProtocolError(
            "unsupported worker fields: " + ", ".join(sorted(extras))
        )


def credential_environment_key(reference: str) -> str:
    """Return the private, process-local environment slot for one reference."""

    rendered = str(reference)
    if _CREDENTIAL_REF.fullmatch(rendered) is None:
        raise WorkerProtocolError("credential reference is invalid")
    encoded = "".join(
        character.upper() if character.isalnum() else "_"
        for character in rendered
    )
    return _CREDENTIAL_ENV_PREFIX + encoded


@dataclass(frozen=True)
class WorkerPaths:
    work_directory: str
    artifact_directory: str
    project_root: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "work_directory", _text(self.work_directory, "work_directory")
        )
        object.__setattr__(
            self,
            "artifact_directory",
            _text(self.artifact_directory, "artifact_directory"),
        )
        object.__setattr__(
            self,
            "project_root",
            _text(self.project_root, "project_root", nullable=True, min_length=0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_root": self.project_root,
            "work_directory": self.work_directory,
            "artifact_directory": self.artifact_directory,
        }

    @classmethod
    def from_dict(cls, value: object) -> "WorkerPaths":
        raw = _mapping(value, "paths")
        _reject_extra(raw, {"project_root", "work_directory", "artifact_directory"})
        try:
            return cls(
                project_root=raw.get("project_root"),
                work_directory=raw["work_directory"],
                artifact_directory=raw["artifact_directory"],
            )
        except KeyError as exc:
            raise WorkerProtocolError(f"paths missing field: {exc.args[0]}") from exc


@dataclass(frozen=True)
class WorkerCommand:
    task_id: str
    attempt: int
    task_type: WorkerTaskType
    input_schema: str
    input: Mapping[str, Any]
    paths: WorkerPaths
    credential_refs: tuple[str, ...] = ()
    project_id: str | None = None
    deadline_at: str | None = None
    trace_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        object.__setattr__(self, "attempt", _positive_integer(self.attempt, "attempt"))
        try:
            task_type = WorkerTaskType(self.task_type)
        except (TypeError, ValueError) as exc:
            raise WorkerProtocolError("unknown task_type") from exc
        object.__setattr__(self, "task_type", task_type)
        object.__setattr__(
            self, "input_schema", _text(self.input_schema, "input_schema")
        )
        object.__setattr__(self, "input", _mapping(self.input, "input"))
        if not isinstance(self.paths, WorkerPaths):
            object.__setattr__(self, "paths", WorkerPaths.from_dict(self.paths))
        refs = tuple(_text(item, "credential_refs item") for item in self.credential_refs)
        if len(refs) != len(set(refs)):
            raise WorkerProtocolError("credential_refs must contain unique values")
        for reference in refs:
            credential_environment_key(reference)
        object.__setattr__(self, "credential_refs", refs)
        object.__setattr__(
            self,
            "project_id",
            _text(self.project_id, "project_id", nullable=True, min_length=0),
        )
        object.__setattr__(
            self,
            "deadline_at",
            _date_time(self.deadline_at, "deadline_at", nullable=True),
        )
        object.__setattr__(
            self, "trace_context", _mapping(self.trace_context, "trace_context")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": WORKER_COMMAND_SCHEMA,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "task_type": self.task_type.value,
            "project_id": self.project_id,
            "input_schema": self.input_schema,
            "input": dict(self.input),
            "paths": self.paths.to_dict(),
            "credential_refs": list(self.credential_refs),
            "deadline_at": self.deadline_at,
            "trace_context": dict(self.trace_context),
        }

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkerCommand":
        allowed = {
            "schema_version", "task_id", "attempt", "task_type", "project_id",
            "input_schema", "input", "paths", "credential_refs", "deadline_at",
            "trace_context",
        }
        _reject_extra(value, allowed)
        if value.get("schema_version") != WORKER_COMMAND_SCHEMA:
            raise WorkerProtocolError("unsupported worker command schema_version")
        required = {
            "task_id", "attempt", "task_type", "input_schema", "input", "paths",
            "credential_refs",
        }
        missing = required - set(value)
        if missing:
            raise WorkerProtocolError("worker command missing: " + ", ".join(sorted(missing)))
        refs = value["credential_refs"]
        if not isinstance(refs, list):
            raise WorkerProtocolError("credential_refs must be an array")
        try:
            task_type = WorkerTaskType(value["task_type"])
        except (TypeError, ValueError) as exc:
            raise WorkerProtocolError("unknown task_type") from exc
        return cls(
            task_id=value["task_id"],
            attempt=_positive_integer(value["attempt"], "attempt"),
            task_type=task_type,
            project_id=value.get("project_id"),
            input_schema=value["input_schema"],
            input=_mapping(value["input"], "input"),
            paths=WorkerPaths.from_dict(value["paths"]),
            credential_refs=tuple(refs),
            deadline_at=value.get("deadline_at"),
            trace_context=(
                _mapping(value["trace_context"], "trace_context")
                if "trace_context" in value
                else {}
            ),
        )

    @classmethod
    def from_json_line(cls, line: str) -> "WorkerCommand":
        return cls.from_dict(_json_object(line.strip()))


@dataclass(frozen=True)
class WorkerControl:
    task_id: str
    attempt: int
    control_type: WorkerControlType
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        object.__setattr__(self, "attempt", _positive_integer(self.attempt, "attempt"))
        try:
            control_type = WorkerControlType(self.control_type)
        except (TypeError, ValueError) as exc:
            raise WorkerProtocolError("unknown control_type") from exc
        object.__setattr__(self, "control_type", control_type)
        object.__setattr__(self, "data", _mapping(self.data, "data"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": WORKER_CONTROL_SCHEMA,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "control_type": self.control_type.value,
            "data": dict(self.data),
        }

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkerControl":
        _reject_extra(value, {"schema_version", "task_id", "attempt", "control_type", "data"})
        if value.get("schema_version") != WORKER_CONTROL_SCHEMA:
            raise WorkerProtocolError("unsupported worker control schema_version")
        try:
            control_type = WorkerControlType(value["control_type"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkerProtocolError("unknown or missing control_type") from exc
        return cls(
            task_id=value.get("task_id"),
            attempt=_positive_integer(value.get("attempt"), "attempt"),
            control_type=control_type,
            data=_mapping(value["data"], "data") if "data" in value else {},
        )

    @classmethod
    def from_json_line(cls, line: str) -> "WorkerControl":
        return cls.from_dict(_json_object(line.strip()))


@dataclass(frozen=True)
class WorkerMessage:
    task_id: str
    attempt: int
    sequence: int
    message_type: WorkerMessageType
    occurred_at: str
    data: Mapping[str, Any]
    progress: float | None = None
    step: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _text(self.task_id, "task_id"))
        object.__setattr__(self, "attempt", _positive_integer(self.attempt, "attempt"))
        object.__setattr__(self, "sequence", _positive_integer(self.sequence, "sequence"))
        try:
            message_type = WorkerMessageType(self.message_type)
        except (TypeError, ValueError) as exc:
            raise WorkerProtocolError("unknown message_type") from exc
        object.__setattr__(self, "message_type", message_type)
        object.__setattr__(self, "occurred_at", _date_time(self.occurred_at, "occurred_at"))
        object.__setattr__(self, "data", _mapping(self.data, "data"))
        if self.progress is not None:
            if isinstance(self.progress, bool) or not isinstance(self.progress, (int, float)):
                raise WorkerProtocolError("progress must be a number or null")
            if not 0 <= float(self.progress) <= 1:
                raise WorkerProtocolError("progress must be between 0 and 1")
            object.__setattr__(self, "progress", float(self.progress))
        object.__setattr__(
            self,
            "step",
            _text(self.step, "step", nullable=True, min_length=0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": WORKER_MESSAGE_SCHEMA,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "sequence": self.sequence,
            "message_type": self.message_type.value,
            "occurred_at": self.occurred_at,
            "progress": self.progress,
            "step": self.step,
            "data": dict(self.data),
        }

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkerMessage":
        allowed = {
            "schema_version", "task_id", "attempt", "sequence", "message_type",
            "occurred_at", "progress", "step", "data",
        }
        _reject_extra(value, allowed)
        if value.get("schema_version") != WORKER_MESSAGE_SCHEMA:
            raise WorkerProtocolError("unsupported worker message schema_version")
        required = {
            "task_id", "attempt", "sequence", "message_type", "occurred_at", "data"
        }
        missing = required - set(value)
        if missing:
            raise WorkerProtocolError("worker message missing: " + ", ".join(sorted(missing)))
        try:
            message_type = WorkerMessageType(value["message_type"])
        except (TypeError, ValueError) as exc:
            raise WorkerProtocolError("unknown message_type") from exc
        return cls(
            task_id=value["task_id"],
            attempt=_positive_integer(value["attempt"], "attempt"),
            sequence=_positive_integer(value["sequence"], "sequence"),
            message_type=message_type,
            occurred_at=value["occurred_at"],
            progress=value.get("progress"),
            step=value.get("step"),
            data=_mapping(value["data"], "data"),
        )

    @classmethod
    def from_json_line(cls, line: str) -> "WorkerMessage":
        return cls.from_dict(_json_object(line.strip()))


def parse_command_line(line: str) -> WorkerCommand | WorkerControl:
    value = _json_object(line.strip())
    if value.get("schema_version") == WORKER_COMMAND_SCHEMA:
        return WorkerCommand.from_dict(value)
    if value.get("schema_version") == WORKER_CONTROL_SCHEMA:
        return WorkerControl.from_dict(value)
    raise WorkerProtocolError("unsupported worker stdin schema_version")


def parse_message_line(line: str) -> WorkerMessage:
    if not line.strip():
        raise WorkerProtocolError("worker stdout line must contain one JSON object")
    return WorkerMessage.from_json_line(line)
