from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from substar_core.contracts.editor_document import (
    build_editor_document,
    source_tokens_from_asr,
)
from substar_core.domain import EditorDocument
from substar_core.domain import ChangeKind, ChangeProvenance
from substar_core.document_operations import apply_document_operation
from substar_core.segmentation.execution_planner import execution_block_plan


def validate_editor_document(document: EditorDocument) -> EditorDocument:
    """Reject historical lineage instead of rewriting it at runtime."""

    historical = [
        change.operation
        for change in document.changes
        if change.operation in {"build_from_split_stages", "normalize_legacy_groups"}
    ]
    if historical:
        raise ValueError(f"historical editor lineage is not accepted: {historical}")
    return document


def build_sentence_boundary_document(
    evidence: Mapping[str, Any], *, source_asset_id: str
) -> EditorDocument:
    units = list(evidence["units"])
    source_tokens = source_tokens_from_asr(units, source_asset_id=source_asset_id)
    final_index = int(units[-1]["index"])
    boundaries: list[int] = []
    for position, unit in enumerate(units[:-1]):
        following = units[position + 1]
        if bool(unit.get("sentence_end")) or (
            unit.get("sentence_id") is not None
            and following.get("sentence_id") is not None
            and unit.get("sentence_id") != following.get("sentence_id")
        ):
            boundaries.append(int(unit["index"]))
    if not boundaries and final_index > int(units[0]["index"]):
        boundaries = [
            int(units[index]["index"])
            for index in range(len(units) - 1)
            if float(units[index + 1]["start"]) - float(units[index]["end"]) >= 0.65
        ]
    split_input_plan = execution_block_plan(
        units, target_seconds=90, basis="asr_source_boundaries"
    )
    document = build_editor_document(
        source_tokens=source_tokens,
        source_kind="asr",
        source_asset_id=source_asset_id,
        execution_plan={
            "blocks": split_input_plan["blocks"],
            "boundaries_after": split_input_plan["boundaries_after"],
            "skipped_reason": None,
        },
        semantic_grouping={
            "protections": [],
            "meaning_groups": [],
            "review_regions": [],
        },
        cue_layout={"display_breaks": boundaries},
    )
    return validate_editor_document(document)


