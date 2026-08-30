from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from substar_core.domain.editor_document import (
    ChangeKind,
    ChangeProvenance,
    DisplayCue,
    DisplayToken,
    DocumentValidationError,
    EditorDocument,
    SourceToken,
    stable_id,
)
from substar_core.editor.domain.groups import initialize_segmentation_groups


SOURCE_SCHEMA = "substar.source-tokens.v1"
PROVENANCE_EPOCH = "1970-01-01T00:00:00.000+00:00"


def _attribute(row: object, name: str) -> object:
    if isinstance(row, Mapping):
        if name not in row:
            raise ValueError(f"source token 缺少 {name}")
        return row[name]
    if not hasattr(row, name):
        raise ValueError(f"source token 缺少 {name}")
    return getattr(row, name)


def _source_tokens(
    rows: Iterable[object],
    *,
    source_kind: Literal["asr", "jianying"],
    source_asset_id: str,
) -> list[SourceToken]:
    asset_id = str(source_asset_id).strip()
    if not asset_id:
        raise ValueError("source_asset_id 不能为空")
    tokens: list[SourceToken] = []
    seen_indexes: set[int] = set()
    previous_start = -1.0
    for row in rows:
        index = int(_attribute(row, "index"))
        start = float(_attribute(row, "start"))
        end = float(_attribute(row, "end"))
        text = str(_attribute(row, "text")).strip()
        speaker: object = None
        if isinstance(row, Mapping):
            speaker = row.get("speaker_id")
        elif hasattr(row, "speaker_id"):
            speaker = getattr(row, "speaker_id")
        if index in seen_indexes:
            raise ValueError(f"source token index 重复：{index}")
        if start < previous_start:
            raise ValueError(f"source token 时间乱序：{index}")
        token_id = stable_id(
            "src",
            {
                "source_kind": source_kind,
                "source_asset_id": asset_id,
                "index": index,
                "start": start,
                "end": end,
            },
        )
        tokens.append(
            SourceToken(
                token_id=token_id,
                index=index,
                text=text,
                start=start,
                end=end,
                speaker=str(speaker) if speaker is not None and speaker != "" else None,
            )
        )
        seen_indexes.add(index)
        previous_start = start
    if not tokens:
        raise ValueError("source_tokens 不能为空")
    return tokens


def source_tokens_from_asr(
    rows: Iterable[object], *, source_asset_id: str
) -> list[SourceToken]:
    """Create domain SourceTokens from explicit ASR alignment rows in seconds."""

    return _source_tokens(rows, source_kind="asr", source_asset_id=source_asset_id)


def source_tokens_from_jianying(
    rows: Iterable[object], *, source_asset_id: str
) -> list[SourceToken]:
    """Create domain SourceTokens from explicit Jianying alignment rows in seconds."""

    return _source_tokens(rows, source_kind="jianying", source_asset_id=source_asset_id)


def _require_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} 必须是对象")
    return value


