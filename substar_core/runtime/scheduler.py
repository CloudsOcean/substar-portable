from __future__ import annotations

import os
import hashlib
import queue
import re
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .model import TaskRuntimeError
from .registry import ResourcePool, TaskHandler, TaskRegistry, TaskWorkContext
from .service import TaskService
from .supervisor import WorkerCompletion, WorkerEvent, WorkerHandle, WorkerSupervisor
from .worker_protocol import (
    WorkerCommand,
    WorkerMessageType,
    WorkerPaths,
    WorkerTaskType,
    credential_environment_key,
)


SupervisorFactory = Callable[..., WorkerSupervisor]
CredentialResolver = Callable[[tuple[str, ...]], Mapping[str, str]]
_PROTOCOL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SENSITIVE_ENVIRONMENT_MARKERS = (
    "api_key",
    "apikey",
    "access_key",
    "authorization",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)


class CredentialUnavailableError(TaskRuntimeError):
    def __init__(self, reference: str) -> None:
        self.reference = reference
        super().__init__(f"worker credential is unavailable: {reference}")


def _sanitized_worker_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not any(
            marker in key.casefold()
            for marker in _SENSITIVE_ENVIRONMENT_MARKERS
        )
    }


@dataclass
class _Execution:
    task: dict[str, Any]
    context: TaskWorkContext
    handler: TaskHandler
    resources: tuple[str, ...]
    supervisor: WorkerSupervisor
    handle: WorkerHandle
    last_heartbeat: float
    event_errors: list[str]
    startup_error: bool = False


