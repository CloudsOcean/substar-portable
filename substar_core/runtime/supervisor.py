from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

from .windows_process import (
    WindowsJobObject,
    force_kill_process_tree,
    worker_creation_flags,
)
from .worker_protocol import (
    WorkerCommand,
    WorkerControl,
    WorkerControlType,
    WorkerMessage,
    WorkerMessageType,
    WorkerProtocolError,
    parse_message_line,
)


MessageCallback = Callable[["WorkerEvent"], None]
ExitCallback = Callable[["WorkerCompletion"], None]
PopenFactory = Callable[..., subprocess.Popen[str]]
TreeKiller = Callable[[subprocess.Popen[str], WindowsJobObject | None], bool]


class WorkerSupervisorError(RuntimeError):
    pass


class WorkerAlreadyRunningError(WorkerSupervisorError):
    pass


class WorkerNotFoundError(WorkerSupervisorError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class WorkerEvent:
    task_id: str
    attempt: int
    kind: str
    created_at: str
    message: WorkerMessage | None = None
    text: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerCompletion:
    task_id: str
    attempt: int
    status: str
    returncode: int
    started_at: str
    finished_at: str
    duration_seconds: float
    cancellation_requested: bool
    forced: bool
    timed_out: bool
    result: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    stderr_log_path: str | None = None
    stderr_tail: tuple[str, ...] = ()
    protocol_errors: tuple[str, ...] = ()


@dataclass
class _RunningTask:
    task_id: str
    attempt: int
    process: subprocess.Popen[str]
    started_at: str
    started_monotonic: float
    timeout_seconds: float | None
    timeout_grace_seconds: float
    stderr_log_path: Path | None
    on_message: MessageCallback | None
    on_exit: ExitCallback | None
    job_object: WindowsJobObject | None = None
    cancel_requested: bool = False
    cancel_deadline: float | None = None
    timed_out: bool = False
    forced: bool = False
    result: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    sequence: int = -1
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=80))
    protocol_errors: list[str] = field(default_factory=list)
    terminal_message_type: WorkerMessageType | None = None
    stdin_lock: threading.Lock = field(default_factory=threading.Lock)
    completed: threading.Event = field(default_factory=threading.Event)
    completion: WorkerCompletion | None = None
    monitor_thread: threading.Thread | None = None


class WorkerHandle:
    """Public capability for one supervisor-owned worker process."""

    def __init__(self, supervisor: "WorkerSupervisor", task: _RunningTask) -> None:
        self._supervisor = supervisor
        self._task = task

    @property
    def task_id(self) -> str:
        return self._task.task_id

    @property
    def attempt(self) -> int:
        return self._task.attempt

    @property
    def pid(self) -> int:
        return self._task.process.pid

    def running(self) -> bool:
        return not self._task.completed.is_set()

    def cancel(self, grace_seconds: float = 5.0) -> bool:
        return self._supervisor.cancel(
            self.task_id, attempt=self.attempt, grace_seconds=grace_seconds
        )

    def force_kill(self) -> bool:
        return self._supervisor.force_kill(
            self.task_id, attempt=self.attempt
        )

    def wait(self, timeout: float | None = None) -> WorkerCompletion:
        started = time.monotonic()
        completion = self._supervisor.wait(
            self.task_id, attempt=self.attempt, timeout=timeout
        )
        monitor = self._task.monitor_thread
        if monitor is not None and monitor is not threading.current_thread():
            remaining = None
            if timeout is not None:
                remaining = max(0.0, float(timeout) - (time.monotonic() - started))
            monitor.join(timeout=remaining)
            if monitor.is_alive():
                raise TimeoutError(f"worker monitor wait timed out: {self.task_id}")
        return completion


