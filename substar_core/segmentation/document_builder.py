from __future__ import annotations

import unicodedata
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
from substar_core.language_layout import layout_tokens, editor_token_fragments
from substar_core.segmentation.input_contract import _unpunctuated_word


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
    source_by_reference_index: dict[int, Any] = {}
    sources_by_reference_index: dict[int, list[Any]] = {}
    source_cursor = 0
    for raw in reference_rows:
        fragments = editor_token_fragments(_unpunctuated_word(raw["reference_text"]))
        if fragments:
            if source_cursor >= len(source_rows):
                raise ValueError("reference audit exceeds the semantic source ledger")
            source_by_reference_index[int(raw["reference_index"])] = source_rows[source_cursor]
            sources_by_reference_index[int(raw["reference_index"])] = source_rows[source_cursor:source_cursor + len(fragments)]
            source_cursor += len(fragments)
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
    for raw in reference_rows:
        if not raw.get("retained_source"):
            continue
        source_token = source_by_reference_index.get(int(raw["reference_index"]))
        display_token = display_by_source_id.get(source_token.token_id) if source_token else None
        if display_token:
            reference_changes.append({
                "change_id": f"reference-retained-{raw['reference_index']}",
                "type": "retained_source", "token_ids": [display_token.token_id],
                "source_indexes": [source_token.index], "before": raw["source_text"],
                "after": "", "status": "retained",
            })
    grouped_rows: dict[int, list[Mapping[str, Any]]] = {}
    for raw in reference_rows:
        if raw.get("replacement_group") is not None:
            grouped_rows.setdefault(int(raw["replacement_group"]), []).append(raw)
    grouped_indexes: set[int] = set()
    for group_index, rows in grouped_rows.items():
        members = list({token.token_id: token for row in rows
                        for source in sources_by_reference_index.get(int(row["reference_index"]), [])
                        for token in [display_by_source_id[source.token_id]]}.values())
        ids = list(dict.fromkeys(token.token_id for token in members))
        if len(ids) < 2:
            continue
        cue = next(cue for cue in document.cues if ids[0] in cue.display_token_ids)
        positions = [cue.display_token_ids.index(token_id) for token_id in ids if token_id in cue.display_token_ids]
        if len(positions) != len(ids) or positions != list(range(positions[0], positions[0] + len(ids))):
            continue
        after = layout_tokens(token.text for token in members)
        operation_id = f"op_reference_semantic_group_{group_index}"
        document = apply_document_operation(document, {
            "operation_id": operation_id, "type": "merge",
            "payload": {"cue_id": cue.cue_id, "token_ids": ids, "text": after,
                        "provenance": {"kind": "import", "operation": "reference_manuscript_replace",
                                       "actor": "reference-manuscript", "metadata": {"reference": True}}},
        })
        merged = next(token for token in document.display_tokens if token.provenance.metadata.get("operation_id") == operation_id)
        for source_id in merged.source_token_ids:
            display_by_source_id[source_id] = merged
        grouped_indexes.update(int(row["reference_index"]) for row in rows)
        reference_changes.append({"change_id": f"reference-group-{group_index}", "type": "replace",
                                  "token_ids": [merged.token_id], "source_indexes": [source_by_reference_index[int(row["reference_index"])].index for row in rows],
                                  "before": rows[0]["source_text"], "after": after, "status": "applied"})

    for raw in reference_report.get("provenance", []):
        if not isinstance(raw, Mapping) or not bool(raw.get("changed")):
            continue
        reference_index = int(raw["reference_index"])
        if reference_index in grouped_indexes:
            continue
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
        source.index: display_by_source_id[source.token_id]
        for source in document.source_tokens
        if source.token_id in display_by_source_id
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

    if not reference_changes and not reference_report.get("requires_review"):
        return document
    audit = ChangeProvenance(
        kind=ChangeKind.IMPORT,
        operation="reference_manuscript_alignment",
        actor="reference-manuscript",
        metadata={
            "reference": True,
            "mode": "semantic",
            "authority": "reference_assisted",
            "requires_review": bool(reference_report.get("requires_review")),
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
        projected = layout_tokens(projected_parts)
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
    display_by_source = {
        source_id: token.token_id
        for token in document.display_tokens for source_id in token.source_token_ids
    }
    source_to_display = {
        source.index: display_by_source[source.token_id] for source in document.source_tokens
    }
    return apply_reference_report(document, reference_report, source_to_display, display_breaks=display_breaks)


def apply_reference_report(
    document: EditorDocument, reference_report: Mapping[str, Any],
    source_to_display: Mapping[int, str], *, display_breaks: list[int] | None = None,
    operation_prefix: str = "reference_initial",
) -> EditorDocument:
    """Apply one shared, reversible report without changing Cue identities/times."""
    source_to_display = dict(source_to_display)
    display_breaks = display_breaks or []
    expected_insertions = {(reference_report.get("reference_sha256"), int(item["reference_index"]), str(item["text"]))
                           for item in reference_report.get("insertions", [])}
    obsolete = {token.token_id for token in document.display_tokens
                if token.state.value == "deleted" and not token.source_token_ids
                and token.provenance.operation == "reference_manuscript_insert"
                and (token.provenance.metadata.get("reference_sha256"),
                     token.provenance.metadata.get("reference_index"), token.text) not in expected_insertions}
    if obsolete:
        document = replace(document,
            display_tokens=tuple(token for token in document.display_tokens if token.token_id not in obsolete),
            cues=tuple(replace(cue, display_token_ids=tuple(t for t in cue.display_token_ids if t not in obsolete))
                       for cue in document.cues),
            changes=tuple(replace(change, metadata={**change.metadata,
                "reference_changes": [row for row in change.metadata["reference_changes"]
                                      if not obsolete.intersection(row.get("token_ids", []))]})
                if "reference_changes" in change.metadata else change for change in document.changes))
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
                "evidence": {key: item[key] for key in ("decision", "score", "reference_frequency", "frequency_boost", "evidence_count") if key in item},
            }
        )
    if replacements:
        document = apply_document_operation(
            document,
            {
                "operation_id": f"op_{operation_prefix}_replacements",
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

    for item in reference_report.get("merges", []):
        indexes = list(item["source_indexes"])
        token_ids = list(dict.fromkeys(source_to_display[index] for index in indexes))
        cue = next(cue for cue in document.cues if token_ids[0] in cue.display_token_ids)
        if not all(token_id in cue.display_token_ids for token_id in token_ids):
            raise ValueError("reference replacement crosses an existing Cue boundary")
        operation_id = f"op_{operation_prefix}_merge_{item['reference_index']}"
        document = apply_document_operation(document, {
            "operation_id": operation_id, "type": "merge",
            "payload": {"cue_id": cue.cue_id, "token_ids": token_ids, "text": item["after"],
                        "provenance": {"kind": "import", "operation": "reference_manuscript_replace",
                                       "actor": "reference-manuscript", "metadata": {"reference": True}}},
        })
        merged = next(token for token in document.display_tokens
                      if token.provenance.metadata.get("operation_id") == operation_id)
        for index in indexes:
            source_to_display[index] = merged.token_id
        reference_changes.append({"change_id": f"reference-merge-{item['reference_index']}",
                                  "type": "replace", "token_ids": [merged.token_id],
                                  "source_indexes": indexes, "before": item["before"],
                                  "after": item["after"], "status": "applied"})

    insertion_anchor_by_source: dict[int, str | None] = {}
    insertion_anchor_by_offset: dict[tuple[int, int], str] = {}
    fragments_by_origin: dict[str, list[tuple[int, int, str]]] = {}

    def anchor_at_offset(source_index: int, offset: int, reference_index: int) -> str:
        nonlocal document
        origin = source_to_display[source_index]
        if origin not in fragments_by_origin:
            original = next(token for token in document.display_tokens if token.token_id == origin)
            fragments_by_origin[origin] = [(0, len(original.text), origin)]
        fragments = fragments_by_origin[origin]
        for position, (start, end, token_id) in enumerate(fragments):
            if offset == end:
                return token_id
            if start < offset < end:
                token = next(token for token in document.display_tokens if token.token_id == token_id)
                cut = offset - start
                prefix, suffix = token.text[:cut].strip(), token.text[cut:].strip()
                if not prefix or not suffix:
                    return token_id
                cue = next(cue for cue in document.cues if token_id in cue.display_token_ids)
                operation_id = f"op_{operation_prefix}_fragment_{reference_index}"
                document = apply_document_operation(document, {
                    "operation_id": operation_id + "_left", "type": "replace",
                    "payload": {"token_id": token_id, "text": prefix, "expected_text": token.text},
                })
                document = apply_document_operation(document, {
                    "operation_id": operation_id, "type": "insert",
                    "payload": {"cue_id": cue.cue_id, "after_token_id": token_id,
                                "token": {"text": suffix, "original_text": suffix, "source_token_ids": []},
                                "provenance": {"kind": "manual", "operation": "reference_display_fragment",
                                               "actor": "reference-manuscript",
                                               "metadata": {"original_token_id": origin}}},
                })
                right = next(token for token in document.display_tokens
                             if token.provenance.metadata.get("operation_id") == operation_id)
                fragments[position:position + 1] = [(start, offset, token_id), (offset, end, right.token_id)]
                return token_id
        return fragments[-1][2]

    reference_insertion_ids: list[str] = []
    existing_insertions = {
        (token.provenance.metadata.get("reference_sha256"), token.provenance.metadata.get("reference_index"), token.text): token
        for token in document.display_tokens
        if token.provenance.operation == "reference_manuscript_insert"
        and token.state.value == "deleted"
    }
    for item in sorted(
        reference_report.get("insertions", []),
        key=lambda value: int(value["reference_index"]),
    ):
        source_index = int(item["after_source_index"])
        placement = str(item.get("placement", "left"))
        if source_index < 0:
            first_token = source_to_display[min(source_to_display)]
            cue = next(cue for cue in document.cues if first_token in cue.display_token_ids)
            anchor_id = insertion_anchor_by_source.get(source_index)
        else:
            anchor_id = insertion_anchor_by_source.get(
                source_index, source_to_display[source_index]
            )
            if "after_source_offset" in item:
                offset = int(item["after_source_offset"])
                anchor_id = insertion_anchor_by_offset.get((source_index, offset))
                if anchor_id is None:
                    anchor_id = anchor_at_offset(source_index, offset, int(item["reference_index"]))
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
        operation_id = f"op_{operation_prefix}_insert_{reference_index}"
        inserted = existing_insertions.get((reference_report.get("reference_sha256"), reference_index, str(item["text"])))
        if inserted is not None and inserted.token_id not in cue.display_token_ids:
            inserted = None
        if inserted is None:
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
                                "reference_sha256": reference_report.get("reference_sha256"),
                                "reference_index": reference_index,
                            },
                        },
                    },
                },
            )
            inserted = next(
                token for token in document.display_tokens
                if token.provenance.metadata.get("operation_id") == operation_id
            )
        reference_insertion_ids.append(inserted.token_id)
        insertion_anchor_by_source[source_index] = inserted.token_id
        if "after_source_offset" in item:
            insertion_anchor_by_offset[(source_index, int(item["after_source_offset"]))] = inserted.token_id
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
                "reason": str(item.get("reason", "reference_only")),
            }
        )

    if reference_insertion_ids:
        document = apply_document_operation(
            document,
            {
                "operation_id": f"op_{operation_prefix}_insertions_deleted",
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
            "matcher_version": reference_report.get("matcher_version"),
            "reference_sha256": reference_report.get("reference_sha256"),
            "local_decisions": reference_report.get("local_decisions", []),
            "lexical_consensus": reference_report.get("lexical_consensus", []),
            "authority": "reference_assisted",
            "requires_review": bool(reference_report.get("requires_review")),
            "confidence": str(reference_report.get("confidence", "unknown")),
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
