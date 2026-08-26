from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Mapping, Sequence


EXECUTION_BLOCK_PLAN_SCHEMA = "substar.execution-block-plan.v1"
DEFAULT_TARGET_SECONDS = 90.0
DEFAULT_MINIMUM_SECONDS = 75.0
DEFAULT_MAXIMUM_SECONDS = 100.0


@dataclass(frozen=True)
class BoundaryEvidence:
    alignment_index: int
    end: float
    distance_seconds: float
    gap_seconds: float
    speaker_change: bool
    low_volume: bool
    score: float


def _field(unit: Any, name: str, default: Any = None) -> Any:
    if isinstance(unit, Mapping):
        return unit.get(name, default)
    return getattr(unit, name, default)


def _text(unit: Any) -> str:
    return str(_field(unit, "text", _field(unit, "word", "")) or "").strip()


def _low_volume(unit: Any, following: Any) -> bool:
    if bool(_field(unit, "low_volume_after", False)):
        return True
    for name in ("boundary_rms_db", "rms_db", "volume_db"):
        values = [
            value
            for value in (_field(unit, name), _field(following, name))
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        if values and min(float(value) for value in values) <= -42.0:
            return True
    return False


def _speaker_change(unit: Any, following: Any) -> bool:
    left = str(_field(unit, "speaker_id", "") or "")
    right = str(_field(following, "speaker_id", "") or "")
    if not left or not right or left == right:
        return False
    left_confidence = float(_field(unit, "speaker_confidence", 1.0) or 0.0)
    right_confidence = float(_field(following, "speaker_confidence", 1.0) or 0.0)
    return min(left_confidence, right_confidence) >= 0.75


def _evidence(units: Sequence[Any], position: int, target: float) -> BoundaryEvidence:
    unit = units[position]
    following = units[position + 1]
    end = float(_field(unit, "end", 0.0))
    gap = max(0.0, float(_field(following, "start", end)) - end)
    speaker = _speaker_change(unit, following)
    quiet = _low_volume(unit, following)
    distance = abs(end - target)
    score = -distance
    score += 150.0 if speaker else 0.0
    score += min(gap, 2.0) * 70.0
    score += 65.0 if quiet else 0.0
    return BoundaryEvidence(
        alignment_index=int(_field(unit, "index", position)),
        end=end,
        distance_seconds=distance,
        gap_seconds=gap,
        speaker_change=speaker,
        low_volume=quiet,
        score=score,
    )


def balanced_target_times(units: Sequence[Any], target_seconds: float) -> list[float]:
    if not units:
        return []
    start = float(_field(units[0], "start", 0.0))
    finish = float(_field(units[-1], "end", start))
    duration = max(0.0, finish - start)
    target = max(1.0, float(target_seconds))
    if duration <= target:
        return []
    targets: list[float] = []
    boundary = start + target
    while boundary < finish:
        # Avoid manufacturing a tiny tail merely to keep a mathematically exact
        # interval. The final block may absorb up to one quarter of the target.
        if finish - boundary < target * 0.25:
            break
        targets.append(boundary)
        boundary += target
    return targets


def plan_execution_seams(
    units: Sequence[Any],
    *,
    target_seconds: float = DEFAULT_TARGET_SECONDS,
    search_radius_seconds: float | None = None,
    minimum_seconds: float | None = None,
    maximum_seconds: float | None = None,
    forbidden_after: Iterable[int] = (),
) -> tuple[list[int], list[BoundaryEvidence]]:
    """Choose exhaustive execution seams from observable evidence only.

    Candidate durations are measured from the start of the previous block.
    This prevents a locally attractive seam from manufacturing a tiny tail.
    Explicit callers retain their historical symmetric search window; the
    production default is the bounded 75--100 second profile.
    """

    if len(units) < 2:
        return [], []
    bounded_profile = minimum_seconds is not None and maximum_seconds is not None
    target_seconds = max(1.0, float(target_seconds))
    radius = (
        max(1.0, float(search_radius_seconds))
        if search_radius_seconds is not None
        else max(1.0, target_seconds / 6.0)
    )
    minimum = max(
        1.0,
        float(minimum_seconds)
        if minimum_seconds is not None
        else target_seconds - radius,
    )
    maximum = max(
        minimum,
        float(maximum_seconds)
        if maximum_seconds is not None
        else target_seconds + radius,
    )
    forbidden = {int(value) for value in forbidden_after}
    seams: list[int] = []
    selected: list[BoundaryEvidence] = []
    previous_position = -1
    block_start = float(_field(units[0], "start", 0.0))
    finish = float(_field(units[-1], "end", block_start))
    total_duration = max(0.0, finish - block_start)
    minimum_blocks = max(1, math.ceil(total_duration / maximum))
    maximum_blocks = max(1, math.floor(total_duration / minimum))
    balanced_blocks = None
    if bounded_profile and minimum_blocks <= maximum_blocks:
        balanced_blocks = min(
            maximum_blocks,
            max(minimum_blocks, round(total_duration / target_seconds)),
        )
    remaining_blocks = balanced_blocks
    while finish - block_start > maximum:
        target = (
            block_start + (finish - block_start) / remaining_blocks
            if remaining_blocks and remaining_blocks > 1
            else block_start + target_seconds
        )
        candidates = [
            (position, _evidence(units, position, target))
            for position in range(previous_position + 1, len(units) - 1)
            if int(_field(units[position], "index", position)) not in forbidden
            and minimum
            <= float(_field(units[position], "end", 0.0)) - block_start
            <= maximum
            and (
                (
                    minimum * (remaining_blocks - 1)
                    <= finish - float(_field(units[position + 1], "start", 0.0))
                    <= maximum * (remaining_blocks - 1)
                )
                if remaining_blocks and remaining_blocks > 1
                else finish - float(_field(units[position + 1], "start", 0.0)) >= minimum
            )
        ]
        if not candidates:
            candidates = [
                (position, _evidence(units, position, target))
                for position in range(previous_position + 1, len(units) - 1)
                if int(_field(units[position], "index", position)) not in forbidden
                and minimum
                <= float(_field(units[position], "end", 0.0)) - block_start
                <= maximum
            ]
        if not candidates:
            candidates = [
                (position, _evidence(units, position, target))
                for position in range(previous_position + 1, len(units) - 1)
                if int(_field(units[position], "index", position)) not in forbidden
            ]
            if not candidates:
                break
        position, evidence = max(
            candidates,
            key=lambda item: (item[1].score, -item[1].distance_seconds),
        )
        previous_position = position
        seams.append(evidence.alignment_index)
        selected.append(evidence)
        block_start = float(_field(units[position + 1], "start", evidence.end))
        if remaining_blocks is not None:
            remaining_blocks = max(1, remaining_blocks - 1)
    return seams, selected


def _bounded_execution_seams(
    units: Sequence[Any], *, target_seconds: float, minimum_seconds: float,
    maximum_seconds: float, forbidden_after: set[int],
) -> tuple[list[int], list[BoundaryEvidence]] | None:
    """Find a globally feasible bounded path before applying local evidence.

    The old greedy planner could pick several individually attractive seams
    and manufacture a tiny tail.  This DAG keeps every block inside the
    requested envelope whenever such a path exists; observable boundary
    evidence remains a secondary preference among feasible paths.
    """
    if len(units) < 2:
        return ([], [])
    candidates = [
        position for position, unit in enumerate(units[:-1])
        if int(_field(unit, "index", position)) not in forbidden_after
    ]
    terminal = len(units) - 1
    nodes = [*candidates, terminal]
    # node -> (cost, seam positions); -1 is the virtual position before input.
    paths: dict[int, tuple[float, list[int]]] = {-1: (0.0, [])}
    for node in nodes:
        best: tuple[float, list[int]] | None = None
        for previous, (prior_cost, prior_path) in list(paths.items()):
            if previous >= node:
                continue
            start_position = previous + 1
            duration = (
                float(_field(units[node], "end", 0.0))
                - float(_field(units[start_position], "start", 0.0))
            )
            if not minimum_seconds <= duration <= maximum_seconds:
                continue
            cost = prior_cost + abs(duration - target_seconds)
            if node != terminal:
                target = float(_field(units[start_position], "start", 0.0)) + target_seconds
                cost -= _evidence(units, node, target).score * 0.001
            path = prior_path if node == terminal else [*prior_path, node]
            candidate = (cost, path)
            if best is None or candidate < best:
                best = candidate
        if best is not None:
            paths[node] = best
    if terminal not in paths:
        return None
    seam_positions = paths[terminal][1]
    seams = [int(_field(units[position], "index", position)) for position in seam_positions]
    evidence = []
    previous = -1
    for position in seam_positions:
        start = float(_field(units[previous + 1], "start", 0.0))
        evidence.append(_evidence(units, position, start + target_seconds))
        previous = position
    return seams, evidence


def execution_block_plan(
    units: Sequence[Any], *,
    target_seconds: float = DEFAULT_TARGET_SECONDS,
    minimum_seconds: float = DEFAULT_MINIMUM_SECONDS,
    maximum_seconds: float = DEFAULT_MAXIMUM_SECONDS,
    allowed_after: Iterable[int] | None = None,
    forbidden_after: Iterable[int] = (),
    basis: str = "source_tokens",
) -> dict[str, Any]:
    allowed = None if allowed_after is None else {int(value) for value in allowed_after}
    forbidden = {int(value) for value in forbidden_after}
    if allowed is not None:
        forbidden.update(
            int(_field(unit, "index", position))
            for position, unit in enumerate(units[:-1])
            if int(_field(unit, "index", position)) not in allowed
        )
    bounded = _bounded_execution_seams(
        units,
        target_seconds=float(target_seconds),
        minimum_seconds=float(minimum_seconds),
        maximum_seconds=float(maximum_seconds),
        forbidden_after=forbidden,
    )
    if bounded is None:
        seams, evidence = plan_execution_seams(
            units,
            target_seconds=target_seconds,
            minimum_seconds=minimum_seconds,
            maximum_seconds=maximum_seconds,
            forbidden_after=forbidden,
        )
    else:
        seams, evidence = bounded
    positions = {int(_field(unit, "index", position)): position for position, unit in enumerate(units)}
    ranges: list[tuple[int, int]] = []
    left = 0
    for seam in seams:
        right = positions[seam]
        ranges.append((left, right))
        left = right + 1
    if units:
        ranges.append((left, len(units) - 1))
    blocks = [
        {
            "block_id": f"block_{number:04d}",
            "alignment_start": int(_field(units[start], "index", start)),
            "alignment_end": int(_field(units[end], "index", end)),
            "start": float(_field(units[start], "start", 0.0)),
            "end": float(_field(units[end], "end", 0.0)),
        }
        for number, (start, end) in enumerate(ranges, start=1)
    ]
    total_duration = (
        max(
            0.0,
            float(_field(units[-1], "end", 0.0))
            - float(_field(units[0], "start", 0.0)),
        )
        if units
        else 0.0
    )
    exceptions = []
    if total_duration > maximum_seconds:
        for block in blocks:
            duration = float(block["end"]) - float(block["start"])
            if duration < minimum_seconds or duration > maximum_seconds:
                exceptions.append({
                    "code": "execution_block_duration_out_of_range",
                    "block_id": block["block_id"],
                    "duration_seconds": duration,
                })
    return {
        "schema_version": EXECUTION_BLOCK_PLAN_SCHEMA,
        "basis": str(basis),
        "target_seconds": float(target_seconds),
        "minimum_seconds": float(minimum_seconds),
        "maximum_seconds": float(maximum_seconds),
        "boundaries_after": seams,
        "boundary_evidence": [item.__dict__ for item in evidence],
        "blocks": blocks,
        "exceptions": exceptions,
    }