def build_reference_script_document(
    material: Mapping[str, Any],
    *,
    source_asset_id: str,
    display_breaks: list[int],
    reference_report: Mapping[str, Any],
) -> EditorDocument:
    units = list(material["units"])
    source_tokens = source_tokens_from_asr(units, source_asset_id=source_asset_id)
    split_input_plan = execution_block_plan(
        units, target_seconds=90, basis="reference_script"
    )
    document = build_editor_document(
        source_tokens=source_tokens,
        source_kind="asr",
        source_asset_id=source_asset_id,
        execution_plan={
            "blocks": split_input_plan["blocks"],
            "boundaries_after": split_input_plan["boundaries_after"],
            "skipped_reason": None,
        },
        semantic_grouping={
            "protections": [],
            "meaning_groups": [],
            "review_regions": [],
        },
        cue_layout={"display_breaks": display_breaks},
        generation_mode="reference_script",
    )
    source_to_display = {
        source.index: next(
            token.token_id
            for token in document.display_tokens
            if source.token_id in token.source_token_ids
        )
        for source in document.source_tokens
    }
    reference_changes: list[dict[str, Any]] = []
    replacements = []
    for item in reference_report.get("replacements", []):
        source_index = int(item["source_index"])
        token_id = source_to_display[source_index]
        status = str(item.get("status", "applied"))
        if status == "applied":
            replacements.append(
                {
                    "token_id": token_id,
                    "text": str(item["after"]),
                    "expected_text": str(item["before"]),
                }
            )
        reference_changes.append(
            {
                "change_id": f"reference-replace-{int(item['reference_index'])}",
                "type": "replace",
                "token_ids": [token_id],
                "source_indexes": [source_index],
                "before": str(item["before"]),
                "after": str(item["after"]),
                "status": status,
                "lexical_match": bool(item.get("lexical_match")),
            }
        )
    if replacements:
        document = apply_document_operation(
            document,
            {
                "operation_id": "op_reference_initial_replacements",
                "type": "batch_replace",
                "payload": {
                    "replacements": replacements,
                    "provenance": {
                        "kind": "import",
                        "operation": "reference_manuscript_replace",
                        "actor": "reference-manuscript",
                        "metadata": {"reference": True},
                    },
                },
            },
        )

    insertion_anchor_by_source: dict[int, str | None] = {}
    for item in sorted(
        reference_report.get("insertions", []),
        key=lambda value: int(value["reference_index"]),
    ):
        source_index = int(item["after_source_index"])
        placement = str(item.get("placement", "left"))
        if source_index < 0:
            cue = document.cues[0]
            anchor_id = insertion_anchor_by_source.get(source_index)
        else:
            anchor_id = insertion_anchor_by_source.get(
                source_index, source_to_display[source_index]
            )
            anchor_cue = next(
                cue
                for cue in document.cues
                if anchor_id in cue.display_token_ids
            )
            cue = anchor_cue
            if (
                placement == "right"
                and source_index in display_breaks
                and source_index not in insertion_anchor_by_source
            ):
                cue_position = document.cues.index(anchor_cue)
                if cue_position + 1 < len(document.cues):
                    cue = document.cues[cue_position + 1]
                    anchor_id = None
        reference_index = int(item["reference_index"])
        operation_id = f"op_reference_initial_insert_{reference_index}"
        document = apply_document_operation(
            document,
            {
                "operation_id": operation_id,
                "type": "insert",
                "payload": {
                    "cue_id": cue.cue_id,
                    "after_token_id": anchor_id,
                    "token": {
                        "text": str(item["text"]),
                        "original_text": str(item["text"]),
                        "source_token_ids": [],
                    },
                    "provenance": {
                        "kind": "manual",
                        "operation": "reference_manuscript_insert",
                        "actor": "reference-manuscript",
                        "metadata": {
                            "reference": True,
                            "timing_source": "reference_estimated",
                        },
                    },
                },
            },
        )
        inserted = next(
            token
            for token in document.display_tokens
            if token.provenance.metadata.get("operation_id") == operation_id
        )
        insertion_anchor_by_source[source_index] = inserted.token_id
        reference_changes.append(
            {
                "change_id": f"reference-insert-{reference_index}",
                "type": "insert",
                "token_ids": [inserted.token_id],
                "source_indexes": [source_index] if source_index >= 0 else [],
                "before": "",
                "after": str(item["text"]),
                "status": "applied",
                "timing_source": "reference_estimated",
                "placement": placement,
            }
        )

    for item in reference_report.get("retained_source", []):
        source_index = int(item["source_index"])
        reference_changes.append(
            {
                "change_id": f"reference-retained-{source_index}",
                "type": "retained_source",
                "token_ids": [source_to_display[source_index]],
                "source_indexes": [source_index],
                "before": str(item["before"]),
                "after": "",
                "status": "retained",
                "reason": str(item.get("reason", "reference_omitted")),
            }
        )

    audit = ChangeProvenance(
        kind=ChangeKind.IMPORT,
        operation="reference_manuscript_alignment",
        actor="reference-manuscript",
        metadata={
            "reference": True,
            "quality": str(reference_report.get("quality", "failed")),
            "similarity": float(reference_report.get("similarity", 0.0)),
            "break_symbols": str(reference_report.get("break_symbols", "")),
            "reference_changes": reference_changes,
            "replacement_count": len(replacements),
            "suggested_replacement_count": sum(
                1
                for item in reference_report.get("replacements", [])
                if str(item.get("status", "applied")) == "suggested"
            ),
            "insertion_count": len(reference_report.get("insertions", [])),
            "retained_source_count": len(reference_report.get("retained_source", [])),
        },
    )
    document = replace(document, changes=(*document.changes, audit))
    return validate_editor_document(document)
