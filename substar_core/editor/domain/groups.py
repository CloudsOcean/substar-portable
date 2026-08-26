from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from substar_core.domain import (
    ChangeKind,
    ChangeProvenance,
    EditorDocument,
    SemanticGroup,
    stable_id,
)


def _segmentation_lineage(document: EditorDocument) -> dict[str, Any]:
    for change in document.changes:
        if change.operation == "build_from_segmentation":
            return dict(change.metadata)
    return {}


def initialize_segmentation_groups(document: EditorDocument) -> EditorDocument:
    """Derive deterministic editor groups from canonical segmentation lineage."""
    if document.groups:
        return document
    lineage = _segmentation_lineage(document)
    cue_lineage = {
        str(key): dict(value)
        for key, value in dict(lineage.get("cue_lineage", {})).items()
        if isinstance(value, Mapping)
    }
    original_rows = list(cue_lineage.values())
    source_by_id = {token.token_id: token for token in document.source_tokens}
    display_by_id = {token.token_id: token for token in document.display_tokens}
    lineage_created_at = next(
        (
            change.created_at
            for change in document.changes
            if change.operation == "build_from_segmentation"
        ),
        "1970-01-01T00:00:00.000+00:00",
    )
    initialization = ChangeProvenance(
        kind=ChangeKind.IMPORT,
        operation="initialize_segmentation_groups",
        actor="editor",
        created_at=lineage_created_at,
        metadata={"source": "build_from_segmentation.cue_lineage"},
    )
    specs: dict[str, dict[str, Any]] = {}
    cues = []
    for cue in document.cues:
        direct = cue_lineage.get(cue.cue_id)
        source_indexes = sorted(
            {
                source_by_id[source_id].index
                for display_id in cue.display_token_ids
                if display_id in display_by_id
                for source_id in display_by_id[display_id].source_token_ids
                if source_id in source_by_id
            }
        )
        overlaps: list[dict[str, Any]] = []
        if direct is not None:
            overlaps = [direct]
        elif source_indexes:
            start, end = source_indexes[0], source_indexes[-1]
            overlaps = [
                row
                for row in original_rows
                if isinstance(row.get("source_indexes"), list)
                and len(row["source_indexes"]) == 2
                and int(row["source_indexes"][1]) >= start
                and int(row["source_indexes"][0]) <= end
            ]
        meaning_ids = tuple(
            dict.fromkeys(
                str(value)
                for row in overlaps
                for value in row.get("semantic_group_ids", [])
            )
        )
        block_ids = tuple(
            dict.fromkeys(
                str(value)
                for row in overlaps
                for value in row.get("planning_block_ids", [])
            )
        )
        if meaning_ids:
            group_id = stable_id("grp", {"source_group_ids": meaning_ids})
            origin = "segmentation" if len(meaning_ids) == 1 else "merged"
            confidence = "high"
        else:
            group_id = stable_id("grp", {"manual_cue_id": cue.cue_id})
            origin = "manual"
            confidence = "low"
        spec = specs.setdefault(
            group_id,
            {
                "origin": origin,
                "source_group_ids": [],
                "execution_block_ids": [],
                "confidence": confidence,
            },
        )
        spec["source_group_ids"] = list(
            dict.fromkeys([*spec["source_group_ids"], *meaning_ids])
        )
        spec["execution_block_ids"] = list(
            dict.fromkeys([*spec["execution_block_ids"], *block_ids])
        )
        cues.append(replace(cue, group_id=group_id))
    groups = tuple(
        SemanticGroup(
            group_id=group_id,
            origin=spec["origin"],
            source_group_ids=tuple(spec["source_group_ids"]),
            execution_block_ids=tuple(spec["execution_block_ids"]),
            dirty_flags=(),
            migration_confidence=spec["confidence"],
            provenance=initialization,
        )
        for group_id, spec in specs.items()
    )
    return replace(document, cues=tuple(cues), groups=groups)
