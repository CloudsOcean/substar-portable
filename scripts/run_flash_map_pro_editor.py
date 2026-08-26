from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_global_planner_ab import call_streaming_model  # noqa: E402
from scripts.segmentation_support import (  # noqa: E402
    SegmentationError,
    _direct_report,
    call_model,
    read,
    resolve_api_key,
    source_punctuation_kwargs,
    write_json,
    write_two_level_artifacts,
)
from substar_core.segmentation.material import extract_alignment, extract_master  # noqa: E402
from substar_core.segmentation.chunking import _unit_original_ranges  # noqa: E402
from substar_core.segmentation.validation import evaluate_direct_plan  # noqa: E402


PROMPTS = {
    "p1": PROJECT_ROOT / "prompts" / "04P1_Flash_全片分层保护.md",
    "p2": PROJECT_ROOT / "prompts" / "04P2_Pro_全片意义组.md",
    "p3": PROJECT_ROOT / "prompts" / "04P3_Flash_并发三候选.md",
    "p4": PROJECT_ROOT / "prompts" / "04P4_Pro_全片总编修改.md",
}
LEVELS = {"hard", "strong_soft", "outer_soft"}
ENGLISH_HARD_LIMIT = 55
ATTACHMENTS = {
    "atomic",
    "attach_left",
    "attach_right",
    "self_contained",
    "outer_container",
}
RELATIONS = {"continuous", "related", "separate"}
CRITICAL_REVIEW_CODES = {
    "cross_group_dangling_phrase",
    "crossed_sentence_boundary",
    "dangling_function_phrase",
    "dangling_line_end",
    "copula_complement_cut",
    "orphan_short_object",
}


def system_prompt(stage: str, material: str) -> str:
    parts = [read(PROMPTS[stage])]
    if stage in {"p3", "p4"}:
        parts.append(
            "# ACTIVE_OUTPUT_PROFILE\n"
            + json.dumps(
                source_punctuation_kwargs(), ensure_ascii=False, indent=2
            )
        )
    parts.extend(
        [
            (
                "# EXAMPLE_POLICY\n"
                "本实验只允许使用提示词中人工构造的例句。不得检索、引用或模仿"
                "公司范稿、测试素材、历史输出或人工答案中的具体措辞。"
            ),
            "只返回一个 JSON 对象，不输出 Markdown 代码围栏。",
        ]
    )
    return "\n\n".join(parts)


def unit_indexes(units: list[Any]) -> set[int]:
    return {int(unit.index) for unit in units}


