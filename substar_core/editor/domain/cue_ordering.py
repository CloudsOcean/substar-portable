from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from substar_core.domain import DisplayCue, DocumentValidationError, EditorDocument


def cue_order_key(cue: DisplayCue, stable_position: int) -> tuple[float, float, int, str]:
    """Return the canonical time order without consulting Group membership."""
    return (float(cue.start), float(cue.end), int(stable_position), str(cue.cue_id))


def canonicalize_cue_order(cues: Iterable[DisplayCue]) -> tuple[DisplayCue, ...]:
    """Stably order cues by time and rebuild their positional indexes."""
    current = tuple(cues)
    positions = {cue.cue_id: position for position, cue in enumerate(current)}
    ordered = sorted(
        current,
        key=lambda cue: cue_order_key(cue, positions.get(cue.cue_id, len(current))),
    )
    return tuple(
        cue if cue.index == index else replace(cue, index=index)
        for index, cue in enumerate(ordered)
    )


def is_canonical_cue_order(cues: Iterable[DisplayCue]) -> bool:
    current = tuple(cues)
    canonical = canonicalize_cue_order(current)
    return all(
        before.cue_id == after.cue_id and before.index == after.index
        for before, after in zip(current, canonical)
    ) and len(current) == len(canonical)


def assert_canonical_cue_order(cues: Iterable[DisplayCue]) -> None:
    if not is_canonical_cue_order(cues):
        raise DocumentValidationError(
            "cues must be stored in canonical time order with positional indexes"
        )


def canonicalize_document_cues(document: EditorDocument) -> EditorDocument:
    cues = canonicalize_cue_order(document.cues)
    if cues == document.cues:
        return document
    return replace(document, cues=cues)