def _require_rows(value: object, name: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise ValueError(f"{name} 必须是对象数组")
    return list(value)


def _validate_source_tokens(
    tokens: Sequence[SourceToken],
    *,
    source_kind: Literal["asr", "jianying"],
    source_asset_id: str,
) -> dict[int, SourceToken]:
    if not tokens:
        raise ValueError("source_tokens 不能为空")
    by_index = {token.index: token for token in tokens}
    indexes = [token.index for token in tokens]
    if len(by_index) != len(tokens) or indexes != sorted(indexes):
        raise ValueError("source_tokens 的 index 必须唯一且有序")
    if indexes != list(range(indexes[0], indexes[-1] + 1)):
        raise ValueError("source_tokens 的 index 必须连续")
    for token in tokens:
        expected = stable_id(
            "src",
            {
                "source_kind": source_kind,
                "source_asset_id": source_asset_id,
                "index": token.index,
                "start": token.start,
                "end": token.end,
            },
        )
        if token.token_id != expected:
            raise ValueError(f"SourceToken stable ID 校验失败：{token.index}")
    return by_index


def _change(
    *, kind: ChangeKind, operation: str, metadata: Mapping[str, Any]
) -> ChangeProvenance:
    return ChangeProvenance(
        kind=kind,
        operation=operation,
        actor="segmentation-adapter",
        created_at=PROVENANCE_EPOCH,
        metadata=dict(metadata),
    )


def _display_tokens(
    source_tokens: Sequence[SourceToken],
    raw_canonicalizations: object,
    raw_ai_calibrations: object = (),
) -> tuple[list[DisplayToken], list[ChangeProvenance], dict[int, str]]:
    by_index = {token.index: token for token in source_tokens}
    canonicalizations = _require_rows(
        raw_canonicalizations, "semantic_grouping.canonicalizations"
    )
    ai_calibrations = _require_rows(
        raw_ai_calibrations, "semantic_grouping.ai_calibrations"
    )
    # Deterministic canonicalizations and AI calibrations are separate current
    # inputs. Keep one materialized row per source span, preferring the
    # explicit AI record when both stages address the same span.
    by_span: dict[tuple[int, int], Mapping[str, Any]] = {}
    for row in canonicalizations:
        start = int(row.get("alignment_start", -1))
        end = int(row.get("alignment_end", -1))
        by_span[(start, end)] = row
    for row in ai_calibrations:
        start = int(row.get("alignment_start", -1))
        end = int(row.get("alignment_end", -1))
        by_span[(start, end)] = row
    canonicalizations = [
        by_span[key] for key in sorted(by_span, key=lambda item: (item[0], item[1]))
    ]
    canonical_by_start: dict[int, Mapping[str, Any]] = {}
    occupied: set[int] = set()
    for row in canonicalizations:
        start = int(row.get("alignment_start", -1))
        end = int(row.get("alignment_end", -1))
        canonical_text = str(row.get("canonical_text", "")).strip()
        indexes = list(range(start, end + 1))
        if start > end or not canonical_text or any(index not in by_index for index in indexes):
            raise ValueError(f"P2 canonicalization 范围非法：{start}..{end}")
        if any(index in occupied for index in indexes):
            raise ValueError("P2 canonicalizations 不得重叠")
        canonical_by_start[start] = row
        occupied.update(indexes)

    display_tokens: list[DisplayToken] = []
    canonical_changes: list[ChangeProvenance] = []
    source_to_display: dict[int, str] = {}
    cursor = min(by_index)
    position = 0
    while cursor <= max(by_index):
        canonical = canonical_by_start.get(cursor)
        if canonical is None:
            source = by_index[cursor]
            provenance = _change(
                kind=ChangeKind.SOURCE,
                operation="project_source_token",
                metadata={"source_token_ids": [source.token_id]},
            )
            display = DisplayToken.create(
                position=position,
                text=source.text,
                source_token_ids=(source.token_id,),
                provenance=provenance,
            )
            display_tokens.append(display)
            source_to_display[cursor] = display.token_id
            cursor += 1
            position += 1
            continue

        start = int(canonical["alignment_start"])
        end = int(canonical["alignment_end"])
        source = [by_index[index] for index in range(start, end + 1)]
        source_ids = [token.token_id for token in source]
        before = " ".join(token.text for token in source)
        after = str(canonical["canonical_text"]).strip()
        operations: list[dict[str, Any]] = []
        replace_inputs = source_ids
        if len(source_ids) > 1:
            merged_id = stable_id(
                "merge", {"source_token_ids": source_ids, "before": before}
            )
            operations.append(
                {
                    "operation": "merge",
                    "input_token_ids": source_ids,
                    "output_token_id": merged_id,
                    "before": before,
                    "after": before,
                }
            )
            replace_inputs = [merged_id]
        operations.append(
            {
                "operation": "replace",
                "input_token_ids": replace_inputs,
                "before": before,
                "after": after,
            }
        )
        is_ai_calibration = str(canonical.get("source", "")) not in {
            "glossary_deterministic",
            "deterministic",
            "program_deterministic",
        } or str(canonical.get("kind", "")) == "ai_calibration"
        provenance = _change(
            kind=ChangeKind.AI if is_ai_calibration else ChangeKind.NORMALIZATION,
            operation=(
                "ai_calibration_apply"
                if is_ai_calibration
                else "canonicalize_source_tokens"
            ),
            metadata={
                "stage": "semantic_grouping",
                "alignment_start": start,
                "alignment_end": end,
                "operations": operations,
                "source": str(canonical.get("source", "p2_contextual")),
                "confidence": float(canonical.get("confidence", 1.0)),
                "reason": str(canonical.get("reason", "")),
                "ai_calibration": is_ai_calibration,
            },
        )
        display = DisplayToken.create(
            position=position,
            text=after,
            original_text=before,
            source_token_ids=source_ids,
            provenance=provenance,
        )
        operations[-1]["output_token_id"] = display.token_id
        display_tokens.append(display)
        canonical_changes.append(provenance)
        for index in range(start, end + 1):
            source_to_display[index] = display.token_id
        cursor = end + 1
        position += 1
    return display_tokens, canonical_changes, source_to_display


def _overlapping_ids(
    rows: Sequence[Mapping[str, Any]], start: int, end: int, id_key: str
) -> list[str]:
    return [
        str(row[id_key])
        for row in rows
        if int(row["alignment_start"]) <= end
        and start <= int(row["alignment_end"])
    ]


def _collapse_zero_duration_spans(
    spans: Sequence[tuple[int, int]], by_index: Mapping[int, SourceToken]
) -> list[tuple[int, int]]:
    """Fold zero-duration ASR-only groups into a neighbouring timed cue.

    Source tokens remain untouched.  Only the display grouping changes, so the
    editor can expose and delete provider hallucinations without inventing time.
    """

    normalized: list[tuple[int, int]] = []
    pending_prefix: tuple[int, int] | None = None
    for start, end in spans:
        has_duration = by_index[end].end > by_index[start].start
        if has_duration:
            if pending_prefix is not None:
                start = pending_prefix[0]
                pending_prefix = None
            normalized.append((start, end))
            continue
        if normalized:
            normalized[-1] = (normalized[-1][0], end)
        elif pending_prefix is None:
            pending_prefix = (start, end)
        else:
            pending_prefix = (pending_prefix[0], end)
    if pending_prefix is not None:
        raise DocumentValidationError(
            "source contains no positive-duration range for a display cue"
        )
    return normalized


def build_editor_document(
    *,
    source_tokens: Sequence[SourceToken],
    source_kind: Literal["asr", "jianying"],
    source_asset_id: str,
    execution_plan: Mapping[str, Any],
    semantic_grouping: Mapping[str, Any],
    cue_layout: Mapping[str, Any],
    generation_mode: str = "asr",
) -> EditorDocument:
    """Build one editor document from explicit segmentation inputs."""

    if source_kind not in {"asr", "jianying"}:
        raise ValueError("source_kind 只能是 asr 或 jianying")
    asset_id = str(source_asset_id).strip()
    if not asset_id:
        raise ValueError("source_asset_id 不能为空")
    planning = _require_mapping(execution_plan, "execution_plan")
    segmentation = _require_mapping(semantic_grouping, "semantic_grouping")
    layout = _require_mapping(cue_layout, "cue_layout")
    by_index = _validate_source_tokens(
        source_tokens, source_kind=source_kind, source_asset_id=asset_id
    )
    first, final = min(by_index), max(by_index)

    blocks = _require_rows(planning.get("blocks", []), "execution_plan.blocks")
    planning_boundaries = [int(value) for value in planning.get("boundaries_after", [])]
    skipped_reason = planning.get("skipped_reason")
    if skipped_reason and planning_boundaries:
        raise ValueError("skipped planning must not contain boundaries_after")
    protections = _require_rows(segmentation.get("protections", []), "semantic_grouping.protections")
    meaning_groups = _require_rows(
        segmentation.get("meaning_groups", []), "semantic_grouping.meaning_groups"
    )
    review_regions = _require_rows(
        segmentation.get("review_regions", []), "semantic_grouping.review_regions"
    )
    display_tokens, canonical_changes, source_to_display = _display_tokens(
        source_tokens,
        [],
        [],
    )
    display_by_id = {token.token_id: token for token in display_tokens}

    cuts = sorted({int(value) for value in layout.get("display_breaks", [])})
    if any(cut < first or cut >= final or cut not in by_index for cut in cuts):
        raise ValueError("display_breaks must reference non-final source indexes")
    requested_spans: list[tuple[int, int]] = []
    left = first
    for cut in cuts:
        requested_spans.append((left, cut))
        left = cut + 1
    requested_spans.append((left, final))
    spans = _collapse_zero_duration_spans(requested_spans, by_index)
    effective_cuts = [end for _, end in spans[:-1]]
    collapsed_cuts = sorted(set(cuts) - set(effective_cuts))

    document_key = f"{source_kind}:{asset_id}"
    cues: list[DisplayCue] = []
    cue_lineage: dict[str, Any] = {}
    for cue_index, (start, end) in enumerate(spans):
        token_ids: list[str] = []
        for index in range(start, end + 1):
            display_id = source_to_display[index]
            if not token_ids or token_ids[-1] != display_id:
                token_ids.append(display_id)
        source = [by_index[index] for index in range(start, end + 1)]
        speakers = {token.speaker for token in source if token.speaker}
        cue_id = stable_id(
            "cue",
            {
                "document_key": document_key,
                "first_source_token_id": source[0].token_id,
                "last_source_token_id": source[-1].token_id,
            },
        )
        cue = DisplayCue(
            cue_id=cue_id,
            index=cue_index,
            display_token_ids=tuple(token_ids),
            start=source[0].start,
            end=source[-1].end,
            translation=None,
            speaker=next(iter(speakers)) if len(speakers) == 1 else None,
        )
        cues.append(cue)
        canonical_display_ids = [
            token_id
            for token_id in token_ids
            if display_by_id[token_id].provenance.operation
            == "canonicalize_source_tokens"
        ]
        ai_calibration_display_ids = [
            token_id
            for token_id in token_ids
            if display_by_id[token_id].provenance.kind is ChangeKind.AI
        ]
        cue_lineage[cue_id] = {
            "source_indexes": [start, end],
            "source_token_ids": [token.token_id for token in source],
            "planning_block_ids": _overlapping_ids(
                blocks, start, end, "block_id"
            ),
            "semantic_group_ids": _overlapping_ids(
                meaning_groups, start, end, "group_id"
            ),
            "protection_span_ids": _overlapping_ids(
                protections, start, end, "span_id"
            ),
            "canonical_display_token_ids": canonical_display_ids,
            "calibration_display_token_ids": ai_calibration_display_ids,
            "cut_after": end if end in effective_cuts else None,
        }

    review_cue_ids = [
        cue_id
        for cue_id, lineage in cue_lineage.items()
        if any(
            int(lineage["source_indexes"][0]) <= int(region["alignment_end"])
            and int(lineage["source_indexes"][1]) >= int(region["alignment_start"])
            for region in review_regions
        )
    ]

    segmentation_lineage = _change(
        kind=ChangeKind.IMPORT,
        operation="build_from_segmentation",
        metadata={
            "source": {"kind": source_kind, "asset_id": asset_id},
            "generation_mode": str(generation_mode),
            "planning": {
                "skipped": bool(skipped_reason),
                "skip_reason": str(skipped_reason) if skipped_reason else None,
                "boundaries_after": planning_boundaries,
                "block_ids": [str(row["block_id"]) for row in blocks],
            },
            "segmentation": {
                "semantic_group_ids": [str(row["group_id"]) for row in meaning_groups],
                "protection_span_ids": [str(row["span_id"]) for row in protections],
                "review_regions": review_regions,
                "review_cue_ids": review_cue_ids,
            },
            "layout": {
                "requested_display_breaks": cuts,
                "display_breaks": effective_cuts,
                "collapsed_zero_duration_breaks": collapsed_cuts,
            },
            "cue_lineage": cue_lineage,
        },
    )
    return initialize_segmentation_groups(
        EditorDocument.create(
            source_tokens=source_tokens,
            display_tokens=display_tokens,
            cues=cues,
            document_key=document_key,
            complete=False,
            changes=[segmentation_lineage, *canonical_changes],
        )
    )


def _read_json(path: Path, label: str) -> object:
    explicit = Path(path)
    if not explicit.is_file():
        raise ValueError(f"{label} 不存在：{explicit}")
    return json.loads(explicit.read_text(encoding="utf-8"))


def build_editor_document_from_files(
    *,
    source_tokens_file: Path,
    execution_plan_file: Path,
    semantic_grouping_file: Path,
    cue_layout_file: Path,
) -> EditorDocument:
    """Require four explicit JSON paths; never probe a job directory."""

    raw_source = _require_mapping(
        _read_json(source_tokens_file, "source_tokens_file"), "source_tokens_file"
    )
    if raw_source.get("schema_version") != SOURCE_SCHEMA:
        raise ValueError(f"source_tokens_file.schema_version 必须是 {SOURCE_SCHEMA}")
    source_kind = str(raw_source.get("source_kind", ""))
    source_asset_id = str(raw_source.get("source_asset_id", ""))
    rows = _require_rows(
        raw_source.get("source_tokens"), "source_tokens_file.source_tokens"
    )
    tokens = [SourceToken.from_dict(row) for row in rows]
    return build_editor_document(
        source_tokens=tokens,
        source_kind=source_kind,  # type: ignore[arg-type]
        source_asset_id=source_asset_id,
        execution_plan=_require_mapping(
            _read_json(execution_plan_file, "execution_plan_file"), "execution_plan_file"
        ),
        semantic_grouping=_require_mapping(
            _read_json(semantic_grouping_file, "semantic_grouping_file"), "semantic_grouping_file"
        ),
        cue_layout=_require_mapping(
            _read_json(cue_layout_file, "cue_layout_file"), "cue_layout_file"
        ),
    )
