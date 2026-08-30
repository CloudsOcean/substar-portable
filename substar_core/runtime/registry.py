from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping, Sequence

from .model import TASK_TYPES, InvalidTaskError
from .supervisor import WorkerCompletion
from .worker_protocol import WorkerMessage


@dataclass(frozen=True)
class TaskWorkContext:
    task: Mapping[str, Any]
    input_payload: Mapping[str, Any]
    attempt_directory: Path
    work_directory: Path
    artifact_directory: Path


@dataclass(frozen=True)
class WorkerLaunch:
    argv: tuple[str, ...]
    cwd: Path | None = None
    project_root: Path | None = None
    env: Mapping[str, str] | None = None
    timeout_seconds: float | None = None
    credential_refs: tuple[str, ...] = ()
    worker_input: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.argv or any(
            not isinstance(item, str) or not item for item in self.argv
        ):
            raise ValueError("worker argv must contain non-empty strings")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("worker timeout must be positive")
        if self.env is not None and any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            for key, value in self.env.items()
        ):
            raise ValueError("worker environment must contain non-empty text keys and text values")


PrepareHandler = Callable[[TaskWorkContext], WorkerLaunch]
FinalizeHandler = Callable[[TaskWorkContext, WorkerCompletion], Mapping[str, Any]]
ValidateInputHandler = Callable[[Mapping[str, Any]], Mapping[str, Any]]
WorkerEventHandler = Callable[[TaskWorkContext, WorkerMessage], Mapping[str, Any]]


def _default_validate_input(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise InvalidTaskError("task input must be a JSON object")
    return dict(payload)


def _default_worker_event(
    _context: TaskWorkContext, message: WorkerMessage
) -> Mapping[str, Any]:
    return {
        "progress": float(message.progress or 0.0),
        "message": None,
        "step": None,
        "wait_reason": None,
        "phase": None,
        "completed_units": None,
        "total_units": None,
    }


def _default_finalize(
    _context: TaskWorkContext, completion: WorkerCompletion
) -> Mapping[str, Any]:
    raise InvalidTaskError(
        "task handler must define a type-specific worker result validator"
    )


@dataclass(frozen=True)
class TaskHandler:
    task_type: str
    prepare: PrepareHandler
    validate_input: ValidateInputHandler = _default_validate_input
    handle_worker_event: WorkerEventHandler = _default_worker_event
    finalize: FinalizeHandler = _default_finalize
    resources: tuple[str, ...] = field(default_factory=lambda: ("worker",))

    def __post_init__(self) -> None:
        if self.task_type not in TASK_TYPES:
            raise InvalidTaskError(f"unknown task handler type: {self.task_type!r}")
        normalized = tuple(str(item).strip() for item in self.resources)
        if any(not item for item in normalized) or len(normalized) != len(
            set(normalized)
        ):
            raise InvalidTaskError("handler resources must be unique non-empty names")
        object.__setattr__(self, "resources", normalized)


class TaskRegistry:
    """Thread-safe registry for one production handler per canonical task type."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._handlers: dict[str, TaskHandler] = {}

    def register(self, handler: TaskHandler) -> None:
        if not isinstance(handler, TaskHandler):
            raise TypeError("handler must be a TaskHandler")
        with self._lock:
            if handler.task_type in self._handlers:
                raise InvalidTaskError(
                    f"task handler is already registered: {handler.task_type}"
                )
            self._handlers[handler.task_type] = handler

    def unregister(self, task_type: str) -> None:
        with self._lock:
            self._handlers.pop(str(task_type), None)

    def get(self, task_type: str) -> TaskHandler:
        with self._lock:
            handler = self._handlers.get(str(task_type))
        if handler is None:
            raise InvalidTaskError(f"task handler is not registered: {task_type!r}")
        return handler

    def task_types(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._handlers)

    def handlers(self) -> tuple[TaskHandler, ...]:
        with self._lock:
            return tuple(self._handlers[key] for key in sorted(self._handlers))


class ResourcePool:
    """All-or-nothing named resource claims owned by the scheduler thread."""

    def __init__(self, limits: Mapping[str, int] | None = None) -> None:
        configured = {"worker": 1, **dict(limits or {})}
        if any(isinstance(value, bool) or int(value) < 1 for value in configured.values()):
            raise ValueError("resource limits must be positive integers")
        self._limits = {str(key): int(value) for key, value in configured.items()}
        self._in_use = {key: 0 for key in self._limits}
        self._lock = RLock()

    def can_acquire(self, resources: Sequence[str]) -> bool:
        with self._lock:
            return all(
                resource in self._limits
                and self._in_use[resource] < self._limits[resource]
                for resource in resources
            )

    def acquire(self, resources: Sequence[str]) -> bool:
        with self._lock:
            if not self.can_acquire(resources):
                return False
            for resource in resources:
                self._in_use[resource] += 1
            return True

    def release(self, resources: Sequence[str]) -> None:
        with self._lock:
            for resource in resources:
                if resource not in self._in_use or self._in_use[resource] <= 0:
                    raise RuntimeError(f"resource was not acquired: {resource}")
                self._in_use[resource] -= 1

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {
                key: {"limit": self._limits[key], "in_use": self._in_use[key]}
                for key in sorted(self._limits)
            }