class TaskScheduler:
    """Small durable scheduler that binds the task store to supervised workers."""

    def __init__(
        self,
        service: TaskService,
        registry: TaskRegistry,
        work_root: str | Path,
        *,
        resource_limits: Mapping[str, int] | None = None,
        poll_seconds: float = 0.1,
        lease_seconds: float = 30.0,
        heartbeat_seconds: float = 5.0,
        cancel_grace_seconds: float = 5.0,
        supervisor_factory: SupervisorFactory = WorkerSupervisor,
        credential_resolver: CredentialResolver | None = None,
    ) -> None:
        if poll_seconds <= 0 or lease_seconds <= 0 or heartbeat_seconds <= 0:
            raise ValueError("scheduler timing values must be positive")
        if heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat interval must be shorter than the lease")
        self.service = service
        self.registry = registry
        self.work_root = Path(work_root).resolve()
        self.resources = ResourcePool(resource_limits)
        self.poll_seconds = float(poll_seconds)
        self.lease_seconds = float(lease_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.cancel_grace_seconds = max(0.0, float(cancel_grace_seconds))
        self.supervisor_factory = supervisor_factory
        self.credential_resolver = credential_resolver
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._dispatch_lock = threading.Lock()
        self._executions: dict[tuple[str, int], _Execution] = {}
        self._starting_contexts: dict[tuple[str, int], TaskWorkContext] = {}
        self._pending_event_errors: dict[tuple[str, int], list[str]] = {}
        self._completions: queue.Queue[WorkerCompletion] = queue.Queue()
        self._worker_events: queue.Queue[WorkerEvent] = queue.Queue()
        self._errors: list[str] = []

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self.work_root.mkdir(parents=True, exist_ok=True)
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="substar-task-scheduler",
                daemon=True,
            )
            self._thread.start()

    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active = {
                f"{task_id}:{attempt}": {
                    "attempt": execution.handle.attempt,
                    "pid": execution.handle.pid,
                    "task_type": execution.task["task_type"],
                }
                for (task_id, attempt), execution in self._executions.items()
            }
            error_count = len(self._errors)
        return {
            "running": self.running(),
            "active": active,
            "resources": self.resources.snapshot(),
            "error_count": error_count,
        }

    def shutdown(
        self,
        grace_seconds: float = 5.0,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        if grace_seconds < 0:
            raise ValueError("grace_seconds must be non-negative")
        total_timeout = (
            max(10.0, grace_seconds + 5.0)
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        if total_timeout <= 0:
            raise ValueError("timeout_seconds must be positive")
        started = time.monotonic()
        deadline = started + total_timeout
        self._stop.set()
        with self._lock:
            known_executions = tuple(self._executions.values())
        for execution in known_executions:
            execution.handle.cancel(grace_seconds=max(0.0, grace_seconds))
        # A task becomes ``running`` when claimed, slightly before its worker
        # handle is published in ``_executions``. Wait boundedly for that
        # critical section so a broken handler.prepare cannot deadlock shutdown.
        dispatch_settled = self._dispatch_lock.acquire(
            timeout=min(5.0, max(0.0, deadline - time.monotonic()))
        )
        if dispatch_settled:
            self._dispatch_lock.release()
        with self._lock:
            executions = tuple(self._executions.values())
        for execution in executions:
            execution.handle.cancel(grace_seconds=max(0.0, grace_seconds))
        graceful_deadline = min(
            deadline, started + max(0.0, grace_seconds) + 1.0
        )
        for execution in executions:
            remaining = max(0.0, graceful_deadline - time.monotonic())
            if remaining <= 0:
                break
            try:
                execution.handle.wait(timeout=remaining)
            except TimeoutError:
                continue
        # Request force termination for every remaining process first. Each
        # supervisor monitor performs its own tree kill, so multiple workers
        # consume one shared deadline rather than N sequential 10-second waits.
        for execution in executions:
            if execution.handle.running():
                execution.handle.force_kill()
        worker_deadline = max(time.monotonic(), deadline - 5.0)
        for execution in executions:
            if not execution.handle.running():
                continue
            remaining = max(0.0, worker_deadline - time.monotonic())
            if remaining <= 0:
                break
            try:
                execution.handle.wait(timeout=remaining)
            except TimeoutError:
                self._remember_error(
                    f"worker force-kill timed out: "
                    f"{execution.task['task_id']} attempt {execution.handle.attempt}"
                )
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if not thread.is_alive():
                with self._lock:
                    if self._thread is thread:
                        self._thread = None
        thread_settled = thread is None or not thread.is_alive()
        if thread_settled:
            self._drain_completions(shutting_down=True)
        with self._lock:
            remaining = tuple(self._executions.values())
        unresolved: list[str] = []
        if not dispatch_settled:
            unresolved.append("task dispatch did not settle")
        if not thread_settled:
            unresolved.append("task scheduler thread did not stop")
        for execution in remaining:
            if execution.handle.running():
                identity = (
                    f"{execution.task['task_id']} attempt {execution.handle.attempt}"
                )
                unresolved.append(identity)
                self._remember_error(f"worker still running during shutdown: {identity}")
                continue
            if not thread_settled:
                continue
            try:
                self.service.interrupted(
                    execution.task["task_id"],
                    execution.handle.attempt,
                    reason="application_shutdown",
                    error=self._error(
                        "application_shutdown",
                        "The application stopped before the worker finalized.",
                        retryable=True,
                    ),
                )
            except TaskRuntimeError as exc:
                self._remember_error(exc)
            self._release_execution(
                execution.task["task_id"], execution.handle.attempt
            )
        if unresolved:
            raise RuntimeError(
                "task runtime shutdown did not settle: "
                + ", ".join(unresolved)
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._drain_worker_events()
                self._drain_completions(shutting_down=False)
                self._drive_cancellation()
                self._heartbeat()
                self._dispatch()
            except Exception:
                self._remember_error(traceback.format_exc())
            self._stop.wait(self.poll_seconds)
        self._drain_completions(shutting_down=True)

    def _dispatch(self) -> None:
        for handler in self.registry.handlers():
            if self._stop.is_set() or not self.resources.can_acquire(handler.resources):
                continue
            with self._dispatch_lock:
                if self._stop.is_set():
                    return
                task = self.service.claim_next(
                    {handler.task_type}, lease_seconds=self.lease_seconds
                )
                if task is None:
                    continue
                if not self.resources.acquire(handler.resources):
                    self.service.interrupted(
                        task["task_id"], task["attempt"], reason="resource_claim_race"
                    )
                    continue
                try:
                    self._start_execution(task, handler)
                except Exception as exc:
                    self._remember_error(exc)
                    with self._lock:
                        owned_execution = self._executions.get(
                            (str(task["task_id"]), int(task["attempt"]))
                        )
                    if owned_execution is not None:
                        # The process handle is already published. Its monitor
                        # and completion path retain ownership and release the
                        # resource claim only after the process tree settles.
                        continue
                    self.resources.release(handler.resources)
                    try:
                        if self._stop.is_set():
                            self.service.interrupted(
                                task["task_id"],
                                task["attempt"],
                                reason="application_shutdown",
                                error=self._error(
                                    "application_shutdown",
                                    "The application stopped before worker startup completed.",
                                    retryable=True,
                                ),
                            )
                        elif isinstance(exc, CredentialUnavailableError):
                            self.service.fail(
                                task["task_id"],
                                task["attempt"],
                                self._error(
                                    "credential_unavailable",
                                    "A required credential is unavailable.",
                                    category="configuration",
                                    retryable=True,
                                    details={"credential_ref": exc.reference},
                                ),
                            )
                        else:
                            self.service.fail(
                                task["task_id"],
                                task["attempt"],
                                self._error(
                                    "worker_start_failed",
                                    "Worker process could not be started.",
                                    retryable=True,
                                    details={"reason": str(exc)},
                                ),
                            )
                    except Exception as nested:
                        self._remember_error(nested)

    def _start_execution(self, task: dict[str, Any], handler: TaskHandler) -> None:
        task_id = str(task["task_id"])
        attempt = int(task["attempt"])
        attempt_directory = self.work_root / task_id / "attempts" / str(attempt)
        work_directory = attempt_directory / "work"
        artifact_directory = attempt_directory / "artifacts"
        work_directory.mkdir(parents=True, exist_ok=True)
        artifact_directory.mkdir(parents=True, exist_ok=True)
        context = TaskWorkContext(
            task=task,
            input_payload=self.service.get_task_input(task_id),
            attempt_directory=attempt_directory,
            work_directory=work_directory,
            artifact_directory=artifact_directory,
        )
        launch = handler.prepare(context)
        if self._stop.is_set():
            raise TaskRuntimeError("scheduler stopped during worker preparation")
        worker_command = WorkerCommand(
            task_id=task_id,
            attempt=attempt,
            task_type=WorkerTaskType(handler.task_type),
            project_id=task.get("project_id"),
            input_schema=str(task["input_schema"]),
            input=(
                dict(launch.worker_input)
                if launch.worker_input is not None
                else dict(context.input_payload)
            ),
            paths=WorkerPaths(
                project_root=(
                    str(launch.project_root.resolve())
                    if launch.project_root is not None
                    else None
                ),
                work_directory=str(work_directory),
                artifact_directory=str(artifact_directory),
            ),
            credential_refs=launch.credential_refs,
            trace_context={"scheduler_instance_id": self.service.instance_id},
        )
        stderr_log = attempt_directory / "stderr.log"
        supervisor = self.supervisor_factory(log_directory=attempt_directory)
        execution_key = (task_id, attempt)
        with self._lock:
            self._starting_contexts[execution_key] = context
        try:
            environment = _sanitized_worker_environment()
            for key, value in dict(launch.env or {}).items():
                if any(
                    marker in key.casefold()
                    for marker in _SENSITIVE_ENVIRONMENT_MARKERS
                ):
                    raise TaskRuntimeError(
                        "worker launch environment cannot carry credentials"
                    )
                environment[key] = value
            if launch.credential_refs:
                if self.credential_resolver is None:
                    raise TaskRuntimeError("worker credentials cannot be resolved")
                credentials = dict(self.credential_resolver(launch.credential_refs))
                if set(credentials) != set(launch.credential_refs):
                    raise TaskRuntimeError(
                        "worker credential resolver returned the wrong authority set"
                    )
                for reference, secret in credentials.items():
                    if not isinstance(secret, str) or not secret.strip():
                        raise CredentialUnavailableError(reference)
                    environment[credential_environment_key(reference)] = secret
            handle = supervisor.start(
                task_id,
                attempt,
                launch.argv,
                lambda event: self._worker_events.put(event),
                lambda completion: self._completions.put(completion),
                worker_command=worker_command,
                timeout_seconds=launch.timeout_seconds,
                cwd=launch.cwd,
                env=environment,
                stderr_log_path=stderr_log,
            )
        except Exception:
            with self._lock:
                self._starting_contexts.pop(execution_key, None)
                self._pending_event_errors.pop(execution_key, None)
            raise
        execution = _Execution(
            task=task,
            context=context,
            handler=handler,
            resources=handler.resources,
            supervisor=supervisor,
            handle=handle,
            last_heartbeat=time.monotonic(),
            event_errors=[],
        )
        with self._lock:
            execution.event_errors.extend(
                self._pending_event_errors.pop(execution_key, [])
            )
            self._executions[execution_key] = execution
            self._starting_contexts.pop(execution_key, None)
        if self._stop.is_set():
            handle.force_kill()
            raise TaskRuntimeError("scheduler stopped during worker startup")
        try:
            self.service.attach_worker(
                task_id,
                attempt,
                worker_id=f"wrk_{uuid.uuid4().hex}",
                worker_pid=handle.pid,
                work_directory=str(work_directory),
                stderr_log=str(stderr_log),
            )
        except Exception:
            execution.startup_error = True
            handle.force_kill()
            raise

    def _on_worker_event(self, event: WorkerEvent) -> None:
        if event.kind != "message" or event.message is None:
            return
        message = event.message
        try:
            if message.message_type is WorkerMessageType.READY:
                self.service.heartbeat(
                    event.task_id, event.attempt, lease_seconds=self.lease_seconds
                )
            elif message.message_type is WorkerMessageType.PROGRESS:
                with self._lock:
                    execution = self._executions.get(
                        (event.task_id, event.attempt)
                    )
                update = (
                    execution.handler.handle_worker_event(
                        execution.context, message
                    )
                    if execution is not None
                    else {
                        "progress": float(message.progress or 0.0),
                        "message": None,
                        "step": None,
                        "wait_reason": None,
                        "phase": None,
                        "completed_units": None,
                        "total_units": None,
                    }
                )
                required = {"progress", "message", "step", "wait_reason"}
                optional = {"phase", "completed_units", "total_units"}
                if not required.issubset(update) or set(update) - required - optional:
                    raise TaskRuntimeError(
                        "task handler returned an invalid progress projection"
                    )
                if update.get("phase") == "repair":
                    current_task = self.service.get_task(event.task_id)
                    if not current_task["repair_phase_entered"]:
                        self.service.enter_repair_phase(event.task_id, event.attempt)
                self.service.record_progress(
                    event.task_id,
                    event.attempt,
                    float(update["progress"]),
                    message=update["message"],
                    step=update["step"],
                    wait_reason=update["wait_reason"],
                    phase=update.get("phase"),
                    completed_units=update.get("completed_units"),
                    total_units=update.get("total_units"),
                )
            elif message.message_type is WorkerMessageType.ARTIFACT:
                relative_path, digest, byte_size = self._verify_worker_artifact(
                    event.task_id,
                    event.attempt,
                    str(message.data.get("relative_path", "")),
                    str(message.data.get("sha256", "")),
                    int(message.data.get("byte_size", -1)),
                )
                artifact_type = str(message.data.get("artifact_type", ""))
                if not _PROTOCOL_IDENTIFIER.fullmatch(artifact_type):
                    raise TaskRuntimeError("worker artifact_type is invalid")
                schema_version = (
                    str(message.data["schema_version"])
                    if message.data.get("schema_version")
                    else None
                )
                if schema_version is not None and not _PROTOCOL_IDENTIFIER.fullmatch(
                    schema_version
                ):
                    raise TaskRuntimeError("worker artifact schema_version is invalid")
                self.service.record_artifact(
                    event.task_id,
                    event.attempt,
                    artifact_type=artifact_type,
                    relative_path=relative_path,
                    sha256=digest,
                    byte_size=byte_size,
                    project_id=None,
                    schema_version=schema_version,
                    metadata=None,
                )
        except Exception as exc:
            self._remember_error(exc)
            with self._lock:
                execution = self._executions.get((event.task_id, event.attempt))
                if execution is not None:
                    execution.event_errors.append(str(exc))
                else:
                    self._pending_event_errors.setdefault(
                        (event.task_id, event.attempt), []
                    ).append(str(exc))

    def _drain_worker_events(self) -> None:
        while True:
            try:
                event = self._worker_events.get_nowait()
            except queue.Empty:
                return
            self._on_worker_event(event)

    def _verify_worker_artifact(
        self,
        task_id: str,
        attempt: int,
        relative_path: str,
        claimed_sha256: str,
        claimed_size: int,
    ) -> tuple[str, str, int]:
        with self._lock:
            execution = self._executions.get((task_id, int(attempt)))
            context = (
                execution.context
                if execution is not None
                else self._starting_contexts.get((task_id, int(attempt)))
            )
        if context is None:
            raise TaskRuntimeError("artifact arrived without an active task attempt")
        raw = relative_path.replace("\\", "/")
        relative = PurePosixPath(raw)
        if not raw or relative.is_absolute() or ".." in relative.parts:
            raise TaskRuntimeError("worker artifact path is not relative")
        root = context.artifact_directory.resolve()
        candidate = root.joinpath(*relative.parts).resolve()
        if root not in candidate.parents or not candidate.is_file():
            raise TaskRuntimeError("worker artifact is outside its artifact directory")
        actual_size = candidate.stat().st_size
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        actual_sha256 = digest.hexdigest()
        if actual_size != int(claimed_size) or actual_sha256 != claimed_sha256.lower():
            raise TaskRuntimeError("worker artifact size or sha256 does not match its file")
        return f"artifacts/{relative.as_posix()}", actual_sha256, actual_size

    def _revalidate_registered_artifacts(self, execution: _Execution) -> None:
        """Close the worker-write/checksum race immediately before success."""

        task_id = str(execution.task["task_id"])
        attempt = int(execution.handle.attempt)
        for artifact in self.service.list_artifacts(task_id, attempt=attempt):
            stored_path = PurePosixPath(str(artifact["relative_path"]))
            if (
                len(stored_path.parts) < 2
                or stored_path.parts[0] != "artifacts"
            ):
                raise TaskRuntimeError("registered artifact path is outside attempt artifacts")
            worker_relative = PurePosixPath(*stored_path.parts[1:]).as_posix()
            self._verify_worker_artifact(
                task_id,
                attempt,
                worker_relative,
                str(artifact["sha256"]),
                int(artifact["byte_size"]),
            )

    def _verify_result_artifact_registration(
        self, execution: _Execution, completion: WorkerCompletion
    ) -> None:
        """Require worker-declared artifacts to equal the durable registry.

        A digest-valid file is not a published artifact until its ARTIFACT
        event has been committed.  This exact comparison prevents a worker
        from skipping registration and still reaching a successful finalizer.
        """

        result = completion.result
        if not isinstance(result, Mapping) or "artifacts" not in result:
            return
        declared = result["artifacts"]
        if not isinstance(declared, list):
            raise TaskRuntimeError("worker result artifacts must be an array")

        def row_key(row: Mapping[str, Any], *, registered: bool) -> tuple[Any, ...]:
            relative_path = str(row.get("relative_path", "")).replace("\\", "/")
            if registered:
                prefix = "artifacts/"
                if not relative_path.startswith(prefix):
                    raise TaskRuntimeError("registered artifact path is invalid")
                relative_path = relative_path[len(prefix):]
            return (
                str(row.get("artifact_type", "")),
                relative_path,
                row.get("schema_version"),
                str(row.get("sha256", "")).lower(),
                int(row.get("byte_size", -1)),
            )

        declared_rows: set[tuple[Any, ...]] = set()
        for row in declared:
            if not isinstance(row, Mapping):
                raise TaskRuntimeError("worker result artifact row is invalid")
            declared_rows.add(row_key(row, registered=False))
        registered_rows = {
            row_key(row, registered=True)
            for row in self.service.list_artifacts(
                str(execution.task["task_id"]), attempt=completion.attempt
            )
        }
        if declared_rows != registered_rows or len(declared_rows) != len(declared):
            raise TaskRuntimeError(
                "worker result artifacts do not match the durable artifact registry"
            )

    def _drive_cancellation(self) -> None:
        cancelling = {
            task["task_id"]
            for task in self.service.list_tasks(states=["cancelling"], limit=500)
        }
        with self._lock:
            executions = tuple(self._executions.values())
        for execution in executions:
            if execution.task["task_id"] in cancelling and execution.handle.running():
                execution.handle.cancel(grace_seconds=self.cancel_grace_seconds)

    def _heartbeat(self) -> None:
        now = time.monotonic()
        with self._lock:
            executions = tuple(self._executions.values())
        for execution in executions:
            if now - execution.last_heartbeat < self.heartbeat_seconds:
                continue
            if not execution.handle.running():
                continue
            self.service.heartbeat(
                execution.task["task_id"],
                execution.handle.attempt,
                lease_seconds=self.lease_seconds,
            )
            execution.last_heartbeat = now

    def _drain_completions(self, *, shutting_down: bool) -> None:
        # Supervisor completion is queued only after stdout is fully parsed, so
        # draining worker events first preserves progress/artifact-before-terminal
        # ordering even when SQLite briefly blocks the scheduler thread.
        self._drain_worker_events()
        while True:
            try:
                completion = self._completions.get_nowait()
            except queue.Empty:
                return
            with self._lock:
                execution = self._executions.get(
                    (completion.task_id, completion.attempt)
                )
            if execution is None:
                continue
            try:
                current = self.service.get_task(completion.task_id)
                if completion.status == "succeeded":
                    try:
                        self._revalidate_registered_artifacts(execution)
                        self._verify_result_artifact_registration(
                            execution, completion
                        )
                    except Exception as exc:
                        self._remember_error(exc)
                        execution.event_errors.append(str(exc))
                if (
                    shutting_down
                    and completion.cancellation_requested
                    and current["state"] != "cancelling"
                ):
                    self.service.interrupted(
                        completion.task_id,
                        completion.attempt,
                        reason="application_shutdown",
                        error=self._error(
                            "application_shutdown",
                            "The application stopped while this task was running.",
                            retryable=True,
                        ),
                    )
                elif execution.startup_error:
                    self.service.fail(
                        completion.task_id,
                        completion.attempt,
                        self._error(
                            "worker_start_failed",
                            "Worker process could not be attached to durable task state.",
                            retryable=True,
                        ),
                        exit_code=completion.returncode,
                    )
                elif completion.status == "succeeded" and execution.event_errors:
                    self.service.fail(
                        completion.task_id,
                        completion.attempt,
                        self._error(
                            "worker_event_invalid",
                            "A worker event failed validation.",
                            retryable=True,
                            details={
                                "error_count": len(execution.event_errors),
                                # Keep the bounded validation reason in the durable
                                # failure envelope.  Without it, production failures
                                # collapse to an unactionable counter even though the
                                # scheduler already retained the exact rejection.
                                "reasons": execution.event_errors[-5:],
                            },
                        ),
                        exit_code=completion.returncode,
                    )
                elif completion.status == "succeeded":
                    result = execution.handler.finalize(execution.context, completion)
                    if bool(result.get("needs_attention")):
                        self.service.complete_with_issues(
                            completion.task_id, completion.attempt, result
                        )
                    else:
                        self.service.complete(
                            completion.task_id, completion.attempt, result
                        )
                elif completion.status == "cancelled":
                    self.service.cancelled(completion.task_id, completion.attempt)
                else:
                    public_worker_message = ""
                    if isinstance(completion.error, Mapping):
                        candidate = completion.error.get("public_message")
                        if isinstance(candidate, str):
                            public_worker_message = candidate.strip()[:1600]
                    failure_message = (
                        f"Worker execution failed:\n{public_worker_message}"
                        if public_worker_message
                        else "Worker execution failed."
                    )
                    self.service.fail(
                        completion.task_id,
                        completion.attempt,
                        self._error(
                            "worker_failed",
                            failure_message,
                            retryable=True,
                            details={
                                "returncode": completion.returncode,
                                "status": completion.status,
                                "protocol_error_count": len(
                                    completion.protocol_errors
                                ),
                                **(
                                    {"algorithm_stderr_tail": public_worker_message}
                                    if public_worker_message
                                    else {}
                                ),
                            },
                        ),
                        exit_code=completion.returncode,
                    )
            except Exception as exc:
                self._remember_error(exc)
                try:
                    self.service.interrupted(
                        completion.task_id,
                        completion.attempt,
                        reason="finalization_failed",
                        error=self._error(
                            "finalization_failed",
                            (
                                "Worker result could not be finalized: "
                                f"{type(exc).__name__}: {str(exc)[:1200]}"
                            ),
                            retryable=True,
                            details={
                                "exception_type": type(exc).__name__,
                                "reason": str(exc)[:1200],
                            },
                        ),
                    )
                except TaskRuntimeError as nested:
                    self._remember_error(nested)
            finally:
                self._release_execution(completion.task_id, completion.attempt)

    def _release_execution(self, task_id: str, attempt: int) -> None:
        with self._lock:
            execution = self._executions.pop((task_id, int(attempt)), None)
        if execution is not None:
            self.resources.release(execution.resources)

    def _remember_error(self, error: object) -> None:
        text = str(error)
        with self._lock:
            self._errors.append(text)
            if len(self._errors) > 100:
                del self._errors[:-100]

    @staticmethod
    def _error(
        code: str,
        message: str,
        *,
        category: str = "process_failed",
        retryable: bool,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "substar.api-error.v1",
            "code": code,
            "category": category,
            "message": message,
            "retryable": retryable,
            "request_id": "runtime_scheduler",
            "details": dict(details or {}),
        }
