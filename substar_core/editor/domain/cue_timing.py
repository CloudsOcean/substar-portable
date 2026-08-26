from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from substar_core.domain import (
    ChangeProvenance,
    DisplayCue,
    EditorDocument,
    EntityState,
    SemanticGroup,
)

from .cue_ordering import canonicalize_cue_order


class CueTimingError(ValueError):
    pass


MIN_CUE_SECONDS = 0.04
SHARED_BOUNDARY_EPSILON = 0.041


def smart_snap_search_minimum(
    *,
    previous_start: float | None,
    previous_end: float | None,
    current_start: float,
    previous_is_manual: bool = False,
) -> float:
    """Return the earliest safe smart-snap boundary for one Cue start.

    A touching pair owns one shared boundary.  Smart forward snapping may move
    that boundary left while retaining a minimum duration for the left Cue.
    Real gaps and manual Cues remain hard barriers, so onset detection never
    searches through unrelated or manually positioned material.
    """

    current = max(0.0, float(current_start))
    if previous_start is None or previous_end is None:
        return 0.0
    prior_start = max(0.0, float(previous_start))
    prior_end = max(prior_start, float(previous_end))
    if previous_is_manual or current - prior_end > SHARED_BOUNDARY_EPSILON:
        return min(current, prior_end)
    return min(current, prior_start + MIN_CUE_SECONDS)


@dataclass(frozen=True)
class CueTimeChange:
    cue_id: str
    start: float
    end: float
    expected_start: float | None = None
    expected_end: float | None = None

    def __post_init__(self) -> None:
        if not str(self.cue_id).strip():
            raise CueTimingError("cue time change needs a cue_id")
        object.__setattr__(self, "start", float(self.start))
        object.__setattr__(self, "end", float(self.end))
        if self.expected_start is not None:
            object.__setattr__(self, "expected_start", float(self.expected_start))
        if self.expected_end is not None:
            object.__setattr__(self, "expected_end", float(self.expected_end))
        if self.start < 0 or self.end <= self.start:
            raise CueTimingError("cue time must satisfy 0 <= start < end")


def _token_map(document: EditorDocument):
    return {token.token_id: token for token in document.display_tokens}


def _is_manual_cue(document: EditorDocument, cue: DisplayCue) -> bool:
    tokens = _token_map(document)
    return all(not tokens[token_id].source_token_ids for token_id in cue.display_token_ids)


def _mark_groups_dirty(
    groups: tuple[SemanticGroup, ...], group_ids: set[str | None]
) -> tuple[SemanticGroup, ...]:
    requested = {value for value in group_ids if value}
    return tuple(
        replace(
            group,
            dirty_flags=tuple(dict.fromkeys((*group.dirty_flags, "timing"))),
        )
        if group.group_id in requested
        else group
        for group in groups
    )


def _find_cue(document: EditorDocument, cue_id: str) -> DisplayCue:
    try:
        return next(cue for cue in document.cues if cue.cue_id == cue_id)
    except StopIteration as exc:
        raise CueTimingError(f"unknown cue: {cue_id}") from exc


def _check_expected(cue: DisplayCue, change: CueTimeChange) -> None:
    if change.expected_start is not None and abs(change.expected_start - cue.start) > 1e-9:
        raise CueTimingError(f"cue start changed: {cue.cue_id}")
    if change.expected_end is not None and abs(change.expected_end - cue.end) > 1e-9:
        raise CueTimingError(f"cue end changed: {cue.cue_id}")


def _validate_non_manual_timeline(document: EditorDocument, cues: Iterable[DisplayCue]) -> None:
    previous_end = -1.0
    for cue in canonicalize_cue_order(cues):
        if cue.state is EntityState.DELETED or _is_manual_cue(document, cue):
            continue
        if cue.start < previous_end - 1e-9:
            raise CueTimingError(
                f"final cue timeline overlaps or is out of order at {cue.cue_id}"
            )
        previous_end = cue.end


def _validate_changed_ranges(
    cues: Iterable[DisplayCue], changed_ids: set[str]
) -> None:
    """Reject overlaps introduced by this mutation without invalidating old data."""
    active = [cue for cue in cues if cue.state is not EntityState.DELETED]
    for cue in active:
        if cue.cue_id not in changed_ids:
            continue
        for other in active:
            if other.cue_id == cue.cue_id:
                continue
            if cue.start < other.end and other.start < cue.end:
                raise CueTimingError(
                    f"cue timeline overlaps at {cue.cue_id} and {other.cue_id}"
                )


def apply_cue_time(
    document: EditorDocument,
    change: CueTimeChange,
    provenance: ChangeProvenance,
) -> EditorDocument:
    cue = _find_cue(document, change.cue_id)
    _check_expected(cue, change)
    updated = replace(cue, start=change.start, end=change.end)
    cues = tuple(updated if item.cue_id == cue.cue_id else item for item in document.cues)
    _validate_non_manual_timeline(document, cues)
    _validate_changed_ranges(cues, {cue.cue_id})
    cues = canonicalize_cue_order(cues)
    return replace(
        document,
        cues=cues,
        groups=_mark_groups_dirty(document.groups, {cue.group_id}),
        changes=(*document.changes, provenance),
    )


def apply_cue_times(
    document: EditorDocument,
    changes: Iterable[CueTimeChange],
    provenance: ChangeProvenance,
) -> EditorDocument:
    prepared = tuple(changes)
    if not prepared:
        raise CueTimingError("set_cue_times needs at least one cue")
    if len({change.cue_id for change in prepared}) != len(prepared):
        raise CueTimingError("cue time change IDs must be unique")
    cue_map = {cue.cue_id: cue for cue in document.cues}
    replacements: dict[str, DisplayCue] = {}
    for change in prepared:
        cue = cue_map.get(change.cue_id)
        if cue is None:
            raise CueTimingError(f"unknown cue: {change.cue_id}")
        _check_expected(cue, change)
        replacements[change.cue_id] = replace(
            cue, start=change.start, end=change.end
        )
    cues = tuple(replacements.get(cue.cue_id, cue) for cue in document.cues)
    _validate_non_manual_timeline(document, cues)
    _validate_changed_ranges(cues, set(replacements))
    cues = canonicalize_cue_order(cues)
    return replace(
        document,
        cues=cues,
        groups=_mark_groups_dirty(
            document.groups,
            {cue_map[cue_id].group_id for cue_id in replacements},
        ),
        changes=(*document.changes, provenance),
    )