def normalize_p1(
    value: dict[str, Any],
    units: list[Any],
    *,
    require_coverage: bool = False,
    expected_coverage: tuple[int, int] | None = None,
    owner_core: tuple[int, int] | None = None,
) -> dict[str, Any]:
    if value.get("schema_version") != "substar.stage1.protection.v1":
        raise SegmentationError("P1 schema_version 错误")
    coverage = value.get("coverage_check", {})
    coverage_start, coverage_end = expected_coverage or (
        int(units[0].index),
        int(units[-1].index),
    )
    if require_coverage and (
        not isinstance(coverage, dict)
        or coverage.get("complete") is not True
        or coverage.get("alignment_start") != coverage_start
        or coverage.get("alignment_end") != coverage_end
    ):
        raise SegmentationError(
            f"P1 未声明完整扫描责任区 {coverage_start}..{coverage_end}"
        )
    valid = unit_indexes(units)
    units_by_index = {int(unit.index): unit for unit in units}
    spans: list[dict[str, Any]] = []
    for number, raw in enumerate(value.get("spans", []), start=1):
        if not isinstance(raw, dict):
            raise SegmentationError("P1 span 必须是对象")
        start = raw.get("alignment_start")
        end = raw.get("alignment_end")
        level = str(raw.get("level", raw.get("protection_level", "")))
        attachment = str(raw.get("attachment", ""))
        head_index = raw.get("head_index")
        if head_index is None and owner_core is None:
            # Backward-compatible normalization for frozen pre-window P1
            # artifacts. New windowed P1 responses must declare ownership.
            head_index = end
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or start not in valid
            or end not in valid
            or end < start
            or level not in LEVELS
            or attachment not in ATTACHMENTS
            or not isinstance(head_index, int)
            or not start <= head_index <= end
        ):
            raise SegmentationError(f"P1 span 非法：{raw}")
        if owner_core is not None and not (
            owner_core[0] <= head_index <= owner_core[1]
        ):
            continue
        source_text = " ".join(
            str(units_by_index[index].text)
            for index in range(start, end + 1)
        )
        source_char_count = len(source_text)
        spans.append(
            {
                "span_id": f"s{number:04d}",
                "alignment_start": start,
                "alignment_end": end,
                "head_index": head_index,
                "protection_level": level,
                "category": str(raw.get("category", "syntactic_structure")),
                "attachment": attachment,
                "source_char_count": source_char_count,
                "delivery_feasible_as_single_cue": (
                    source_char_count <= ENGLISH_HARD_LIMIT
                ),
                "confidence": max(
                    0.0, min(1.0, float(raw.get("confidence", 0.8)))
                ),
                "reason": str(raw.get("reason", "")),
            }
        )
    spans.sort(
        key=lambda item: (
            int(item["alignment_start"]),
            -int(item["alignment_end"]),
            item["protection_level"],
        )
    )
    # A single bounded model repair can still return crossing (rather than
    # nested) spans. This is a contract defect, not a reason to retry the
    # programme indefinitely. Resolve it deterministically while preserving
    # the stronger, more confident and more atomic protection, and expose
    # every dropped span for audit.
    level_rank = {"hard": 3, "strong_soft": 2, "outer_soft": 1}
    dropped_crossings: list[dict[str, Any]] = []

    def retention_key(span: dict[str, Any]) -> tuple[int, int, float, int]:
        length = int(span["alignment_end"]) - int(span["alignment_start"]) + 1
        return (
            level_rank[str(span["protection_level"])],
            1 if span["attachment"] == "atomic" else 0,
            float(span["confidence"]),
            -length,
        )

    while True:
        crossing_pair: tuple[int, int] | None = None
        for left_pos, left in enumerate(spans):
            for right_pos in range(left_pos + 1, len(spans)):
                right = spans[right_pos]
                if int(right["alignment_start"]) > int(left["alignment_end"]):
                    break
                if (
                    int(left["alignment_start"])
                    < int(right["alignment_start"])
                    <= int(left["alignment_end"])
                    < int(right["alignment_end"])
                ):
                    crossing_pair = (left_pos, right_pos)
                    break
            if crossing_pair is not None:
                break
        if crossing_pair is None:
            break
        left_pos, right_pos = crossing_pair
        left = spans[left_pos]
        right = spans[right_pos]
        loser_pos = (
            right_pos
            if retention_key(left) >= retention_key(right)
            else left_pos
        )
        winner = right if loser_pos == left_pos else left
        loser = spans.pop(loser_pos)
        dropped_crossings.append(
            {
                "reason_code": "crossing_span_contract_normalization",
                "kept_span": {
                    "alignment_start": winner["alignment_start"],
                    "alignment_end": winner["alignment_end"],
                    "protection_level": winner["protection_level"],
                },
                "dropped_span": {
                    "alignment_start": loser["alignment_start"],
                    "alignment_end": loser["alignment_end"],
                    "protection_level": loser["protection_level"],
                    "confidence": loser["confidence"],
                },
            }
        )
        spans.sort(
            key=lambda item: (
                int(item["alignment_start"]),
                -int(item["alignment_end"]),
                item["protection_level"],
            )
        )
    for number, span in enumerate(spans, start=1):
        span["span_id"] = f"s{number:04d}"

    def normalize_breaks(name: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[int] = set()
        for raw in value.get(name, []):
            if not isinstance(raw, dict):
                continue
            after = raw.get("after_alignment")
            if (
                not isinstance(after, int)
                or after not in valid
                or after == int(units[-1].index)
                or after in seen
            ):
                continue
            seen.add(after)
            if owner_core is not None and not (
                owner_core[0] <= after <= owner_core[1]
            ):
                continue
            rows.append(
                {
                    "after_alignment": after,
                    "priority": str(raw.get("priority", "medium")),
                    "reason": str(raw.get("reason", "")),
                }
            )
        return sorted(rows, key=lambda item: int(item["after_alignment"]))

    return {
        "schema_version": "substar.stage1.protection.v1",
        "spans": spans,
        "preferred_breaks_after": normalize_breaks("preferred_breaks_after"),
        "forbidden_breaks_after": normalize_breaks("forbidden_breaks_after"),
        "coverage_check": {
            "alignment_start": coverage_start,
            "alignment_end": coverage_end,
            "complete": bool(coverage.get("complete", not require_coverage)),
        },
        "contract_normalization": {
            "dropped_crossing_spans": dropped_crossings,
        },
    }


def protection_for_editor(protection: dict[str, Any]) -> dict[str, Any]:
    """Hide impossible whole-span requests while retaining nested protections."""
    result = copy.deepcopy(protection)
    result["spans"] = [
        span
        for span in result.get("spans", [])
        if not (
            span.get("protection_level") == "strong_soft"
            and span.get("delivery_feasible_as_single_cue") is False
        )
    ]
    return result


def build_p1_windows(
    units: list[Any],
    *,
    core_units: int,
    overlap_units: int,
) -> list[dict[str, int]]:
    core_size = max(1, int(core_units))
    overlap = max(0, int(overlap_units))
    windows: list[dict[str, int]] = []
    for core_left in range(0, len(units), core_size):
        core_right = min(len(units) - 1, core_left + core_size - 1)
        context_left = max(0, core_left - overlap)
        context_right = min(len(units) - 1, core_right + overlap)
        windows.append(
            {
                "core_left_pos": core_left,
                "core_right_pos": core_right,
                "context_left_pos": context_left,
                "context_right_pos": context_right,
                "core_start": int(units[core_left].index),
                "core_end": int(units[core_right].index),
                "context_start": int(units[context_left].index),
                "context_end": int(units[context_right].index),
            }
        )
    return windows


def render_p1_window_payload(
    units: list[Any],
    window: dict[str, int],
) -> str:
    local_units = units[
        window["context_left_pos"] : window["context_right_pos"] + 1
    ]
    alignment = "\n".join(
        f"{unit.index}\t{unit.start:.3f}\t{unit.end:.3f}\t{unit.text}\t"
        f"{unit.sentence_id if unit.sentence_id is not None else '-'}\t"
        f"{1 if unit.sentence_start else 0}\t{1 if unit.sentence_end else 0}"
        for unit in local_units
    )
    transcript = " ".join(str(unit.text) for unit in local_units)
    return "\n\n".join(
        [
            "# CORE_OWNERSHIP",
            (
                f"core_alignment: {window['core_start']}..{window['core_end']}\n"
                f"context_alignment: {window['context_start']}.."
                f"{window['context_end']}\n"
                "只输出 head_index 位于 core_alignment 的保护结构。"
            ),
            "# LOCAL_TRANSCRIPT\n```text\n" + transcript + "\n```",
            (
                "# LOCAL_ALIGNMENT\n"
                "index / start / end / text / sentence_id / sentence_start / "
                "sentence_end\n```tsv\n"
                + alignment
                + "\n```"
            ),
        ]
    )


def merge_p1_windows(
    chunks: list[dict[str, Any]],
    units: list[Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    span_by_key: dict[tuple[int, int, int, str], dict[str, Any]] = {}
    level_by_range: dict[tuple[int, int], str] = {}
    preferred_by_after: dict[int, dict[str, Any]] = {}
    forbidden_by_after: dict[int, dict[str, Any]] = {}
    coverage: list[tuple[int, int]] = []
    for chunk in chunks:
        check = chunk["coverage_check"]
        coverage.append(
            (int(check["alignment_start"]), int(check["alignment_end"]))
        )
        for span in chunk.get("spans", []):
            span_range = (
                int(span["alignment_start"]),
                int(span["alignment_end"]),
            )
            previous_level = level_by_range.get(span_range)
            current_level = str(span["protection_level"])
            if previous_level is not None and previous_level != current_level:
                raise SegmentationError(
                    "P1窗口对同一区间给出冲突保护等级："
                    f"{span_range} {previous_level}/{current_level}"
                )
            level_by_range[span_range] = current_level
            key = (
                span_range[0],
                span_range[1],
                int(span["head_index"]),
                current_level,
            )
            existing = span_by_key.get(key)
            if existing is None or float(span["confidence"]) > float(
                existing["confidence"]
            ):
                span_by_key[key] = copy.deepcopy(span)
        for row in chunk.get("preferred_breaks_after", []):
            after = int(row["after_alignment"])
            existing = preferred_by_after.get(after)
            if existing is None or str(row.get("priority")) == "high":
                preferred_by_after[after] = copy.deepcopy(row)
        for row in chunk.get("forbidden_breaks_after", []):
            forbidden_by_after[int(row["after_alignment"])] = copy.deepcopy(
                row
            )

    # Core ownership must cover the programme exactly once.
    cursor = int(units[0].index)
    for start, end in sorted(coverage):
        if start != cursor or end < start:
            raise SegmentationError(
                f"P1核心区覆盖不连续：expected={cursor}, got={start}-{end}"
            )
        cursor = end + 1
    if cursor - 1 != int(units[-1].index):
        raise SegmentationError(
            f"P1核心区末端错误：expected={units[-1].index}, got={cursor - 1}"
        )

    conflicts = sorted(set(preferred_by_after) & set(forbidden_by_after))
    for after in conflicts:
        preferred_by_after.pop(after, None)
    raw = {
        "schema_version": "substar.stage1.protection.v1",
        "spans": list(span_by_key.values()),
        "preferred_breaks_after": list(preferred_by_after.values()),
        "forbidden_breaks_after": list(forbidden_by_after.values()),
        "coverage_check": {
            "alignment_start": int(units[0].index),
            "alignment_end": int(units[-1].index),
            "complete": True,
        },
    }
    merged = normalize_p1(raw, units, require_coverage=True)
    window_contract_drops: list[dict[str, Any]] = []
    for window_number, chunk in enumerate(chunks, start=1):
        for item in chunk.get("contract_normalization", {}).get(
            "dropped_crossing_spans", []
        ):
            if isinstance(item, dict):
                window_contract_drops.append(
                    {"window_number": window_number, **copy.deepcopy(item)}
                )
    merged["contract_normalization"][
        "dropped_crossing_spans"
    ] = window_contract_drops
    audit = {
        "schema_version": "substar.stage1.p1-window-merge-audit.v1",
        "window_count": len(chunks),
        "coverage": sorted(coverage),
        "preferred_forbidden_conflicts": conflicts,
        "span_count": len(merged["spans"]),
        "dropped_crossing_span_count": len(window_contract_drops),
        "dropped_crossing_spans": window_contract_drops,
    }
    return merged, audit


def normalize_p2(
    value: dict[str, Any],
    units: list[Any],
    protection: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if value.get("schema_version") != "substar.stage1.meaning-groups.v1":
        raise SegmentationError("P2 schema_version 错误")
    raw_groups = value.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise SegmentationError("P2 缺少 groups")
    expected = int(units[0].index)
    normalized: list[dict[str, Any]] = []
    for number, raw in enumerate(raw_groups, start=1):
        start = raw.get("alignment_start")
        end = raw.get("alignment_end")
        if start != expected or not isinstance(end, int) or end < start:
            raise SegmentationError(
                f"P2 覆盖错误：expected={expected}, got={start}-{end}"
            )
        relation = str(
            raw.get("continuity_after", {}).get("relation", "separate")
        )
        if relation not in RELATIONS:
            relation = "separate"
        normalized.append(
            {
                "group_id": f"g{number:04d}",
                "alignment_start": start,
                "alignment_end": end,
                "center_count": max(1, int(raw.get("center_count", 1))),
                "confidence": max(
                    0.0, min(1.0, float(raw.get("confidence", 0.8)))
                ),
                "review_flags": [
                    str(item) for item in raw.get("review_flags", [])
                ],
                "candidate_internal_boundaries": sorted(
                    {
                        int(item)
                        for item in raw.get(
                            "candidate_internal_boundaries", []
                        )
                        if isinstance(item, int) and start <= item < end
                    }
                ),
                "continuity_after": {
                    "relation": relation,
                    "confidence": max(
                        0.0,
                        min(
                            1.0,
                            float(
                                raw.get("continuity_after", {}).get(
                                    "confidence", 0.5
                                )
                            ),
                        ),
                    ),
                    "reason": str(
                        raw.get("continuity_after", {}).get("reason", "")
                    ),
                    "speaker_transition": str(
                        raw.get("continuity_after", {}).get(
                            "speaker_transition", "unknown"
                        )
                    ),
                },
                "reason": str(raw.get("reason", "")),
            }
        )
        expected = end + 1
    if expected - 1 != int(units[-1].index):
        raise SegmentationError(
            f"P2 末索引错误：expected={units[-1].index}, got={expected - 1}"
        )

    # Only hard protection is an absolute grouping contract. strong_soft is
    # evidence for P2/P4 and may be crossed when the full-program Pro model
    # identifies a clearer independent discourse centre.
    protected = [
        span
        for span in protection["spans"]
        if span["protection_level"] == "hard"
    ]
    actions: list[dict[str, Any]] = []
    merged: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(normalized):
        current = copy.deepcopy(normalized[cursor])
        consumed = [current["group_id"]]
        while cursor + 1 < len(normalized):
            boundary = int(current["alignment_end"])
            crossing = [
                span
                for span in protected
                if int(span["alignment_start"])
                <= boundary
                < int(span["alignment_end"])
            ]
            if not crossing:
                break
            cursor += 1
            following = normalized[cursor]
            consumed.append(following["group_id"])
            current["alignment_end"] = following["alignment_end"]
            current["continuity_after"] = copy.deepcopy(
                following["continuity_after"]
            )
            current["reason"] = (
                current["reason"] + "; merged to preserve P1 structure"
            ).strip("; ")
            actions.append(
                {
                    "after_alignment": boundary,
                    "merged_group_ids": list(consumed),
                    "span_ids": [span["span_id"] for span in crossing],
                }
            )
        merged.append(current)
        cursor += 1
    for number, group in enumerate(merged, start=1):
        group["group_id"] = f"g{number:04d}"
    merged[-1]["continuity_after"] = {
        "relation": "separate",
        "confidence": 1.0,
        "reason": "terminal programme boundary",
        "speaker_transition": "unknown",
    }
    return {
        "schema_version": "substar.stage1.meaning-groups.v1",
        "groups": merged,
        "protection_conflicts": value.get("protection_conflicts", []),
    }, actions


def p2_density_violations(
    p2: dict[str, Any],
    *,
    soft_span_units: int = 48,
    hard_span_units: int = 96,
) -> list[dict[str, Any]]:
    """Detect multi-centre drift without prescribing a target group count."""

    violations: list[dict[str, Any]] = []
    for group in p2.get("groups", []):
        size = (
            int(group["alignment_end"])
            - int(group["alignment_start"])
            + 1
        )
        flags = {str(item) for item in group.get("review_flags", [])}
        internal = group.get("candidate_internal_boundaries", [])
        if int(group.get("center_count", 1)) != 1:
            violations.append(
                {
                    "group_id": group["group_id"],
                    "code": "multiple_centres_declared",
                    "size": size,
                }
            )
        if size > hard_span_units:
            violations.append(
                {
                    "group_id": group["group_id"],
                    "code": "density_hard_span_exceeded",
                    "size": size,
                    "limit": hard_span_units,
                }
            )
        elif size > soft_span_units and (
            "long_single_center" not in flags or not internal
        ):
            violations.append(
                {
                    "group_id": group["group_id"],
                    "code": "long_group_missing_review_contract",
                    "size": size,
                    "soft_limit": soft_span_units,
                }
            )
    return violations


def group_batches(
    groups: list[dict[str, Any]], target_units: int
) -> list[tuple[int, int]]:
    batches: list[tuple[int, int]] = []
    left = 0
    total = 0
    for position, group in enumerate(groups):
        size = int(group["alignment_end"]) - int(group["alignment_start"]) + 1
        if position > left and total + size > target_units:
            batches.append((left, position - 1))
            left = position
            total = 0
        total += size
    batches.append((left, len(groups) - 1))
    return batches


def render_local_payload(
    master: str,
    units: list[Any],
    groups: list[dict[str, Any]],
    protection: dict[str, Any],
    left: int,
    right: int,
) -> str:
    context_left = max(0, left - 1)
    context_right = min(len(groups) - 1, right + 1)
    window_start = int(groups[context_left]["alignment_start"])
    window_end = int(groups[context_right]["alignment_end"])
    core_start = int(groups[left]["alignment_start"])
    core_end = int(groups[right]["alignment_end"])
    positions = {int(unit.index): pos for pos, unit in enumerate(units)}
    start_pos = positions[window_start]
    end_pos = positions[window_end]
    ranges = _unit_original_ranges(master, units)
    char_start = ranges[start_pos][0]
    char_end = (
        ranges[end_pos + 1][0] if end_pos + 1 < len(ranges) else len(master)
    )
    local_master = master[char_start:char_end].strip()
    local_units = units[start_pos : end_pos + 1]
    alignment = "\n".join(
        f"{unit.index}\t{unit.start:.3f}\t{unit.end:.3f}\t{unit.text}\t"
        f"{unit.sentence_id if unit.sentence_id is not None else '-'}\t"
        f"{1 if unit.sentence_start else 0}\t{1 if unit.sentence_end else 0}"
        for unit in local_units
    )
    local_spans = [
        span
        for span in protection["spans"]
        if int(span["alignment_end"]) >= window_start
        and int(span["alignment_start"]) <= window_end
    ]
    preferred = [
        row
        for row in protection["preferred_breaks_after"]
        if window_start <= int(row["after_alignment"]) < window_end
    ]
    forbidden = [
        row
        for row in protection["forbidden_breaks_after"]
        if window_start <= int(row["after_alignment"]) < window_end
    ]
    return "\n\n".join(
        [
            "# OWNERSHIP",
            (
                f"core_group_ids: {groups[left]['group_id']}.."
                f"{groups[right]['group_id']}\n"
                f"core_alignment: {core_start}..{core_end}\n"
                "Only output core groups. Neighbour groups are read-only context."
            ),
            "# LOCAL_MASTER_TRANSCRIPT\n```text\n" + local_master + "\n```",
            (
                "# LOCAL_ALIGNMENT\n"
                "index / start / end / text / sentence_id / sentence_start / "
                "sentence_end\n```tsv\n"
                + alignment
                + "\n```"
            ),
            "# CONTEXT_AND_CORE_GROUPS\n"
            + json.dumps(
                {
                    "groups": groups[context_left : context_right + 1],
                    "core_group_ids": [
                        group["group_id"] for group in groups[left : right + 1]
                    ],
                },
                ensure_ascii=False,
            ),
            "# P1_PROTECTION\n"
            + json.dumps(
                {
                    "spans": local_spans,
                    "preferred_breaks_after": preferred,
                    "forbidden_breaks_after": forbidden,
                },
                ensure_ascii=False,
            ),
        ]
    )


def normalize_local_result(
    value: dict[str, Any],
    core_groups: list[dict[str, Any]],
    hard_spans: list[dict[str, Any]],
) -> tuple[dict[str, list[int]], dict[str, Any]]:
    if (
        value.get("schema_version")
        not in {None, "substar.stage1.local-candidates.v1"}
        or not isinstance(value.get("candidates"), list)
    ):
        raise SegmentationError("P3 schema_version 错误")
    expected = [str(group["group_id"]) for group in core_groups]
    candidates = value.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise SegmentationError("P3 缺少候选")
    normalized_candidates: list[dict[str, Any]] = []
    for raw_candidate in candidates:
        rows = raw_candidate.get("groups", [])
        if not isinstance(rows, list):
            continue
        by_id = {
            str(row.get("group_id")): row
            for row in rows
            if isinstance(row, dict)
        }
        if set(by_id) != set(expected):
            continue
        group_breaks: dict[str, list[int]] = {}
        valid_candidate = True
        for group in core_groups:
            group_id = str(group["group_id"])
            start = int(group["alignment_start"])
            end = int(group["alignment_end"])
            raw_breaks = by_id[group_id].get("line_breaks_after", [])
            if not isinstance(raw_breaks, list) or any(
                not isinstance(item, int) or not start <= item < end
                for item in raw_breaks
            ):
                # Never silently discard relative, out-of-range, or malformed
                # indices. That made an invalid model plan look like a valid
                # unsplit group and hid the actual API contract failure.
                valid_candidate = False
                break
            breaks = sorted(set(raw_breaks))
            for cut in breaks:
                if any(
                    int(span["alignment_start"])
                    <= cut
                    < int(span["alignment_end"])
                    for span in hard_spans
                ):
                    valid_candidate = False
                    break
            if not valid_candidate:
                break
            group_breaks[group_id] = breaks
        if valid_candidate:
            normalized_candidates.append(
                {
                    "candidate_id": str(
                        raw_candidate.get(
                            "candidate_id",
                            f"candidate_{len(normalized_candidates) + 1}",
                        )
                    ),
                    "group_breaks": group_breaks,
                    "tradeoffs": raw_candidate.get("tradeoffs", []),
                }
            )
    if not normalized_candidates:
        raise SegmentationError("P3 没有覆盖全部核心组的合法候选")
    selected_id = str(value.get("selected_candidate_id", ""))
    selected = next(
        (
            item
            for item in normalized_candidates
            if item["candidate_id"] == selected_id
        ),
        normalized_candidates[0],
    )
    audit = {
        "schema_version": "substar.stage1.local-candidates.v1",
        "candidates": normalized_candidates,
        "selected_candidate_id": selected["candidate_id"],
        "uncertain_boundaries": value.get("uncertain_boundaries", []),
        "forced_by_hard_limit": value.get("forced_by_hard_limit", []),
    }
    return selected["group_breaks"], audit


def select_hard_valid_local_candidate(
    value: dict[str, Any],
    core_groups: list[dict[str, Any]],
    hard_spans: list[dict[str, Any]],
    *,
    master: str,
    units: list[Any],
    protection: dict[str, Any],
) -> tuple[dict[str, list[int]], dict[str, Any]]:
    """Select only among model candidates that already satisfy hard layout.

    This validator never invents or moves a boundary. If all candidates are
    invalid, the caller may request one finite model contract repair.
    """

    _, audit = normalize_local_result(value, core_groups, hard_spans)
    core_start = int(core_groups[0]["alignment_start"])
    core_end = int(core_groups[-1]["alignment_end"])
    local_units = [
        unit
        for unit in units
        if core_start <= int(unit.index) <= core_end
    ]
    unit_positions = {int(unit.index): position for position, unit in enumerate(units)}
    original_ranges = _unit_original_ranges(master, units)
    start_position = unit_positions[core_start]
    end_position = unit_positions[core_end]
    char_start = original_ranges[start_position][0]
    char_end = (
        original_ranges[end_position + 1][0]
        if end_position + 1 < len(original_ranges)
        else len(master)
    )
    local_master = master[char_start:char_end].strip()
    valid_candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in audit["candidates"]:
        cuts = cuts_from_group_breaks(
            core_groups,
            candidate["group_breaks"],
            core_end,
        )
        plan = build_plan_from_cuts(
            cuts,
            local_units,
            protection,
            core_groups,
        )
        result = evaluate_direct_plan(
            local_master,
            local_units,
            plan,
            review_confidence=0.72,
            **source_punctuation_kwargs(),
        )
        if result.valid:
            valid_candidates.append(candidate)
        else:
            rejected.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "issues": result.issues[:20],
                }
            )
    if not valid_candidates:
        raise SegmentationError(
            "P3 所有模型候选均违反字符/结构硬约束："
            + json.dumps(rejected, ensure_ascii=False)
        )
    requested_id = str(audit["selected_candidate_id"])
    selected = next(
        (
            candidate
            for candidate in valid_candidates
            if candidate["candidate_id"] == requested_id
        ),
        valid_candidates[0],
    )
    audit["hard_invalid_candidates"] = rejected
    if selected["candidate_id"] != requested_id:
        audit["selected_overridden_by_hard_contract"] = {
            "requested_candidate_id": requested_id,
            "selected_candidate_id": selected["candidate_id"],
        }
    audit["selected_candidate_id"] = selected["candidate_id"]
    return selected["group_breaks"], audit


def cuts_from_group_breaks(
    meaning_groups: list[dict[str, Any]],
    group_breaks: dict[str, list[int]],
    last_index: int,
) -> set[int]:
    cuts: set[int] = set()
    for group in meaning_groups:
        cuts.update(group_breaks.get(str(group["group_id"]), []))
        end = int(group["alignment_end"])
        if end < last_index:
            cuts.add(end)
    return cuts


def build_plan_from_cuts(
    cuts: set[int],
    units: list[Any],
    protection: dict[str, Any],
    meaning_groups: list[dict[str, Any]],
) -> dict[str, Any]:
    first = int(units[0].index)
    last = int(units[-1].index)
    valid_cuts = sorted(cut for cut in cuts if first <= cut < last)
    # P2/P2mix meaning groups may intentionally omit continuity metadata.  The
    # current one-step contract keeps that field out of the model response;
    # retain it when an older/explicit producer supplies it, otherwise let the
    # display-boundary default below apply.
    original_relations = {
        int(group["alignment_end"]): copy.deepcopy(group["continuity_after"])
        for group in meaning_groups
        if isinstance(group.get("continuity_after"), dict)
    }
    spans = protection["spans"]
    groups: list[dict[str, Any]] = []
    start = first
    for number, end in enumerate(valid_cuts + [last], start=1):
        contained = [
            copy.deepcopy(span)
            for span in spans
            if start
            <= int(span["alignment_start"])
            <= int(span["alignment_end"])
            <= end
        ]
        relation = original_relations.get(
            end,
            {
                "relation": "continuous",
                "confidence": 1.0,
                "reason": "P3 internal display boundary",
                "speaker_transition": "unknown",
            },
        )
        groups.append(
            {
                "group_id": f"g{number:04d}",
                "alignment_start": start,
                "alignment_end": end,
                "line_breaks_after": [],
                "protected_spans": contained,
                "deletions": [],
                "corrections": [],
                "confidence": 0.8,
                "needs_review": False,
                "continuity_after": relation,
                "reason": "P2 meaning-group map; P3 display cut",
            }
        )
        start = end + 1
    groups[-1]["continuity_after"] = {
        "relation": "separate",
        "confidence": 1.0,
        "reason": "terminal programme boundary",
        "speaker_transition": "unknown",
    }
    return {
        "schema_version": "substar.stage1.direct.v1",
        "source_language": "Auto",
        "groups": groups,
        "coverage_check": {"complete": True, "ordered": True},
    }


def flatten_plan_cuts(plan: dict[str, Any], last_index: int) -> set[int]:
    cuts: set[int] = set()
    for group in plan.get("groups", []):
        cuts.update(
            int(item)
            for item in group.get("line_breaks_after", [])
            if isinstance(item, int)
        )
        end = int(group["alignment_end"])
        if end < last_index:
            cuts.add(end)
    return cuts


def hard_span_cut(
    cut: int,
    protection: dict[str, Any],
) -> bool:
    return any(
        span["protection_level"] == "hard"
        and int(span["alignment_start"]) <= cut < int(span["alignment_end"])
        for span in protection["spans"]
    )


def normalize_p4(
    value: dict[str, Any],
    *,
    max_transactions: int | None = None,
    max_break_edits_per_transaction: int = 6,
) -> dict[str, Any]:
    if value.get("schema_version") != "substar.stage1.editor-patch.v1":
        raise SegmentationError("P4 schema_version 错误")
    transactions: list[dict[str, Any]] = []
    transaction_ids: set[str] = set()
    for number, raw in enumerate(value.get("transactions", []), start=1):
        if not isinstance(raw, dict):
            continue

        def integers(name: str) -> list[int]:
            return sorted(
                {
                    int(item)
                    for item in raw.get(name, [])
                    if isinstance(item, int)
                }
            )

        transaction_id = str(raw.get("transaction_id", f"t{number:04d}"))
        if transaction_id in transaction_ids:
            raise SegmentationError(f"P4 transaction_id 重复：{transaction_id}")
        transaction_ids.add(transaction_id)
        remove = integers("remove_breaks_after")
        add = integers("add_breaks_after")
        if set(remove) & set(add):
            raise SegmentationError(
                f"P4事务自相消：{transaction_id}"
            )
        if len(remove) + len(add) > max_break_edits_per_transaction:
            raise SegmentationError(
                f"P4事务修改范围过大：{transaction_id}"
            )
        transactions.append(
            {
                "transaction_id": transaction_id,
                "operation": str(raw.get("operation", "move_boundary")),
                "boundary_level": str(
                    raw.get("boundary_level", "display_cue")
                ),
                "reason_code": str(raw.get("reason_code", "structural_risk")),
                "remove_breaks_after": remove,
                "add_breaks_after": add,
                "reason": str(raw.get("reason", "")),
                "confidence": max(
                    0.0, min(1.0, float(raw.get("confidence", 0.8)))
                ),
            }
        )
    if max_transactions is not None and len(transactions) > max_transactions:
        raise SegmentationError(
            f"P4事务超过预算：{len(transactions)}/{max_transactions}"
        )
    return {
        "schema_version": "substar.stage1.editor-patch.v1",
        "overall_risk": str(value.get("overall_risk", "unknown")),
        "transactions": transactions,
        "remaining_review": value.get("remaining_review", []),
    }


def apply_editor_transactions(
    initial_cuts: set[int],
    patch: dict[str, Any],
    *,
    master: str,
    units: list[Any],
    protection: dict[str, Any],
    meaning_groups: list[dict[str, Any]],
) -> tuple[set[int], list[dict[str, Any]]]:
    current = set(initial_cuts)
    first = int(units[0].index)
    last = int(units[-1].index)
    results: list[dict[str, Any]] = []
    current_plan = build_plan_from_cuts(
        current, units, protection, meaning_groups
    )
    current_evaluation = evaluate_direct_plan(
        master,
        units,
        current_plan,
        review_confidence=0.72,
        **source_punctuation_kwargs(),
    )

    def critical_notices(evaluation: Any) -> list[dict[str, Any]]:
        return [
            item
            for item in evaluation.review_notices
            if str(item.get("code", "")) in CRITICAL_REVIEW_CODES
        ]

    def notice_boundaries(evaluation: Any) -> set[int]:
        boundaries: set[int] = set()
        for item in critical_notices(evaluation):
            value = item.get("after_alignment")
            if not isinstance(value, int):
                value = item.get("alignment_end")
            if isinstance(value, int):
                boundaries.add(value)
        return boundaries

    for transaction in patch["transactions"]:
        remove = set(transaction["remove_breaks_after"])
        add = set(transaction["add_breaks_after"])
        entry = {**transaction, "accepted": False, "rejection_reason": None}
        if not remove.issubset(current):
            entry["rejection_reason"] = "remove_break_not_present"
            results.append(entry)
            continue
        if any(cut < first or cut >= last for cut in add):
            entry["rejection_reason"] = "add_break_out_of_range"
            results.append(entry)
            continue
        if any(hard_span_cut(cut, protection) for cut in add):
            entry["rejection_reason"] = "add_break_inside_hard_span"
            results.append(entry)
            continue
        candidate_cuts = (current - remove) | add
        candidate_plan = build_plan_from_cuts(
            candidate_cuts, units, protection, meaning_groups
        )
        evaluation = evaluate_direct_plan(
            master,
            units,
            candidate_plan,
            review_confidence=0.72,
            **source_punctuation_kwargs(),
        )
        if not evaluation.valid:
            entry["rejection_reason"] = "hard_validation_failed"
            entry["hard_issues"] = evaluation.issues[:10]
            results.append(entry)
            continue
        targeted_existing_risk = bool(
            remove & notice_boundaries(current_evaluation)
        )
        if (
            targeted_existing_risk
            and len(critical_notices(evaluation))
            >= len(critical_notices(current_evaluation))
        ):
            entry["rejection_reason"] = "targeted_soft_risk_not_reduced"
            entry["critical_before"] = len(
                critical_notices(current_evaluation)
            )
            entry["critical_after"] = len(critical_notices(evaluation))
            results.append(entry)
            continue
        current = candidate_cuts
        current_evaluation = evaluation
        entry["accepted"] = True
        results.append(entry)
    return current, results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Flash保护/意义组/并发候选 + Pro全片事务化总编"
    )
    parser.add_argument("material", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--api-key-env", default="SUBSTAR_LLM_API_KEY")
    parser.add_argument("--flash-model", default="deepseek-v4-flash")
    parser.add_argument("--pro-model", default="deepseek-v4-pro")
    parser.add_argument(
        "--p2-model",
        help="P2 全片意义组模型；默认沿用 --pro-model",
    )
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--p1-core-units", type=int, default=200)
    parser.add_argument("--p1-overlap-units", type=int, default=48)
    parser.add_argument("--p3-target-units", type=int, default=180)
    parser.add_argument(
        "--target-units",
        type=int,
        help="旧参数兼容：若提供则覆盖 --p3-target-units",
    )
    parser.add_argument("--p4-max-transactions", type=int, default=12)
    parser.add_argument("--disable-p5", action="store_true")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--max-tokens", type=int, default=128000)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    p2_model = args.p2_model or args.pro_model

    api_key, key_source = resolve_api_key(args.api_key_env)
    if not api_key:
        raise RuntimeError("未配置 Stage1 LLM API key")
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    material = read(args.material)
    master = extract_master(material)
    units = extract_alignment(material)
    first = int(units[0].index)
    last = int(units[-1].index)

    p1_path = output / "p1_protection.json"
    if args.resume and p1_path.exists():
        p1 = json.loads(p1_path.read_text(encoding="utf-8"))
        p1_call = json.loads(
            (output / "p1_api_call.json").read_text(encoding="utf-8")
        )
    else:
        p1_dir = output / "p1_windows"
        p1_dir.mkdir(parents=True, exist_ok=True)
        windows = build_p1_windows(
            units,
            core_units=args.p1_core_units,
            overlap_units=args.p1_overlap_units,
        )

        def run_p1_window(
            number: int, window: dict[str, int]
        ) -> tuple[int, dict[str, Any], dict[str, Any]]:
            raw_path = p1_dir / f"window_{number:03d}_raw.json"
            normalized_path = p1_dir / f"window_{number:03d}.json"
            call_path = p1_dir / f"window_{number:03d}_api_call.json"
            payload = render_p1_window_payload(units, window)
            context_units = units[
                window["context_left_pos"] : window["context_right_pos"] + 1
            ]
            if (
                args.resume
                and raw_path.exists()
                and normalized_path.exists()
                and call_path.exists()
            ):
                return (
                    number,
                    json.loads(normalized_path.read_text(encoding="utf-8")),
                    json.loads(call_path.read_text(encoding="utf-8")),
                )
            raw, telemetry = call_model(
                base_url=args.base_url,
                api_key=api_key,
                model=args.flash_model,
                system_prompt=system_prompt("p1", payload),
                user_payload=payload,
                timeout=args.timeout,
                max_tokens=min(args.max_tokens, 32000),
                json_mode=True,
                thinking_mode="enabled",
                reasoning_effort="high",
                request_attempts=2,
            )
            write_json(raw_path, raw)
            try:
                normalized = normalize_p1(
                    raw,
                    context_units,
                    require_coverage=True,
                    expected_coverage=(
                        window["core_start"],
                        window["core_end"],
                    ),
                    owner_core=(
                        window["core_start"],
                        window["core_end"],
                    ),
                )
            except SegmentationError as exc:
                repair_raw, repair_telemetry = call_model(
                    base_url=args.base_url,
                    api_key=api_key,
                    model=args.flash_model,
                    system_prompt="\n\n".join(
                        [
                            system_prompt("p1", payload),
                            "# ONE_FINITE_CONTRACT_REPAIR",
                            "只修复上一响应的索引、head_index、核心区覆盖或JSON契约。"
                            "不得扩大任务，不得解释。",
                        ]
                    ),
                    user_payload="\n\n".join(
                        [
                            payload,
                            "# REJECTED_RESPONSE\n"
                            + json.dumps(raw, ensure_ascii=False),
                            "# CONTRACT_ERROR\n" + str(exc),
                        ]
                    ),
                    timeout=args.timeout,
                    max_tokens=min(args.max_tokens, 32000),
                    json_mode=True,
                    thinking_mode="enabled",
                    reasoning_effort="high",
                    request_attempts=1,
                )
                write_json(
                    p1_dir / f"window_{number:03d}_repair_raw.json",
                    repair_raw,
                )
                write_json(
                    p1_dir / f"window_{number:03d}_repair_api_call.json",
                    repair_telemetry,
                )
                raw = repair_raw
                telemetry = repair_telemetry
                normalized = normalize_p1(
                    raw,
                    context_units,
                    require_coverage=True,
                    expected_coverage=(
                        window["core_start"],
                        window["core_end"],
                    ),
                    owner_core=(
                        window["core_start"],
                        window["core_end"],
                    ),
                )
            write_json(normalized_path, normalized)
            write_json(call_path, telemetry)
            return number, normalized, telemetry

        p1_results: list[
            tuple[int, dict[str, Any], dict[str, Any]]
        ] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(args.workers, len(windows)))
        ) as executor:
            futures = [
                executor.submit(run_p1_window, number, window)
                for number, window in enumerate(windows, start=1)
            ]
            for future in concurrent.futures.as_completed(futures):
                p1_results.append(future.result())
        p1_results.sort(key=lambda item: item[0])
        p1, p1_merge_audit = merge_p1_windows(
            [item[1] for item in p1_results], units
        )
        p1_call = {
            "mode": "windowed_parallel",
            "model": args.flash_model,
            "window_count": len(windows),
            "core_units": args.p1_core_units,
            "overlap_units": args.p1_overlap_units,
            "duration_seconds": max(
                (
                    float(item[2].get("duration_seconds", 0))
                    for item in p1_results
                ),
                default=0,
            ),
            "sum_api_duration_seconds": sum(
                float(item[2].get("duration_seconds", 0))
                for item in p1_results
            ),
        }
        write_json(output / "p1_window_merge_audit.json", p1_merge_audit)
        write_json(output / "p1_raw_response.json", p1)
        write_json(p1_path, p1)
        write_json(output / "p1_api_call.json", p1_call)

    p2_path = output / "p2_meaning_groups.json"
    p2_repair_raw_path = output / "p2_density_repair_raw.json"
    p2_repair_call_path = output / "p2_density_repair_api_call.json"
    p1_p2_payload = protection_for_editor(p1)
    write_json(output / "p1_p2_payload.json", p1_p2_payload)
    p2_user_payload = "\n\n".join(
        [
            "# FULL_PROGRAM\n" + material,
            "# P1_ACTIONABLE_PROTECTION\n"
            + json.dumps(p1_p2_payload, ensure_ascii=False),
            (
                "# PROJECTION_CONTRACT\n"
                "超过显示硬上限的 strong_soft 只留在完整 P1 审计，"
                "不作为要求整体保留的 P2 输入；其内部 hard/可执行"
                " strong_soft 子结构仍然保留。"
            ),
        ]
    )
    if args.resume and p2_path.exists():
        p2_raw_path = output / "p2_raw_response.json"
        p2_raw = json.loads(
            (
                p2_raw_path
                if p2_raw_path.exists()
                else p2_path
            ).read_text(encoding="utf-8")
        )
        p2_call = json.loads((output / "p2_api_call.json").read_text(encoding="utf-8"))
    else:
        p2_raw, p2_call = call_streaming_model(
            base_url=args.base_url,
            api_key=api_key,
            model=p2_model,
            system=system_prompt("p2", material),
            user=p2_user_payload,
            timeout=args.timeout,
            max_tokens=args.max_tokens,
            reasoning_effort="max",
            raw_response_path=output / "p2_raw_response.txt",
        )
        write_json(output / "p2_raw_response.json", p2_raw)
        write_json(output / "p2_api_call.json", p2_call)
    p2, p2_merge_actions = normalize_p2(p2_raw, units, p1)
    density_violations = p2_density_violations(p2)
    if density_violations:
        if args.resume and p2_repair_raw_path.exists():
            repair_raw = json.loads(
                p2_repair_raw_path.read_text(encoding="utf-8")
            )
            repair_call = (
                json.loads(
                    p2_repair_call_path.read_text(encoding="utf-8")
                )
                if p2_repair_call_path.exists()
                else {"mode": "resumed_density_repair"}
            )
        else:
            repair_raw, repair_call = call_streaming_model(
                base_url=args.base_url,
                api_key=api_key,
                model=p2_model,
                system="\n\n".join(
                    [
                        system_prompt("p2", material),
                        "# ONE_FINITE_DENSITY_REPAIR",
                        "上一响应违反意义组密度契约。只重新划分多中心或"
                        "超跨度组并保持全片连续唯一覆盖；不要按固定组数或"
                        "等长切分，不生成显示 cue，不解释。",
                    ]
                ),
                user="\n\n".join(
                    [
                        p2_user_payload,
                        "# REJECTED_P2\n"
                        + json.dumps(p2_raw, ensure_ascii=False),
                        "# DENSITY_VIOLATIONS\n"
                        + json.dumps(
                            density_violations, ensure_ascii=False
                        ),
                    ]
                ),
                timeout=args.timeout,
                max_tokens=args.max_tokens,
                reasoning_effort="max",
                raw_response_path=output / "p2_density_repair_raw_response.txt",
            )
            write_json(p2_repair_raw_path, repair_raw)
            write_json(p2_repair_call_path, repair_call)
        p2_raw = repair_raw
        p2_call = repair_call
        p2, p2_merge_actions = normalize_p2(p2_raw, units, p1)
        density_violations = p2_density_violations(p2)
        if density_violations:
            write_json(
                output / "p2_density_failure.json",
                {"violations": density_violations},
            )
            raise SegmentationError(
                "P2 一次有限密度修复后仍不合格："
                f"{density_violations[:10]}"
            )
    write_json(p2_path, p2)
    write_json(output / "p2_api_call.json", p2_call)
    write_json(
        output / "p2_protection_merge_actions.json",
        {"actions": p2_merge_actions},
    )
    write_json(
        output / "p2_density_audit.json",
        {
            "schema_version": "substar.stage1.p2-density-audit.v1",
            "model": p2_model,
            "group_count": len(p2["groups"]),
            "max_group_units": max(
                int(group["alignment_end"])
                - int(group["alignment_start"])
                + 1
                for group in p2["groups"]
            ),
            "violations": density_violations,
            "repair_used": p2_repair_raw_path.exists(),
        },
    )

    meaning_groups = p2["groups"]
    p3_target_units = (
        args.target_units
        if args.target_units is not None
        else args.p3_target_units
    )
    batches = group_batches(meaning_groups, max(40, p3_target_units))
    p3_dir = output / "p3_batches"
    p3_dir.mkdir(parents=True, exist_ok=True)

    def run_batch(batch_number: int, left: int, right: int) -> tuple[int, dict[str, list[int]], dict[str, Any]]:
        batch_path = p3_dir / f"batch_{batch_number:03d}.json"
        raw_path = p3_dir / f"batch_{batch_number:03d}_raw.json"
        call_path = p3_dir / f"batch_{batch_number:03d}_api_call.json"
        repair_raw_path = p3_dir / f"batch_{batch_number:03d}_repair_raw.json"
        repair_call_path = p3_dir / f"batch_{batch_number:03d}_repair_api_call.json"
        core = meaning_groups[left : right + 1]
        payload = render_local_payload(
            master, units, meaning_groups, p1, left, right
        )
        used_repair_response = False
        contract_repair_reason = ""
        if args.resume and batch_path.exists() and call_path.exists():
            audit = json.loads(batch_path.read_text(encoding="utf-8"))
            telemetry = json.loads(call_path.read_text(encoding="utf-8"))
            selected_id = str(audit.get("selected_candidate_id", ""))
            selected_row = next(
                (
                    item
                    for item in audit.get("candidates", [])
                    if str(item.get("candidate_id")) == selected_id
                ),
                None,
            )
            if selected_row and isinstance(selected_row.get("group_breaks"), dict):
                return batch_number, {
                    str(group_id): [
                        int(cut)
                        for cut in breaks
                        if isinstance(cut, int)
                    ]
                    for group_id, breaks in selected_row["group_breaks"].items()
                }, audit
            if raw_path.exists():
                if repair_raw_path.exists():
                    raw = json.loads(repair_raw_path.read_text(encoding="utf-8"))
                    used_repair_response = True
                    contract_repair_reason = "resumed finite repair response"
                else:
                    raw = json.loads(raw_path.read_text(encoding="utf-8"))
            else:
                raise SegmentationError(
                    f"P3 batch {batch_number} 缺少可恢复的原始候选"
                )
        elif args.resume and raw_path.exists() and call_path.exists():
            if repair_raw_path.exists():
                raw = json.loads(repair_raw_path.read_text(encoding="utf-8"))
                used_repair_response = True
                contract_repair_reason = "resumed finite repair response"
            else:
                raw = json.loads(raw_path.read_text(encoding="utf-8"))
            telemetry = json.loads(call_path.read_text(encoding="utf-8"))
        else:
            try:
                raw, telemetry = call_model(
                    base_url=args.base_url,
                    api_key=api_key,
                    model=args.flash_model,
                    system_prompt=system_prompt("p3", payload),
                    user_payload=payload,
                    timeout=args.timeout,
                    max_tokens=min(args.max_tokens, 32000),
                    json_mode=True,
                    thinking_mode="enabled",
                    reasoning_effort="high",
                    request_attempts=2,
                )
                write_json(raw_path, raw)
                write_json(call_path, telemetry)
            except SegmentationError as exc:
                # An empty/non-JSON body has no candidate object to normalize,
                # but it consumes the same single contract-repair budget.
                raw, telemetry = call_model(
                    base_url=args.base_url,
                    api_key=api_key,
                    model=args.flash_model,
                    system_prompt="\n\n".join(
                        [
                            system_prompt("p3", payload),
                            "# ONE_FINITE_CONTRACT_REPAIR",
                            "上一响应为空或不是有效 JSON。只返回完整合法的 JSON "
                            "候选对象，覆盖全部核心组，不切入 hard。不得解释。",
                        ]
                    ),
                    user_payload="\n\n".join(
                        [
                            payload,
                            "# CONTRACT_ERROR\n" + str(exc),
                        ]
                    ),
                    timeout=args.timeout,
                    max_tokens=min(args.max_tokens, 32000),
                    json_mode=True,
                    thinking_mode="enabled",
                    reasoning_effort="high",
                    request_attempts=1,
                )
                write_json(repair_raw_path, raw)
                write_json(repair_call_path, telemetry)
                # Keep the standard paths present for deterministic resume.
                write_json(raw_path, raw)
                write_json(call_path, telemetry)
                used_repair_response = True
                contract_repair_reason = str(exc)
        hard_spans = [
            span
            for span in p1["spans"]
            if span["protection_level"] == "hard"
            and int(span["alignment_end"]) >= int(core[0]["alignment_start"])
            and int(span["alignment_start"]) <= int(core[-1]["alignment_end"])
        ]
        try:
            selected, audit = select_hard_valid_local_candidate(
                raw,
                core,
                hard_spans,
                master=master,
                units=units,
                protection=p1,
            )
            if used_repair_response:
                audit["contract_repaired"] = True
                audit["contract_repair_reason"] = contract_repair_reason
        except SegmentationError as exc:
            if used_repair_response:
                raise
            repair_raw, repair_telemetry = call_model(
                base_url=args.base_url,
                api_key=api_key,
                model=args.flash_model,
                system_prompt="\n\n".join(
                    [
                        system_prompt("p3", payload),
                        "# ONE_FINITE_CONTRACT_REPAIR",
                        "上一响应没有任何一套通过程序硬契约。只修复 JSON 候选："
                        "完整覆盖全部核心组，不切入 hard，不遗漏、重复或改序。"
                        "每个原 group_id 恰好一行且不得新建子组；"
                        "line_breaks_after 只用全片绝对 alignment index，"
                        "严格小于该组 alignment_end。"
                        "不得重新解释任务，不得输出说明。",
                    ]
                ),
                user_payload="\n\n".join(
                    [
                        payload,
                        "# REJECTED_RESPONSE\n"
                        + json.dumps(raw, ensure_ascii=False),
                        "# CONTRACT_ERROR\n" + str(exc),
                    ]
                ),
                timeout=args.timeout,
                max_tokens=min(args.max_tokens, 32000),
                json_mode=True,
                thinking_mode="enabled",
                reasoning_effort="high",
                request_attempts=1,
            )
            write_json(repair_raw_path, repair_raw)
            write_json(repair_call_path, repair_telemetry)
            selected, audit = select_hard_valid_local_candidate(
                repair_raw,
                core,
                hard_spans,
                master=master,
                units=units,
                protection=p1,
            )
            audit["contract_repaired"] = True
            audit["contract_repair_reason"] = str(exc)
        write_json(batch_path, audit)
        return batch_number, selected, audit

    batch_results: list[tuple[int, dict[str, list[int]], dict[str, Any]]] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, min(args.workers, len(batches)))
    ) as executor:
        futures = [
            executor.submit(run_batch, number, left, right)
            for number, (left, right) in enumerate(batches, start=1)
        ]
        for future in concurrent.futures.as_completed(futures):
            batch_results.append(future.result())
    batch_results.sort(key=lambda item: item[0])
    selected_breaks: dict[str, list[int]] = {}
    for _, group_breaks, _ in batch_results:
        overlap = set(selected_breaks) & set(group_breaks)
        if overlap:
            raise SegmentationError(f"P3 核心组所有权重叠：{sorted(overlap)}")
        selected_breaks.update(group_breaks)
    expected_group_ids = {str(group["group_id"]) for group in meaning_groups}
    if set(selected_breaks) != expected_group_ids:
        raise SegmentationError("P3 未完整覆盖 P2 意义组")

    p3_cuts = cuts_from_group_breaks(meaning_groups, selected_breaks, last)
    p3_plan = build_plan_from_cuts(p3_cuts, units, p1, meaning_groups)
    p3_result = evaluate_direct_plan(
        master,
        units,
        p3_plan,
        review_confidence=0.72,
        **source_punctuation_kwargs(),
    )
    hard_completion_actions: list[dict[str, Any]] = []
    if not p3_result.valid:
        raise SegmentationError(
            "P3 全片硬校验失败；禁止确定性补切："
            f"{p3_result.issues[:10]}"
        )
    write_json(output / "p3_initial_plan.json", p3_plan)
    write_json(
        output / "p3_validation.json",
        {
            **_direct_report(p3_result, repaired=False, attempts=0),
            "deterministic_hard_completion": [],
            "deterministic_hard_completion_policy": "forbidden",
        },
    )
    (output / "p3_source_draft.txt").write_text(
        p3_result.draft, encoding="utf-8"
    )
    p1_editor = protection_for_editor(p1)
    write_json(output / "p1_editor_payload.json", p1_editor)

    p4_path = output / "p4_editor_patch.json"
    p4_budget = max(
        1,
        min(
            int(args.p4_max_transactions),
            math.ceil(len(meaning_groups) * 0.05),
        ),
    )
    if args.resume and p4_path.exists():
        p4_raw = json.loads(p4_path.read_text(encoding="utf-8"))
        p4_call = json.loads((output / "p4_api_call.json").read_text(encoding="utf-8"))
    else:
        p4_raw, p4_call = call_streaming_model(
            base_url=args.base_url,
            api_key=api_key,
            model=args.pro_model,
            system=system_prompt("p4", material),
            user="\n\n".join(
                [
                    "# FULL_PROGRAM\n" + material,
                    "# P1_PROTECTION\n"
                    + json.dumps(p1_editor, ensure_ascii=False),
                    "# P2_MEANING_GROUPS\n"
                    + json.dumps(p2, ensure_ascii=False),
                    "# P3_CURRENT_CUTS\n"
                    + json.dumps(sorted(p3_cuts), ensure_ascii=False),
                    "# P3_SOURCE_DRAFT\n```text\n"
                    + p3_result.draft
                    + "\n```",
                    "# PROGRAM_RISK_NOTICES\n"
                    + json.dumps(
                        p3_result.review_notices, ensure_ascii=False
                    ),
                    (
                        "# EDIT_BUDGET\n"
                        f"最多 {p4_budget} 条事务；每条事务最多修改 6 个边界。"
                        "只处理明确硬错误或严重结构风险。"
                    ),
                ]
            ),
            timeout=args.timeout,
            max_tokens=min(args.max_tokens, 64000),
            reasoning_effort="max",
        )
    p4 = normalize_p4(
        p4_raw,
        max_transactions=p4_budget,
        max_break_edits_per_transaction=6,
    )
    write_json(p4_path, p4)
    write_json(output / "p4_api_call.json", p4_call)
    final_cuts, transaction_results = apply_editor_transactions(
        p3_cuts,
        p4,
        master=master,
        units=units,
        protection=p1,
        meaning_groups=meaning_groups,
    )
    final_plan = build_plan_from_cuts(final_cuts, units, p1, meaning_groups)
    final_result = evaluate_direct_plan(
        master,
        units,
        final_plan,
        review_confidence=0.72,
        **source_punctuation_kwargs(),
    )
    if not final_result.valid:
        raise SegmentationError(
            f"P4 应用后硬校验失败：{final_result.issues[:10]}"
        )
    remaining_critical = [
        item
        for item in final_result.review_notices
        if str(item.get("code", "")) in CRITICAL_REVIEW_CODES
    ]
    p5: dict[str, Any] | None = None
    p5_call: dict[str, Any] | None = None
    p5_transaction_results: list[dict[str, Any]] = []
    if (
        remaining_critical
        and not args.disable_p5
        and len(p4["transactions"]) < p4_budget
    ):
        p5_path = output / "p5_editor_patch.json"
        p5_call_path = output / "p5_api_call.json"
        if args.resume and p5_path.exists() and p5_call_path.exists():
            p5_raw = json.loads(p5_path.read_text(encoding="utf-8"))
            p5_call = json.loads(p5_call_path.read_text(encoding="utf-8"))
        else:
            p5_raw, p5_call = call_streaming_model(
                base_url=args.base_url,
                api_key=api_key,
                model=args.pro_model,
                system="\n\n".join(
                    [
                        system_prompt("p4", material),
                        "# FINITE_REVIEW_PASS",
                        "这是唯一一次剩余风险复核。只处理列出的风险。若程序误报，"
                        "不要修改该边界，把解释写入remaining_review；若确有问题，"
                        "输出能同时改善左右两侧的事务。不得扩大到其他区域。",
                    ]
                ),
                user="\n\n".join(
                    [
                        "# FULL_PROGRAM\n" + material,
                        "# P1_PROTECTION\n"
                        + json.dumps(p1_editor, ensure_ascii=False),
                        "# CURRENT_CUTS\n"
                        + json.dumps(sorted(final_cuts), ensure_ascii=False),
                        "# CURRENT_SOURCE_DRAFT\n```text\n"
                        + final_result.draft
                        + "\n```",
                        "# REMAINING_PROGRAM_RISKS\n"
                        + json.dumps(remaining_critical, ensure_ascii=False),
                    ]
                ),
                timeout=args.timeout,
                max_tokens=min(args.max_tokens, 32000),
                reasoning_effort="max",
            )
        p5 = normalize_p4(
            p5_raw,
            max_transactions=p4_budget - len(p4["transactions"]),
            max_break_edits_per_transaction=6,
        )
        write_json(p5_path, p5)
        if p5_call is not None:
            write_json(p5_call_path, p5_call)
        final_cuts, p5_transaction_results = apply_editor_transactions(
            final_cuts,
            p5,
            master=master,
            units=units,
            protection=p1,
            meaning_groups=meaning_groups,
        )
        final_plan = build_plan_from_cuts(
            final_cuts, units, p1, meaning_groups
        )
        final_result = evaluate_direct_plan(
            master,
            units,
            final_plan,
            review_confidence=0.72,
            **source_punctuation_kwargs(),
        )
        if not final_result.valid:
            raise SegmentationError(
                f"P5 应用后硬校验失败：{final_result.issues[:10]}"
            )
        write_json(
            output / "p5_transaction_results.json",
            {"transactions": p5_transaction_results},
        )
    write_json(
        output / "p4_transaction_results.json",
        {"transactions": transaction_results},
    )
    write_json(output / "stage1_direct_plan.json", final_plan)
    write_json(output / "stage1_display_layout_plan.json", final_plan)
    write_two_level_artifacts(output, master, units, final_plan)
    # write_two_level_artifacts preserves the historical display-plan view.
    # Keep the authoritative P2 translation units under a non-overloaded name.
    write_json(output / "stage1_translation_group_plan.json", p2)
    (output / "stage03A_source_draft.txt").write_text(
        final_result.draft, encoding="utf-8"
    )
    write_json(
        output / "stage1_validation.json",
        _direct_report(final_result, repaired=False, attempts=0),
    )
    level_counts = Counter(
        str(span["protection_level"]) for span in p1["spans"]
    )
    p2_group_sizes = [
        int(group["alignment_end"]) - int(group["alignment_start"]) + 1
        for group in meaning_groups
    ]
    contract_repair_batches = [
        number
        for number, _, audit in batch_results
        if audit.get("contract_repaired") is True
    ]
    stage_audit = {
        "schema_version": "substar.experiment.safe-contracts-audit.v1",
        "p1": {
            "coverage_check": p1.get("coverage_check"),
            "span_count": len(p1["spans"]),
            "level_counts": dict(level_counts),
            "infeasible_strong_soft_count": sum(
                1
                for span in p1["spans"]
                if span["protection_level"] == "strong_soft"
                and span["delivery_feasible_as_single_cue"] is False
            ),
            "editor_visible_span_count": len(p1_editor["spans"]),
            "protection_levels_mutated": False,
        },
        "p2": {
            "model": p2_model,
            "group_count": len(meaning_groups),
            "group_size_units": {
                "median": (
                    statistics.median(p2_group_sizes)
                    if p2_group_sizes
                    else 0
                ),
                "max": max(p2_group_sizes, default=0),
                "over_100_count": sum(
                    size > 100 for size in p2_group_sizes
                ),
            },
        },
        "p3": {
            "batch_count": len(batches),
            "contract_repair_count": len(contract_repair_batches),
            "contract_repair_batches": contract_repair_batches,
            "emergency_hard_completion_count": len(
                hard_completion_actions
            ),
        },
        "editor": {
            "p4_accepted_count": sum(
                1 for item in transaction_results if item["accepted"]
            ),
            "p4_rejected_count": sum(
                1 for item in transaction_results if not item["accepted"]
            ),
            "p5_accepted_count": sum(
                1 for item in p5_transaction_results if item["accepted"]
            ),
            "p5_rejected_count": sum(
                1 for item in p5_transaction_results if not item["accepted"]
            ),
        },
        "delivery": {
            "final_cue_count": len(final_plan["groups"]),
            "final_valid": final_result.valid,
            "review_notice_count": len(final_result.review_notices),
            "degraded_by_emergency_completion": bool(
                hard_completion_actions
            ),
        },
    }
    write_json(output / "safe_contracts_stage_audit.json", stage_audit)
    summary = {
        "schema_version": "substar.experiment.flash-map-pro-editor.v1",
        "material": str(args.material.resolve()),
        "flash_model": args.flash_model,
        "pro_model": args.pro_model,
        "p2_model": p2_model,
        "api_key_source": key_source,
        "p1_span_count": len(p1["spans"]),
        "p2_group_count": len(meaning_groups),
        "p2_protection_merges": len(p2_merge_actions),
        "p3_batch_count": len(batches),
        "p3_contract_repair_count": len(contract_repair_batches),
        "p3_initial_cue_count": len(p3_plan["groups"]),
        "p3_review_notice_count": len(p3_result.review_notices),
        "p3_hard_completion_count": len(hard_completion_actions),
        "p4_transaction_count": len(p4["transactions"]),
        "p4_accepted_count": sum(
            1 for item in transaction_results if item["accepted"]
        ),
        "p4_rejected_count": sum(
            1 for item in transaction_results if not item["accepted"]
        ),
        "p5_transaction_count": len(p5["transactions"]) if p5 else 0,
        "p5_accepted_count": sum(
            1 for item in p5_transaction_results if item["accepted"]
        ),
        "p5_rejected_count": sum(
            1 for item in p5_transaction_results if not item["accepted"]
        ),
        "final_cue_count": len(final_plan["groups"]),
        "final_review_notice_count": len(final_result.review_notices),
        "final_valid": final_result.valid,
        "durations_seconds": {
            "p1": p1_call.get("duration_seconds"),
            "p2": p2_call.get("duration_seconds"),
            "p3_max_parallel": max(
                (
                    float(
                        json.loads(
                            (
                                p3_dir
                                / f"batch_{number:03d}_api_call.json"
                            ).read_text(encoding="utf-8")
                        ).get("duration_seconds", 0)
                    )
                    for number in range(1, len(batches) + 1)
                ),
                default=0,
            ),
            "p4": p4_call.get("duration_seconds"),
            "p5": p5_call.get("duration_seconds") if p5_call else 0,
        },
    }
    write_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
