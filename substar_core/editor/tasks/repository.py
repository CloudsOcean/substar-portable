from __future__ import annotations

import contextlib
import contextvars
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from substar_core.artifacts import atomic_write_json

from .contracts import (
    EDITOR_AI_TASK_SCHEMA,
    EditorAiTaskKind,
    EditorAiTaskState,
    editor_ai_task_holds_lock,
    require_editor_ai_task_transition,
)


class EditorAiTaskConflict(RuntimeError):
    pass


class EditorAiTaskCancelled(RuntimeError):
    pass


_LOCKS_GUARD = threading.Lock()
_PROJECT_LOCKS: dict[str, threading.RLock] = {}
_OWNED_TASK_IDS: set[str] = set()
_CANCEL_EVENTS: dict[str, threading.Event] = {}
_CURRENT_TASK_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "substar_current_editor_ai_task_id", default=None
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _lock(project_directory: Path) -> threading.RLock:
    key = str(project_directory.resolve()).casefold()
    with _LOCKS_GUARD:
        return _PROJECT_LOCKS.setdefault(key, threading.RLock())


def task_path(project_directory: Path) -> Path:
    return project_directory / "editor_ai_task.json"


def _read(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EditorAiTaskConflict("editor AI task state is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema_version") != EDITOR_AI_TASK_SCHEMA:
        raise EditorAiTaskConflict("editor AI task state contract is invalid")
    return value


def load_task(
    project_directory: Path, *, reconcile_orphan: bool = True
) -> dict[str, Any] | None:
    path = task_path(project_directory)
    with _lock(project_directory):
        value = _read(path)
        if (
            value is not None
            and reconcile_orphan
            and editor_ai_task_holds_lock(str(value["state"]))
            and str(value["task_id"]) not in _OWNED_TASK_IDS
        ):
            value = _transition_unlocked(
                path,
                value,
                EditorAiTaskState.INTERRUPTED,
                error={
                    "code": "backend_restarted",
                    "message": "The backend restarted while this editor AI task was active.",
                },
            )
        return value


def start_task(
    project_directory: Path,
    *,
    project_id: str,
    kind: EditorAiTaskKind,
    based_on_revision_id: str,
) -> dict[str, Any]:
    path = task_path(project_directory)
    with _lock(project_directory):
        previous = _read(path)
        if previous is not None and editor_ai_task_holds_lock(str(previous["state"])):
            if str(previous["task_id"]) in _OWNED_TASK_IDS:
                raise EditorAiTaskConflict(
                    f"{previous['kind']} task {previous['task_id']} already locks this project"
                )
            _transition_unlocked(
                path,
                previous,
                EditorAiTaskState.INTERRUPTED,
                error={
                    "code": "backend_restarted",
                    "message": "The backend restarted while this editor AI task was active.",
                },
            )
        now = _now()
        task_id = f"editor_ai_{uuid.uuid4().hex}"
        value = {
            "schema_version": EDITOR_AI_TASK_SCHEMA,
            "task_id": task_id,
            "project_id": project_id,
            "kind": kind.value,
            "state": EditorAiTaskState.RUNNING.value,
            "locks_editor": True,
            "based_on_revision_id": based_on_revision_id,
            "result_revision_id": None,
            "created_at": now,
            "started_at": now,
            "finished_at": None,
            "cancel_requested_at": None,
            "error": None,
        }
        atomic_write_json(path, value)
        _OWNED_TASK_IDS.add(task_id)
        _CANCEL_EVENTS[task_id] = threading.Event()
        return value


def _transition_unlocked(
    path: Path,
    value: dict[str, Any],
    state: EditorAiTaskState,
    *,
    result_revision_id: str | None = None,
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    require_editor_ai_task_transition(str(value["state"]), state)
    updated = dict(value)
    updated.update(
        state=state.value,
        locks_editor=editor_ai_task_holds_lock(state),
        result_revision_id=result_revision_id,
        finished_at=None if editor_ai_task_holds_lock(state) else _now(),
        error=error,
    )
    if state is EditorAiTaskState.CANCELLING:
        updated["cancel_requested_at"] = _now()
    atomic_write_json(path, updated)
    if not editor_ai_task_holds_lock(state):
        _OWNED_TASK_IDS.discard(str(updated["task_id"]))
        _CANCEL_EVENTS.pop(str(updated["task_id"]), None)
    return updated


def request_task_cancellation(project_directory: Path) -> dict[str, Any]:
    path = task_path(project_directory)
    with _lock(project_directory):
        current = _read(path)
        if current is None:
            raise EditorAiTaskConflict("editor AI task does not exist")
        state = EditorAiTaskState(str(current["state"]))
        if state in {EditorAiTaskState.QUEUED, EditorAiTaskState.RUNNING}:
            current = _transition_unlocked(
                path, current, EditorAiTaskState.CANCELLING
            )
        elif state is not EditorAiTaskState.CANCELLING:
            return current
        _CANCEL_EVENTS.setdefault(
            str(current["task_id"]), threading.Event()
        ).set()
        return current


def current_task_id() -> str | None:
    return _CURRENT_TASK_ID.get()


def task_cancellation_requested(task_id: str | None = None) -> bool:
    resolved = task_id or _CURRENT_TASK_ID.get()
    if not resolved:
        return False
    event = _CANCEL_EVENTS.get(str(resolved))
    return bool(event and event.is_set())


def raise_if_task_cancelled(task_id: str | None = None) -> None:
    if task_cancellation_requested(task_id):
        raise EditorAiTaskCancelled("editor AI task was cancelled")


def finish_task(
    project_directory: Path,
    task_id: str,
    state: EditorAiTaskState,
    *,
    result_revision_id: str | None = None,
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    path = task_path(project_directory)
    with _lock(project_directory):
        current = _read(path)
        if current is None or current.get("task_id") != task_id:
            raise EditorAiTaskConflict("editor AI task ownership changed")
        return _transition_unlocked(
            path,
            current,
            state,
            result_revision_id=result_revision_id,
            error=error,
        )


def assert_editor_write_allowed(project_directory: Path) -> None:
    current_task_id = _CURRENT_TASK_ID.get()
    task = load_task(project_directory)
    if (
        task is not None
        and editor_ai_task_holds_lock(str(task["state"]))
        and task.get("task_id") != current_task_id
    ):
        raise EditorAiTaskConflict(
            f"{task['kind']} task {task['task_id']} currently locks this project"
        )


@contextlib.contextmanager
def task_context(task_id: str) -> Iterator[None]:
    token = _CURRENT_TASK_ID.set(task_id)
    try:
        yield
    finally:
        _CURRENT_TASK_ID.reset(token)
