from __future__ import annotations

from typing import Any, Mapping


AI_PROGRESS_SCHEMA = "substar.ai-stage-progress.v2"

_PHASE_BANDS: dict[str, tuple[float, float]] = {
    "planning": (0.0, 0.05),
    "executing": (0.05, 0.70),
    "repair": (0.70, 0.85),
    "validating": (0.85, 0.92),
    "materializing": (0.92, 0.97),
    "publishing": (0.97, 1.0),
    "completed": (1.0, 1.0),
}

_PHASE_LABELS = {
    "planning": "准备任务",
    "executing": "模型处理",
    "repair": "修复",
    "validating": "结果验收",
    "materializing": "生成可编辑结果",
    "publishing": "交付产物",
    "completed": "已交付",
}


def ai_progress(
    *,
    kind: str,
    phase: str,
    unit_label: str,
    planned: int = 0,
    completed: int = 0,
    accepted: int = 0,
    failed: int = 0,
    repair_planned: int = 0,
    repair_completed: int = 0,
    repair_accepted: int = 0,
    repair_failed: int = 0,
    problem_count: int = 0,
    detail: str = "",
    unit_kind: str | None = None,
) -> dict[str, Any]:
    """Return the common, monotonic presentation contract for model tasks."""

    if phase not in _PHASE_BANDS:
        raise ValueError(f"unsupported AI progress phase: {phase}")
    planned = max(0, int(planned))
    completed = min(planned, max(0, int(completed))) if planned else 0
    accepted = min(completed, max(0, int(accepted)))
    failed = min(completed, max(0, int(failed)))
    repair_planned = max(0, int(repair_planned))
    repair_completed = (
        min(repair_planned, max(0, int(repair_completed)))
        if repair_planned else 0
    )
    repair_accepted = min(repair_completed, max(0, int(repair_accepted)))
    repair_failed = min(repair_completed, max(0, int(repair_failed)))
    resolved_unit_kind = str(unit_kind or {
        "translation": "semantic_group",
        "calibration": "calibration_block",
        "segmentation": "segmentation_block",
    }.get(str(kind), "work_unit"))
    canonical_label = {
        "semantic_group": "个意义组",
        "calibration_block": "个校准块",
        "segmentation_block": "块",
    }.get(resolved_unit_kind, str(unit_label))

    low, high = _PHASE_BANDS[phase]
    if phase == "executing":
        fraction = completed / planned if planned else 0.0
    elif phase == "repair":
        fraction = repair_completed / repair_planned if repair_planned else 1.0
    elif phase in {"planning", "completed"}:
        fraction = 1.0 if phase == "completed" else 0.0
    else:
        fraction = 0.5
    progress = low + (high - low) * fraction

    if phase == "executing" and planned:
        message = f"{_PHASE_LABELS[phase]} {completed}/{planned} {canonical_label}"
    elif phase == "repair" and repair_planned:
        message = (
            f"{_PHASE_LABELS[phase]} {repair_completed}/{repair_planned} {canonical_label}"
            f" · 首轮通过 {accepted}/{planned}"
        )
    else:
        message = _PHASE_LABELS[phase]
    if detail:
        message += f" · {detail}"

    return {
        "schema_version": AI_PROGRESS_SCHEMA,
        "kind": str(kind),
        "phase": phase,
        "phase_label": _PHASE_LABELS[phase],
        "unit_kind": resolved_unit_kind,
        "unit_label": canonical_label,
        "progress": round(max(0.0, min(1.0, progress)), 6),
        "message": message,
        "units": {
            "planned": planned,
            "completed": completed,
            "accepted": accepted,
            "failed": failed,
            "repair_planned": repair_planned,
            "repair_completed": repair_completed,
            "repair_accepted": repair_accepted,
            "repair_failed": repair_failed,
        },
        "problem_count": max(0, int(problem_count)),
        "steps": [
            {"id": name, "label": _PHASE_LABELS[name]}
            for name in (
                "executing", "repair", "validating",
                "materializing", "publishing", "completed",
            )
        ],
    }


def progress_from_mapping(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    progress = value.get("ai_progress")
    if not isinstance(progress, Mapping):
        return None
    schema = progress.get("schema_version")
    if schema != AI_PROGRESS_SCHEMA:
        return None
    return dict(progress)