class WorkerSupervisor:
    """Own exactly one worker process and its complete lifecycle.

    Workers receive versioned JSONL commands on stdin and must emit only
    versioned JSONL messages on stdout.  Human diagnostics belong on stderr.
    Cancellation first sends a cooperative command; after the grace period the
    complete OS process tree is terminated.
    """

    def __init__(
        self,
        *,
        log_directory: str | Path | None = None,
        popen_factory: PopenFactory = subprocess.Popen,
        tree_killer: TreeKiller = force_kill_process_tree,
        poll_interval: float = 0.05,
        timeout_grace_seconds: float = 2.0,
        event_callback: MessageCallback | None = None,
        completion_callback: ExitCallback | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if timeout_grace_seconds < 0:
            raise ValueError("timeout_grace_seconds must be non-negative")
        self._log_directory = Path(log_directory) if log_directory else None
        self._popen_factory = popen_factory
        self._tree_killer = tree_killer
        self._poll_interval = float(poll_interval)
        self._timeout_grace_seconds = float(timeout_grace_seconds)
        self._event_callback = event_callback
        self._completion_callback = completion_callback
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._active: _RunningTask | None = None
        self._completions: OrderedDict[tuple[str, int], WorkerCompletion] = (
            OrderedDict()
        )

    def start(
        self,
        task_id: str,
        attempt: int,
        command: Sequence[str],
        on_message: MessageCallback | None = None,
        on_exit: ExitCallback | None = None,
        *,
        worker_command: WorkerCommand,
        timeout_seconds: float | None = None,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        stderr_log_path: str | Path | None = None,
    ) -> WorkerHandle:
        if (
            isinstance(command, (str, bytes))
            or not command
            or any(not isinstance(item, str) or not item for item in command)
        ):
            raise ValueError("command must contain non-empty strings")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not isinstance(worker_command, WorkerCommand):
            raise TypeError("worker_command must be a WorkerCommand")
        if worker_command.task_id != task_id or worker_command.attempt != int(attempt):
            raise ValueError("worker_command identity does not match supervisor ownership")
        with self._lock:
            if self._active is not None and not self._active.completed.is_set():
                raise WorkerAlreadyRunningError(
                    f"worker {self._active.task_id} attempt {self._active.attempt} is running"
                )
            log_path = self._resolve_log_path(
                worker_command.task_id, int(attempt), stderr_log_path
            )
            popen_kwargs: dict[str, Any] = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
                "cwd": str(Path(cwd).resolve()) if cwd is not None else None,
                "env": dict(env) if env is not None else None,
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = worker_creation_flags()
            else:
                popen_kwargs["start_new_session"] = True
            process = self._popen_factory(list(command), **popen_kwargs)
            job_object: WindowsJobObject | None = None
            task: _RunningTask | None = None
            try:
                job_object = WindowsJobObject() if os.name == "nt" else None
                if job_object is not None and not job_object.assign(process):
                    raise WorkerSupervisorError(
                        "worker could not be assigned to its process-tree Job Object"
                    )
                if (
                    process.stdin is None
                    or process.stdout is None
                    or process.stderr is None
                ):
                    raise WorkerSupervisorError("worker pipes were not created")
                task = _RunningTask(
                    task_id=worker_command.task_id,
                    attempt=int(attempt),
                    process=process,
                    started_at=_now(),
                    started_monotonic=self._monotonic(),
                    timeout_seconds=(
                        float(timeout_seconds)
                        if timeout_seconds is not None
                        else None
                    ),
                    timeout_grace_seconds=self._timeout_grace_seconds,
                    stderr_log_path=log_path,
                    on_message=on_message,
                    on_exit=on_exit,
                    job_object=job_object,
                )
                self._active = task
                self._write_command(task, worker_command)
            except Exception:
                if self._active is task:
                    self._active = None
                self._cleanup_failed_start(process, job_object)
                raise
            assert task is not None
            handle = WorkerHandle(self, task)
            monitor_thread = threading.Thread(
                target=self._monitor,
                args=(task,),
                name=f"substar-worker-{task.task_id}",
                daemon=True,
            )
            task.monitor_thread = monitor_thread
            monitor_thread.start()
            self._emit(task, "lifecycle", detail={"state": "started", "pid": process.pid})
            return handle

    def running(self) -> bool:
        with self._lock:
            return self._active is not None and not self._active.completed.is_set()

    def active_handle(self) -> WorkerHandle | None:
        with self._lock:
            if self._active is None or self._active.completed.is_set():
                return None
            return WorkerHandle(self, self._active)

    def cancel(
        self,
        task_id: str,
        *,
        attempt: int | None = None,
        grace_seconds: float = 5.0,
    ) -> bool:
        if grace_seconds < 0:
            raise ValueError("grace_seconds must be non-negative")
        with self._lock:
            task = self._active
            if (
                task is None
                or task.completed.is_set()
                or task.process.poll() is not None
                or task.task_id != task_id
                or (attempt is not None and task.attempt != attempt)
            ):
                return False
            if not task.cancel_requested:
                task.cancel_requested = True
                task.cancel_deadline = self._monotonic() + float(grace_seconds)
                try:
                    self._write_command(
                        task,
                        WorkerControl(
                            task_id=task.task_id,
                            attempt=task.attempt,
                            control_type=WorkerControlType.CANCEL,
                            data={"reason": "requested"},
                        ),
                    )
                except (BrokenPipeError, OSError, ValueError):
                    task.cancel_deadline = self._monotonic()
                self._emit(
                    task,
                    "lifecycle",
                    detail={"state": "cancelling", "grace_seconds": grace_seconds},
                )
            return True

    def force_kill(self, task_id: str, *, attempt: int | None = None) -> bool:
        """Immediately request verified process-tree termination, retaining ownership."""

        with self._lock:
            task = self._active
            if (
                task is None
                or task.completed.is_set()
                or task.task_id != task_id
                or (attempt is not None and task.attempt != attempt)
            ):
                return False
            task.cancel_requested = True
            task.forced = True
            task.cancel_deadline = self._monotonic()
        return True

    def wait(
        self,
        task_id: str,
        *,
        attempt: int | None = None,
        timeout: float | None = None,
    ) -> WorkerCompletion:
        with self._lock:
            task = self._active
            if task is None or task.task_id != task_id or (
                attempt is not None and task.attempt != attempt
            ):
                key = self._completion_key(task_id, attempt)
                if key is not None:
                    return self._completions[key]
                raise WorkerNotFoundError(f"unknown worker task: {task_id}")
        if not task.completed.wait(timeout):
            raise TimeoutError(f"worker wait timed out: {task_id}")
        assert task.completion is not None
        return task.completion

    def shutdown(self, grace_seconds: float = 2.0) -> WorkerCompletion | None:
        handle = self.active_handle()
        if handle is None:
            return None
        handle.cancel(grace_seconds=grace_seconds)
        return handle.wait(timeout=max(5.0, grace_seconds + 5.0))

    def _cleanup_failed_start(
        self,
        process: subprocess.Popen[str],
        job_object: WindowsJobObject | None,
    ) -> None:
        try:
            if process.poll() is None:
                try:
                    self._tree_killer(process, job_object)
                except BaseException:
                    process.kill()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream is not None and not stream.closed:
                        stream.close()
                except OSError:
                    pass
            if job_object is not None:
                job_object.close()

    def _attempt_tree_kill(self, task: _RunningTask) -> None:
        settled = False
        try:
            settled = bool(self._tree_killer(task.process, task.job_object))
            if not settled:
                raise WorkerSupervisorError(
                    "process tree termination could not be confirmed"
                )
        except BaseException as exc:
            task.error = {
                "code": "process_tree_kill_failed",
                "message": str(exc) or type(exc).__name__,
            }
            self._emit(
                task,
                "lifecycle",
                detail={"state": "force_kill_failed", "error": str(exc)},
            )
        finally:
            task.cancel_deadline = (
                None
                if settled and task.process.poll() is not None
                else self._monotonic() + 1.0
            )

    def _resolve_log_path(
        self, task_id: str, attempt: int, explicit: str | Path | None
    ) -> Path | None:
        if explicit is not None:
            path = Path(explicit)
        elif self._log_directory is not None:
            path = self._log_directory / f"{task_id}.attempt-{attempt}.stderr.log"
        else:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    @staticmethod
    def _write_command(
        task: _RunningTask, command: WorkerCommand | WorkerControl
    ) -> None:
        stream = task.process.stdin
        if stream is None or stream.closed:
            raise BrokenPipeError("worker stdin is closed")
        with task.stdin_lock:
            stream.write(command.to_json_line())
            stream.flush()

    def _monitor(self, task: _RunningTask) -> None:
        stdout_reader = threading.Thread(
            target=self._read_stdout, args=(task,), daemon=True
        )
        stderr_reader = threading.Thread(
            target=self._read_stderr, args=(task,), daemon=True
        )
        stdout_reader.start()
        stderr_reader.start()
        try:
            while task.process.poll() is None:
                now = self._monotonic()
                if (
                    task.timeout_seconds is not None
                    and not task.timed_out
                    and now - task.started_monotonic >= task.timeout_seconds
                ):
                    task.timed_out = True
                    self.cancel(
                        task.task_id,
                        attempt=task.attempt,
                        grace_seconds=task.timeout_grace_seconds,
                    )
                    self._emit(
                        task,
                        "lifecycle",
                        detail={"state": "timed_out", "timeout_seconds": task.timeout_seconds},
                    )
                if (
                    task.cancel_deadline is not None
                    and now >= task.cancel_deadline
                    and task.process.poll() is None
                ):
                    task.forced = True
                    self._emit(task, "lifecycle", detail={"state": "force_kill"})
                    self._attempt_tree_kill(task)
                time.sleep(self._poll_interval)
            returncode = int(task.process.wait())
        except BaseException as exc:
            task.error = {"code": "supervisor_failure", "message": str(exc)}
            while task.process.poll() is None:
                task.forced = True
                self._attempt_tree_kill(task)
                if task.process.poll() is None:
                    time.sleep(1.0)
            returncode = int(task.process.wait())
        finally:
            try:
                if task.process.stdin is not None and not task.process.stdin.closed:
                    task.process.stdin.close()
            except OSError:
                pass
        # The root can exit while descendants still hold inherited pipe handles.
        # Terminate the owned tree before waiting for EOF, and retain ownership
        # indefinitely rather than publishing a false completion while either
        # reader is still alive.
        tree_settled = False
        while (
            not tree_settled
            or stdout_reader.is_alive()
            or stderr_reader.is_alive()
        ):
            try:
                tree_settled = bool(
                    self._tree_killer(task.process, task.job_object)
                )
                if not tree_settled:
                    raise WorkerSupervisorError(
                        "residual process tree cleanup could not be confirmed"
                    )
            except BaseException as exc:
                tree_settled = False
                task.error = {
                    "code": "residual_process_tree_cleanup_failed",
                    "message": str(exc) or type(exc).__name__,
                }
                self._emit(
                    task,
                    "lifecycle",
                    detail={"state": "residual_process_tree_cleanup_failed"},
                )
            stdout_reader.join(timeout=0.5)
            stderr_reader.join(timeout=0.5)
            if (
                not tree_settled
                and not stdout_reader.is_alive()
                and not stderr_reader.is_alive()
            ):
                time.sleep(max(0.05, self._poll_interval))
        for stream in (task.process.stdout, task.process.stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except OSError:
                pass
        self._complete(task, returncode)

    def _read_stdout(self, task: _RunningTask) -> None:
        assert task.process.stdout is not None
        for raw_line in task.process.stdout:
            line = raw_line.rstrip("\r\n")
            try:
                message = parse_message_line(line)
                if message.task_id != task.task_id:
                    raise WorkerProtocolError(
                        f"message task_id {message.task_id!r} does not match owner {task.task_id!r}"
                    )
                if message.attempt != task.attempt:
                    raise WorkerProtocolError(
                        f"message attempt {message.attempt} does not match owner {task.attempt}"
                    )
                if message.sequence <= task.sequence:
                    raise WorkerProtocolError(
                        f"message sequence {message.sequence} is not greater than {task.sequence}"
                    )
                task.sequence = message.sequence
                if task.terminal_message_type is not None:
                    raise WorkerProtocolError(
                        "worker emitted a message after its terminal message"
                    )
                if message.message_type in {
                    WorkerMessageType.RESULT,
                    WorkerMessageType.ERROR,
                    WorkerMessageType.CANCELLED,
                }:
                    task.terminal_message_type = message.message_type
                if message.message_type is WorkerMessageType.RESULT:
                    task.result = dict(message.data)
                elif message.message_type is WorkerMessageType.ERROR:
                    task.error = dict(message.data)
                self._emit(task, "message", message=message)
            except WorkerProtocolError as exc:
                detail = str(exc)
                task.protocol_errors.append(detail)
                self._emit(
                    task,
                    "protocol_error",
                    text=line[:2000],
                    detail={"error": detail},
                )

    def _read_stderr(self, task: _RunningTask) -> None:
        assert task.process.stderr is not None
        handle: TextIO | None = None
        try:
            if task.stderr_log_path is not None:
                try:
                    handle = task.stderr_log_path.open(
                        "a", encoding="utf-8", newline=""
                    )
                except OSError as exc:
                    task.error = {
                        "code": "stderr_log_unavailable",
                        "message": str(exc),
                    }
                    self._emit(
                        task,
                        "lifecycle",
                        detail={"state": "stderr_log_unavailable"},
                    )
            for raw_line in task.process.stderr:
                line = raw_line.rstrip("\r\n")
                task.stderr_tail.append(line)
                if handle is not None:
                    try:
                        handle.write(raw_line)
                        handle.flush()
                    except OSError:
                        handle.close()
                        handle = None
                        task.error = {
                            "code": "stderr_log_write_failed",
                            "message": "worker stderr log could not be written",
                        }
                self._emit(task, "stderr", text=line)
        finally:
            if handle is not None:
                handle.close()

    def _complete(self, task: _RunningTask, returncode: int) -> None:
        duration = max(0.0, self._monotonic() - task.started_monotonic)
        if task.timed_out:
            status = "timed_out"
        elif (
            task.terminal_message_type is WorkerMessageType.CANCELLED
            or task.cancel_requested
            or (
                task.result is None
                and task.error is None
                and returncode != 0
                and task.forced
            )
        ):
            status = "cancelled"
        elif (
            returncode == 0
            and task.error is None
            and task.terminal_message_type is WorkerMessageType.RESULT
        ):
            status = "succeeded"
        else:
            status = "failed"
        if task.terminal_message_type is None and not task.forced:
            task.protocol_errors.append("worker exited without a terminal message")
        if task.protocol_errors:
            if status == "succeeded":
                status = "failed"
            if status == "failed" and task.error is None:
                task.error = {
                    "code": "worker_protocol_error",
                    "message": task.protocol_errors[0],
                }
        completion = WorkerCompletion(
            task_id=task.task_id,
            attempt=task.attempt,
            status=status,
            returncode=returncode,
            started_at=task.started_at,
            finished_at=_now(),
            duration_seconds=round(duration, 6),
            cancellation_requested=task.cancel_requested,
            forced=task.forced,
            timed_out=task.timed_out,
            result=dict(task.result) if task.result is not None else None,
            error=dict(task.error) if task.error is not None else None,
            stderr_log_path=(
                str(task.stderr_log_path) if task.stderr_log_path is not None else None
            ),
            stderr_tail=tuple(task.stderr_tail),
            protocol_errors=tuple(task.protocol_errors),
        )
        task.completion = completion
        if task.job_object is not None:
            task.job_object.close()
        with self._lock:
            self._completions[(task.task_id, task.attempt)] = completion
            self._completions.move_to_end((task.task_id, task.attempt))
            while len(self._completions) > 32:
                self._completions.popitem(last=False)
            if self._active is task:
                self._active = None
        # Process streams and OS ownership are settled before completion is
        # visible. WorkerHandle.wait() additionally joins the monitor so its
        # caller also observes completion callbacks as delivered.
        task.completed.set()
        self._emit(task, "lifecycle", detail={"state": "completed", "status": status})
        self._safe_callback(task.on_exit, completion)
        self._safe_callback(self._completion_callback, completion)

    def _completion_key(
        self, task_id: str, attempt: int | None
    ) -> tuple[str, int] | None:
        if attempt is not None:
            key = (task_id, attempt)
            return key if key in self._completions else None
        for key in reversed(self._completions):
            if key[0] == task_id:
                return key
        return None

    def _emit(
        self,
        task: _RunningTask,
        kind: str,
        *,
        message: WorkerMessage | None = None,
        text: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        event = WorkerEvent(
            task_id=task.task_id,
            attempt=task.attempt,
            kind=kind,
            created_at=_now(),
            message=message,
            text=text,
            detail=dict(detail or {}),
        )
        self._safe_callback(task.on_message, event)
        self._safe_callback(self._event_callback, event)

    @staticmethod
    def _safe_callback(callback: Callable[[Any], None] | None, value: Any) -> None:
        if callback is None:
            return
        try:
            callback(value)
        except Exception:
            # Scheduler/UI callbacks are observers.  Their failure must never
            # leak process ownership or prevent completion persistence.
            return
