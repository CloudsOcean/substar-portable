from __future__ import annotations

from enum import Enum
from typing import Mapping


EDITOR_AI_TASK_SCHEMA = "substar.editor-ai-task.v1"


class EditorAiTaskKind(str, Enum):
    CALIBRATION = "calibration"
    TRANSLATION = "translation"


class EditorAiTaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_ISSUES = "succeeded_with_issues"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


ACTIVE_EDITOR_AI_TASK_STATES = frozenset(
    {
        EditorAiTaskState.QUEUED,
        EditorAiTaskState.RUNNING,
        EditorAiTaskState.CANCELLING,
    }
)

TERMINAL_EDITOR_AI_TASK_STATES = frozenset(
    {
        EditorAiTaskState.SUCCEEDED,
        EditorAiTaskState.SUCCEEDED_WITH_ISSUES,
        EditorAiTaskState.FAILED,
        EditorAiTaskState.CANCELLED,
        EditorAiTaskState.INTERRUPTED,
    }
)

ALLOWED_EDITOR_AI_TASK_TRANSITIONS: Mapping[
    EditorAiTaskState, frozenset[EditorAiTaskState]
] = {
    EditorAiTaskState.QUEUED: frozenset(
        {
            EditorAiTaskState.RUNNING,
            EditorAiTaskState.CANCELLING,
            EditorAiTaskState.CANCELLED,
        }
    ),
    EditorAiTaskState.RUNNING: frozenset(
        {
            EditorAiTaskState.CANCELLING,
            EditorAiTaskState.SUCCEEDED,
            EditorAiTaskState.SUCCEEDED_WITH_ISSUES,
            EditorAiTaskState.FAILED,
            EditorAiTaskState.INTERRUPTED,
        }
    ),
    EditorAiTaskState.CANCELLING: frozenset(
        {
            EditorAiTaskState.CANCELLED,
            EditorAiTaskState.SUCCEEDED,
            EditorAiTaskState.SUCCEEDED_WITH_ISSUES,
            EditorAiTaskState.FAILED,
            EditorAiTaskState.INTERRUPTED,
        }
    ),
    EditorAiTaskState.SUCCEEDED: frozenset(),
    EditorAiTaskState.SUCCEEDED_WITH_ISSUES: frozenset(),
    EditorAiTaskState.FAILED: frozenset(),
    EditorAiTaskState.CANCELLED: frozenset(),
    EditorAiTaskState.INTERRUPTED: frozenset(),
}


class EditorAiTaskStateError(ValueError):
    pass


def _state(value: str | EditorAiTaskState) -> EditorAiTaskState:
    try:
        return value if isinstance(value, EditorAiTaskState) else EditorAiTaskState(value)
    except ValueError as exc:
        raise EditorAiTaskStateError(f"unknown editor AI task state: {value!r}") from exc


def require_editor_ai_task_transition(
    source: str | EditorAiTaskState,
    target: str | EditorAiTaskState,
) -> None:
    source_state = _state(source)
    target_state = _state(target)
    if target_state not in ALLOWED_EDITOR_AI_TASK_TRANSITIONS[source_state]:
        raise EditorAiTaskStateError(
            f"editor AI task cannot transition from {source_state.value} "
            f"to {target_state.value}"
        )


def editor_ai_task_holds_lock(value: str | EditorAiTaskState) -> bool:
    return _state(value) in ACTIVE_EDITOR_AI_TASK_STATES

