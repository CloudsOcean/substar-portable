from __future__ import annotations

from pathlib import Path

from ..artifacts import atomic_write_json
from ..contracts.editor_document import (
    build_editor_document,
    source_tokens_from_asr,
    source_tokens_from_jianying,
)
from ..domain import ChangeKind, ChangeProvenance, DocumentRevision
from .material import extract_alignment
from ..storage import ProjectStore


def materialize_sentence_boundary_project(
    job_dir: Path,
    *,
    source_kind: str,
    source_asset_id: str,
    project_id: str,
) -> DocumentRevision:
    """Create an editor project directly from the recognizer's sentence cues.

    This is the explicit "segmentation disabled" route.  It does not invent
    model cuts or silently invoke another task; sentence boundaries emitted by ASR
    (or imported from Jianying) become the initial editor cues verbatim.
    """

    units = extract_alignment((job_dir / "chatbox_material.md").read_text(encoding="utf-8"))
    source_tokens = (
        source_tokens_from_jianying(units, source_asset_id=source_asset_id)
        if source_kind == "jianying"
        else source_tokens_from_asr(units, source_asset_id=source_asset_id)
    )
    final_index = units[-1].index
    boundaries: list[int] = []
    for position, unit in enumerate(units[:-1]):
        next_unit = units[position + 1]
        if unit.sentence_end or (
            unit.sentence_id is not None
            and next_unit.sentence_id is not None
            and unit.sentence_id != next_unit.sentence_id
        ):
            boundaries.append(unit.index)
    if not boundaries and final_index > units[0].index:
        # If recognition has no sentence metadata, use deterministic silence
        # gaps instead of fabricating timing or invoking semantic grouping.
        boundaries = [
            units[index].index
            for index in range(len(units) - 1)
            if units[index + 1].start - units[index].end >= 0.65
        ]
    document = build_editor_document(
        source_tokens=source_tokens,
        source_kind=source_kind,
        source_asset_id=source_asset_id,
        execution_plan={
            "blocks": [{
                "block_id": "recognition-boundaries",
                "alignment_start": units[0].index,
                "alignment_end": final_index,
            }],
            "boundaries_after": [],
            "skipped_reason": "segmentation_disabled",
        },
        semantic_grouping={
            "protections": [], "meaning_groups": [],
            "review_regions": [],
        },
        cue_layout={"display_breaks": boundaries},
    )
    project_path = job_dir / "project"
    store = ProjectStore.open(project_path) if (project_path / "manifest.json").is_file() else ProjectStore.create(project_path, project_id=project_id)
    latest = store.load_latest()
    revision = store.save(
        document,
        provenance=ChangeProvenance(
            kind=ChangeKind.IMPORT,
            operation="ingest_boundaries_initial_document",
            actor="workbench",
            metadata={"source_kind": source_kind, "segmentation_enabled": False},
        ),
        expected_revision_id=latest.revision_id if latest is not None else None,
    )
    stage_dir = job_dir / "segmentation"
    stage_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(stage_dir / "editor_document.json", document.to_dict())
    atomic_write_json(stage_dir / "editor_revision.json", {
        "schema_version": "substar.editor-revision-pointer.v1",
        "project_id": project_id,
        "revision_id": revision.revision_id,
        "revision_number": revision.revision_number,
        "document_hash": document.content_hash(),
    })
    return revision
