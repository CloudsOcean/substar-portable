from __future__ import annotations

import re
import uuid
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from .model import (
    TASK_TYPES,
    InvalidTaskError,
    TaskArtifact,
    TaskRecord,
    TaskState,
    canonical_api_error,
)
from .store import RuntimeStore


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_FIELDS = frozenset(
    {
        "apikey",
        "authorization",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "password",
        "secret",
        "token",
    }
)


def _reject_inline_secrets(value: Any, field: str = "task payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = (
                str(key)
                .strip()
                .casefold()
                .replace("-", "")
                .replace("_", "")
            )
            if normalized in _SECRET_FIELDS:
                raise InvalidTaskError(
                    f"{field} cannot contain inline secret field {key!r}; use credential_refs"
                )
            _reject_inline_secrets(child, field)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_inline_secrets(child, field)


def _optional_id(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized or not _SAFE_ID.fullmatch(normalized):
        raise InvalidTaskError(f"{field} is invalid")
    return normalized


def _required_text(value: str, field: str, maximum: int = 255) -> str:
    normalized = str(value).strip()
    if not normalized or len(normalized) > maximum:
        raise InvalidTaskError(f"{field} is invalid")
    return normalized


class TaskService:
    """Application-facing durable task commands for one API instance."""

    def __init__(self, store: RuntimeStore, instance_id: str):
        self.store = store
        self.instance_id = _required_text(instance_id, "instance_id")

    @staticmethod
    def _public(task: TaskRecord) -> dict[str, Any]:
        return task.public()

    def create_task(
        self,
        task_type: str,
        input_schema: str,
        input_payload: Mapping[str, Any],
        project_id: str | None = None,
        parent_task_id: str | None = None,
        idempotency_key: str | None = None,
        expected_revision_id: str | None = None,
        *,
        request_id: str | None = None,
        depends_on_task_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        normalized_type = str(task_type).strip()
        if normalized_type not in TASK_TYPES:
            raise InvalidTaskError(f"unknown task type: {task_type!r}")
        normalized_schema = _required_text(input_schema, "input_schema")
        if not isinstance(input_payload, Mapping):
            raise InvalidTaskError("input_payload must be a JSON object")
        _reject_inline_secrets(input_payload)
        normalized_project = _optional_id(project_id, "project_id")
        normalized_parent = _optional_id(parent_task_id, "parent_task_id")
        normalized_revision = _optional_id(
            expected_revision_id, "expected_revision_id"
        )
        normalized_key: str | None = None
        if idempotency_key is not None:
            normalized_key = _required_text(
                idempotency_key, "idempotency_key", maximum=256
            )
        if isinstance(depends_on_task_ids, (str, bytes, Mapping)):
            raise InvalidTaskError("depends_on_task_ids must be an array")
        try:
            dependency_values = tuple(depends_on_task_ids)
        except TypeError as exc:
            raise InvalidTaskError("depends_on_task_ids must be an array") from exc
        normalized_dependencies = tuple(
            dict.fromkeys(
                _required_text(value, "depends_on_task_id")
                for value in dependency_values
            )
        )
        task, _created = self.store.create_task(
            task_id=f"tsk_{uuid.uuid4().hex}",
            task_type=normalized_type,
            input_schema=normalized_schema,
            input_payload=input_payload,
            project_id=normalized_project,
            parent_task_id=normalized_parent,
            idempotency_key=normalized_key,
            expected_revision_id=normalized_revision,
            request_id=(
                _required_text(request_id, "request_id")
                if request_id is not None
                else None
            ),
            depends_on_task_ids=normalized_dependencies,
        )
        return self._public(task)

    def find_idempotent_task(
        self,
        task_type: str,
        input_schema: str,
        input_payload: Mapping[str, Any],
        project_id: str | None,
        parent_task_id: str | None,
        idempotency_key: str,
        expected_revision_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return an exact prior request before checking runtime availability."""

        normalized_type = str(task_type).strip()
        if normalized_type not in TASK_TYPES:
            raise InvalidTaskError(f"unknown task type: {task_type!r}")
        normalized_schema = _required_text(input_schema, "input_schema")
        if not isinstance(input_payload, Mapping):
            raise InvalidTaskError("input_payload must be a JSON object")
        _reject_inline_secrets(input_payload)
        normalized_key = _required_text(
            idempotency_key, "idempotency_key", maximum=256
        )
        task = self.store.find_idempotent_task(
            task_type=normalized_type,
            input_schema=normalized_schema,
            input_payload=input_payload,
            project_id=_optional_id(project_id, "project_id"),
            parent_task_id=_optional_id(parent_task_id, "parent_task_id"),
            idempotency_key=normalized_key,
            expected_revision_id=_optional_id(
                expected_revision_id, "expected_revision_id"
            ),
        )
        return self._public(task) if task is not None else None

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self._public(self.store.get_task(_required_text(task_id, "task_id")))

    def get_task_input(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(_required_text(task_id, "task_id"))
        return dict(task.input_payload)

    def list_tasks(
        self,
        *,
        project_id: str | None = None,
        task_type: str | None = None,
        states: Iterable[str | TaskState] | None = None,
        parent_task_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= 500:
            raise InvalidTaskError("limit must be between 1 and 500")
        normalized_type = None
        if task_type is not None:
            normalized_type = str(task_type).strip()
            if normalized_type not in TASK_TYPES:
                raise InvalidTaskError(f"unknown task type: {task_type!r}")
        return [
            self._public(task)
            for task in self.store.list_tasks(
                project_id=_optional_id(project_id, "project_id"),
                task_type=normalized_type,
                states=states,
                parent_task_id=_optional_id(parent_task_id, "parent_task_id"),
                limit=int(limit),
            )
        ]

    def add_dependency(
        self, task_id: str, depends_on_task_id: str, condition: str = "succeeded"
    ) -> None:
        self.store.add_dependency(
            _required_text(task_id, "task_id"),
            _required_text(depends_on_task_id, "depends_on_task_id"),
            condition,
        )

    def claim_next(
        self,
        allowed_task_types: Iterable[str],
        lease_seconds: float = 30.0,
        *,
        worker_id: str | None = None,
        worker_pid: int | None = None,
        work_directory: str = "",
        stdout_log: str = "",
        stderr_log: str = "",
    ) -> dict[str, Any] | None:
        normalized = set(str(item).strip() for item in allowed_task_types)
        unknown = normalized - TASK_TYPES
        if unknown:
            raise InvalidTaskError(f"unknown task types: {sorted(unknown)}")
        task = self.store.claim_next(
            normalized,
            self.instance_id,
            lease_seconds,
            worker_id=worker_id,
            worker_pid=worker_pid,
            work_directory=work_directory,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
        )
        return self._public(task) if task is not None else None

    def heartbeat(
        self, task_id: str, attempt: int, lease_seconds: float = 30.0
    ) -> dict[str, Any]:
        return self._public(
            self.store.heartbeat(
                _required_text(task_id, "task_id"),
                int(attempt),
                self.instance_id,
                lease_seconds,
            )
        )

    def attach_worker(
        self,
        task_id: str,
        attempt: int,
        *,
        worker_id: str,
        worker_pid: int,
        work_directory: str = "",
        stdout_log: str = "",
        stderr_log: str = "",
    ) -> dict[str, Any]:
        if isinstance(worker_pid, bool) or int(worker_pid) <= 0:
            raise InvalidTaskError("worker_pid must be a positive integer")
        return self._public(
            self.store.attach_worker(
                _required_text(task_id, "task_id"),
                int(attempt),
                self.instance_id,
                worker_id=_required_text(worker_id, "worker_id"),
                worker_pid=int(worker_pid),
                work_directory=str(work_directory),
                stdout_log=str(stdout_log),
                stderr_log=str(stderr_log),
            )
        )

    def record_progress(
        self,
        task_id: str,
        attempt: int,
        progress: float,
        *,
        message: str | None = None,
        step: str | None = None,
        wait_reason: str | None = None,
        phase: str | None = None,
        completed_units: int | None = None,
        total_units: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._public(
            self.store.record_progress(
                _required_text(task_id, "task_id"),
                int(attempt),
                self.instance_id,
                float(progress),
                message=message,
                step=step,
                wait_reason=wait_reason,
                phase=phase,
                completed_units=completed_units,
                total_units=total_units,
                request_id=(
                    _required_text(request_id, "request_id")
                    if request_id is not None
                    else None
                ),
            )
        )

    def request_cancel(
        self, task_id: str, *, request_id: str | None = None
    ) -> dict[str, Any]:
        return self._public(
            self.store.request_cancel(
                _required_text(task_id, "task_id"),
                request_id=(
                    _required_text(request_id, "request_id")
                    if request_id is not None
                    else None
                ),
            )
        )

    def retry(
        self, task_id: str, *, request_id: str | None = None
    ) -> dict[str, Any]:
        return self._public(
            self.store.retry(
                _required_text(task_id, "task_id"),
                request_id=(
                    _required_text(request_id, "request_id")
                    if request_id is not None
                    else None
                ),
            )
        )

    def complete(
        self,
        task_id: str,
        attempt: int,
        result: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if result is not None and not isinstance(result, Mapping):
            raise InvalidTaskError("result must be a JSON object")
        normalized_result = dict(result) if result is not None else None
        if normalized_result is not None:
            _reject_inline_secrets(normalized_result, "task result")
        return self._public(
            self.store.complete(
                _required_text(task_id, "task_id"),
                int(attempt),
                self.instance_id,
                normalized_result,
                request_id=(
                    _required_text(request_id, "request_id")
                    if request_id is not None
                    else None
                ),
            )
        )

    def delete_task(self, task_id: str) -> dict[str, str]:
        normalized = _required_text(task_id, "task_id")
        self.store.delete_task(normalized)
        return {"deleted": normalized}

    def complete_with_issues(
        self,
        task_id: str,
        attempt: int,
        result: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if result is not None and not isinstance(result, Mapping):
            raise InvalidTaskError("result must be a JSON object")
        normalized_result = dict(result) if result is not None else None
        if normalized_result is not None:
            _reject_inline_secrets(normalized_result, "task result")
        return self._public(
            self.store.complete_with_issues(
                _required_text(task_id, "task_id"),
                int(attempt),
                self.instance_id,
                normalized_result,
                request_id=(
                    _required_text(request_id, "request_id")
                    if request_id is not None
                    else None
                ),
            )
        )

    def enter_repair_phase(
        self, task_id: str, attempt: int, *, request_id: str | None = None
    ) -> dict[str, Any]:
        return self._public(
            self.store.enter_repair_phase(
                _required_text(task_id, "task_id"),
                int(attempt),
                self.instance_id,
                request_id=(
                    _required_text(request_id, "request_id")
                    if request_id is not None
                    else None
                ),
            )
        )

    def fail(
        self,
        task_id: str,
        attempt: int,
        error: Mapping[str, Any],
        *,
        exit_code: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(error, Mapping):
            raise InvalidTaskError("error must be a JSON object")
        normalized_error = canonical_api_error(error)
        _reject_inline_secrets(normalized_error, "task error")
        return self._public(
            self.store.fail(
                _required_text(task_id, "task_id"),
                int(attempt),
                self.instance_id,
                normalized_error,
                exit_code=exit_code,
                request_id=(
                    _required_text(request_id, "request_id")
                    if request_id is not None
                    else None
                ),
            )
        )

    def interrupted(
        self,
        task_id: str,
        attempt: int,
        *,
        error: Mapping[str, Any] | None = None,
        reason: str = "worker_interrupted",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_error = None
        if error is not None:
            normalized_error = canonical_api_error(error)
            _reject_inline_secrets(normalized_error, "task error")
        return self._public(
            self.store.interrupted(
                _required_text(task_id, "task_id"),
                int(attempt),
                self.instance_id,
                error=normalized_error,
                reason=_required_text(reason, "reason"),
                request_id=(
                    _required_text(request_id, "request_id")
                    if request_id is not None
                    else None
                ),
            )
        )

    def cancelled(
        self,
        task_id: str,
        attempt: int,
        *,
        reason: str = "cancelled",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._public(
            self.store.cancelled(
                _required_text(task_id, "task_id"),
                int(attempt),
                self.instance_id,
                reason=_required_text(reason, "reason"),
                request_id=(
                    _required_text(request_id, "request_id")
                    if request_id is not None
                    else None
                ),
            )
        )

    def record_artifact(
        self,
        task_id: str,
        attempt: int,
        *,
        artifact_type: str,
        relative_path: str,
        sha256: str,
        byte_size: int,
        project_id: str | None = None,
        schema_version: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        artifact_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        raw_path = str(relative_path).replace("\\", "/")
        path = PurePosixPath(raw_path)
        if (
            not raw_path
            or path.is_absolute()
            or ".." in path.parts
            or (path.parts and path.parts[0].endswith(":"))
            or str(path) in {"", "."}
        ):
            raise InvalidTaskError("artifact path must be task/project-relative")
        digest = str(sha256).lower()
        if not _SHA256.fullmatch(digest):
            raise InvalidTaskError("artifact sha256 must contain 64 lowercase hex digits")
        if int(byte_size) < 0:
            raise InvalidTaskError("artifact byte_size cannot be negative")
        if metadata is not None:
            if not isinstance(metadata, Mapping):
                raise InvalidTaskError("artifact metadata must be a JSON object")
            _reject_inline_secrets(metadata, "artifact metadata")
        effective_id = artifact_id or f"art_{uuid.uuid4().hex}"
        artifact: TaskArtifact = self.store.record_artifact(
            task_id=_required_text(task_id, "task_id"),
            attempt=int(attempt),
            owner_instance_id=self.instance_id,
            artifact_id=_required_text(effective_id, "artifact_id"),
            artifact_type=_required_text(artifact_type, "artifact_type"),
            relative_path=path.as_posix(),
            sha256=digest,
            byte_size=int(byte_size),
            project_id=_optional_id(project_id, "project_id"),
            schema_version=(
                _required_text(schema_version, "schema_version")
                if schema_version is not None
                else None
            ),
            metadata=metadata,
            request_id=(
                _required_text(request_id, "request_id")
                if request_id is not None
                else None
            ),
        )
        return artifact.public()

    def list_artifacts(
        self, task_id: str, *, attempt: int | None = None
    ) -> list[dict[str, Any]]:
        normalized_attempt = None
        if attempt is not None:
            if isinstance(attempt, bool) or int(attempt) < 1:
                raise InvalidTaskError("attempt must be a positive integer")
            normalized_attempt = int(attempt)
        return [
            artifact.public()
            for artifact in self.store.list_artifacts(
                _required_text(task_id, "task_id"), attempt=normalized_attempt
            )
        ]

    def events(
        self,
        after: int = 0,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if int(after) < 0:
            raise InvalidTaskError("after cannot be negative")
        if not 1 <= int(limit) <= 1000:
            raise InvalidTaskError("limit must be between 1 and 1000")
        return [
            event.public()
            for event in self.store.events(
                after=int(after),
                task_id=_optional_id(task_id, "task_id"),
                project_id=_optional_id(project_id, "project_id"),
                limit=int(limit),
            )
        ]

    def reconcile_startup(self) -> list[dict[str, Any]]:
        return [
            self._public(task)
            for task in self.store.reconcile_startup(self.instance_id)
        ]
