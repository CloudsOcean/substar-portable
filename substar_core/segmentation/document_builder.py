from __future__ import annotations

import unicodedata
from dataclasses import replace
from difflib import SequenceMatcher
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


def attach_semantic_reference_audit(
    document: EditorDocument,
    reference_report: Mapping[str, Any],
    reference_suggestions: list[Mapping[str, Any]] | None = None,
) -> EditorDocument:
    """Attach reference differences after semantic grouping without changing cues."""

    def marker_key(value: object) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return "".join(char for char in normalized if char.isalnum())

    reference_rows = [
        raw
        for raw in reference_report.get("provenance", [])
        if isinstance(raw, Mapping)
        and not bool(raw.get("reference_only"))
        and marker_key(raw.get("reference_text"))
    ]
    source_rows = [
        token for token in document.source_tokens if marker_key(token.text)
    ]
    matcher = SequenceMatcher(
        None,
        [marker_key(raw.get("reference_text")) for raw in reference_rows],
        [marker_key(token.text) for token in source_rows],
        autojunk=False,
    )
    source_by_reference_index: dict[int, Any] = {}
    for reference_start, source_start, size in matcher.get_matching_blocks():
        for offset in range(size):
            source_by_reference_index[
                int(reference_rows[reference_start + offset]["reference_index"])
            ] = source_rows[source_start + offset]
    display_by_source_id = {
        source_id: token
        for token in document.display_tokens
        for source_id in token.source_token_ids
    }
    change_type_by_reference_index: dict[int, str] = {}
    for raw in reference_report.get("changes", []):
        if not isinstance(raw, Mapping):
            continue
        token_range = raw.get("reference_token_range")
        if not isinstance(token_range, (list, tuple)) or len(token_range) != 2:
            continue
        start, end = int(token_range[0]), int(token_range[1])
        if start > end:
            continue
        kind = str(raw.get("kind") or "replace")
        source_range = raw.get("source_token_range")
        source_count = 0
        if isinstance(source_range, (list, tuple)) and len(source_range) == 2:
            source_start, source_end = int(source_range[0]), int(source_range[1])
            source_count = max(0, source_end - source_start + 1)
        for offset, reference_index in enumerate(range(start, end + 1)):
            change_type_by_reference_index[reference_index] = (
                "insert" if kind == "replace" and offset >= source_count else kind
            )

    reference_changes: list[dict[str, Any]] = []
    for raw in reference_report.get("provenance", []):
        if not isinstance(raw, Mapping) or not bool(raw.get("changed")):
            continue
        reference_index = int(raw["reference_index"])
        source_token = source_by_reference_index.get(reference_index)
        if source_token is None:
            continue
        display_token = display_by_source_id.get(source_token.token_id)
        if display_token is None:
            continue
        kind = change_type_by_reference_index.get(reference_index, "replace")
        before = "" if kind == "insert" else str(raw.get("source_text") or "")
        after = display_token.text
        reference_changes.append(
            {
                "change_id": f"reference-{kind}-{reference_index}",
                "type": "insert" if kind == "insert" else "replace",
                "token_ids": [display_token.token_id],
                "source_indexes": [source_token.index],
                "before": before,
                "after": after,
                "status": "applied",
            }
        )

    insertion_anchor_by_gap: dict[tuple[int, int], str] = {}
    source_to_display = {
        source.index: display
        for source in document.source_tokens
        for display in document.display_tokens
        if source.token_id in display.source_token_ids
    }
    for suggestion in reference_suggestions or []:
        after_index = int(suggestion.get("after_index", -1))
        before_index = int(suggestion.get("before_index", after_index + 1))
        after_token = source_to_display.get(after_index)
        before_token = source_to_display.get(before_index)
        after_cue = next(
            (
                cue for cue in document.cues
                if after_token is not None and after_token.token_id in cue.display_token_ids
            ),
            None,
        )
        before_cue = next(
            (
                cue for cue in document.cues
                if before_token is not None and before_token.token_id in cue.display_token_ids
            ),
            None,
        )
        if before_cue is not None and before_cue is not after_cue:
            cue = before_cue
            anchor_id = None
        else:
            cue = after_cue or before_cue or document.cues[0]
            anchor_id = (
                insertion_anchor_by_gap.get((after_index, before_index))
                or (after_token.token_id if after_token is not None else None)
            )
        reference_index = int(suggestion["reference_index"])
        operation_id = f"op_reference_semantic_insert_{reference_index}"
        document = apply_document_operation(
            document,
            {
                "operation_id": operation_id,
                "type": "insert",
                "payload": {
                    "cue_id": cue.cue_id,
                    "after_token_id": anchor_id,
                    "token": {
                        "text": str(suggestion["text"]),
                        "original_text": str(suggestion["text"]),
                        "source_token_ids": [],
                    },
                    "provenance": {
                        "kind": "manual",
                        "operation": "reference_manuscript_insert",
                        "actor": "reference-manuscript",
                        "metadata": {
                            "reference": True,
                            "timing_source": "cue_inherited",
                        },
                    },
                },
            },
        )
        inserted = next(
            token for token in document.display_tokens
            if token.provenance.metadata.get("operation_id") == operation_id
        )
        insertion_anchor_by_gap[(after_index, before_index)] = inserted.token_id
        document = apply_document_operation(
            document,
            {
                "operation_id": f"{operation_id}_default_deleted",
                "type": "delete",
                "payload": {
                    "token_ids": [inserted.token_id],
                    "provenance": {
                        "kind": "import",
                        "operation": "reference_manuscript_insertions_default_deleted",
                        "actor": "reference-manuscript",
                        "metadata": {"reference": True},
                    },
                },
            },
        )
        reference_changes.append(
            {
                "change_id": f"reference-insert-{reference_index}",
                "type": "insert",
                "token_ids": [inserted.token_id],
                "source_indexes": [],
                "before": "",
                "after": str(suggestion["text"]),
                "status": "deleted",
                "timing_source": "cue_inherited",
            }
        )

    if not reference_changes:
        return document
    audit = ChangeProvenance(
        kind=ChangeKind.IMPORT,
        operation="reference_manuscript_alignment",
        actor="reference-manuscript",
        metadata={
            "reference": True,
            "mode": "semantic",
            "similarity": float(reference_report.get("similarity", 0.0)),
            "reference_changes": reference_changes,
            "replacement_count": sum(
                1 for item in reference_changes if item["type"] == "replace"
            ),
            "insertion_count": sum(
                1 for item in reference_changes if item["type"] == "insert"
            ),
        },
    )
    return validate_editor_document(
        replace(document, changes=(*document.changes, audit))
    )


def apply_semantic_display_projection(
    document: EditorDocument,
    projection: list[Mapping[str, Any]],
) -> EditorDocument:
    """Restore reference punctuation after AI has selected cue boundaries."""

    text_by_source_index = {
        int(row["index"]): str(row["text"]).strip()
        for row in projection
        if str(row.get("text") or "").strip()
    }
    source_index_by_id = {
        token.token_id: token.index for token in document.source_tokens
    }
    changed_count = 0
    display_tokens = []
    for token in document.display_tokens:
        projected_parts = [
            text_by_source_index[source_index_by_id[source_id]]
            for source_id in token.source_token_ids
            if source_id in source_index_by_id
            and source_index_by_id[source_id] in text_by_source_index
        ]
        projected = " ".join(projected_parts).strip()
        if not projected or projected == token.text:
            display_tokens.append(token)
            continue
        display_tokens.append(replace(token, text=projected))
        changed_count += 1
    if not changed_count:
        return document
    audit = ChangeProvenance(
        kind=ChangeKind.IMPORT,
        operation="reference_manuscript_display_projection",
        actor="reference-manuscript",
        metadata={
            "reference": True,
            "mode": "semantic",
            "projected_token_count": changed_count,
            "cue_boundaries_changed": False,
        },
    )
    return validate_editor_document(
        replace(
            document,
            display_tokens=tuple(display_tokens),
            changes=(*document.changes, audit),
        )
    )


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
    reference_insertion_ids: list[str] = []
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
        reference_insertion_ids.append(inserted.token_id)
        insertion_anchor_by_source[source_index] = inserted.token_id
        reference_changes.append(
            {
                "change_id": f"reference-insert-{reference_index}",
                "type": "insert",
                "token_ids": [inserted.token_id],
                "source_indexes": [source_index] if source_index >= 0 else [],
                "before": "",
                "after": str(item["text"]),
                "status": "deleted",
                "timing_source": "reference_estimated",
                "placement": placement,
            }
        )

    if reference_insertion_ids:
        document = apply_document_operation(
            document,
            {
                "operation_id": "op_reference_initial_insertions_deleted",
                "type": "delete",
                "payload": {
                    "token_ids": reference_insertion_ids,
                    "provenance": {
                        "kind": "import",
                        "operation": "reference_manuscript_insertions_default_deleted",
                        "actor": "reference-manuscript",
                        "metadata": {"reference": True},
                    },
                },
            },
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
