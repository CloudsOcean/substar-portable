from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


PROTECTION_LEVELS = {"hard", "strong_soft", "outer_soft"}


def normalize_analysis_v2(analysis: dict[str, Any]) -> dict[str, Any]:
    """Return the hierarchical v2 analysis shape.

    The compatibility path is intentionally lossless: legacy hard/soft spans
    are upgraded, never collapsed into the direct plan's flat representation.
    """

    normalized = copy.deepcopy(analysis)
    normalized["schema_version"] = "substar.segmentation.analysis.v1"
    for group in normalized.get("groups", []):
        spans = list(group.get("protected_spans", []))
        if not spans:
            for level, key in (
                ("hard", "hard_protected_spans"),
                ("strong_soft", "soft_protected_spans"),
            ):
                for number, span in enumerate(group.get(key, []), start=1):
                    spans.append(
                        {
                            "span_id": f"{group.get('group_id', 'g')}_{level}_{number}",
                            "alignment_start": int(
                                span.get("alignment_start", span.get("start_alignment", -1))
                            ),
                            "alignment_end": int(
                                span.get("alignment_end", span.get("end_alignment", -1))
                            ),
                            "category": str(span.get("category", key)),
                            "protection_level": level,
                            "reason": str(span.get("reason", "")),
                        }
                    )
        group["protected_spans"] = spans
        group.pop("hard_protected_spans", None)
        group.pop("soft_protected_spans", None)

        preferred = group.get("preferred_boundaries")
        if preferred is None:
            preferred = [
                {
                    "after_alignment": int(value),
                    "priority": "normal",
                    "relation": "legacy_preferred_cut",
                    "reason": "由旧 preferred_cut_after 升级",
                }
                for value in group.get("preferred_cut_after", [])
            ]
        group["preferred_boundaries"] = preferred
        group.pop("preferred_cut_after", None)

        forbidden = group.get("forbidden_boundaries")
        if forbidden is None:
            forbidden = [
                {
                    "after_alignment": int(value),
                    "reason": "由旧 forbidden_cut_after 升级",
                }
                for value in group.get("forbidden_cut_after", [])
            ]
        group["forbidden_boundaries"] = forbidden
        group.pop("forbidden_cut_after", None)
    return normalized


def derive_hierarchy(
    group: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate a laminar span family and derive parent/child identifiers."""

    spans = copy.deepcopy(list(group.get("protected_spans", [])))
    issues: list[dict[str, Any]] = []
    group_start = int(group.get("alignment_start", -1))
    group_end = int(group.get("alignment_end", -1))
    identifiers: set[str] = set()

    for position, span in enumerate(spans, start=1):
        span_id = str(span.get("span_id") or f"p{position:04d}")
        span["span_id"] = span_id
        if span_id in identifiers:
            issues.append({"code": "duplicate_span_id", "span_id": span_id})
        identifiers.add(span_id)
        start = span.get("alignment_start")
        end = span.get("alignment_end")
        level = str(span.get("protection_level", ""))
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or start < group_start
            or end > group_end
            or end < start
        ):
            issues.append({"code": "invalid_hierarchical_span", "span": span})
        if level not in PROTECTION_LEVELS:
            issues.append(
                {"code": "invalid_protection_level", "span_id": span_id, "level": level}
            )

    for left_position, left in enumerate(spans):
        left_start = int(left.get("alignment_start", -1))
        left_end = int(left.get("alignment_end", -1))
        for right in spans[left_position + 1 :]:
            right_start = int(right.get("alignment_start", -1))
            right_end = int(right.get("alignment_end", -1))
            overlaps = left_start <= right_end and right_start <= left_end
            nested = (
                left_start <= right_start <= right_end <= left_end
                or right_start <= left_start <= left_end <= right_end
            )
            if overlaps and not nested:
                issues.append(
                    {
                        "code": "crossing_protected_spans",
                        "left": left["span_id"],
                        "right": right["span_id"],
                    }
                )

    if issues:
        return spans, issues

    for span in spans:
        start = int(span["alignment_start"])
        end = int(span["alignment_end"])
        containers = [
            parent
            for parent in spans
            if parent["span_id"] != span["span_id"]
            and int(parent["alignment_start"]) <= start
            and end <= int(parent["alignment_end"])
        ]
        if containers:
            parent = min(
                containers,
                key=lambda item: int(item["alignment_end"])
                - int(item["alignment_start"]),
            )
            span["parent_span_id"] = parent["span_id"]
        else:
            span["parent_span_id"] = None

    children: dict[str, list[str]] = {str(span["span_id"]): [] for span in spans}
    for span in spans:
        parent_id = span.get("parent_span_id")
        if parent_id:
            children[str(parent_id)].append(str(span["span_id"]))
    for span in spans:
        span["child_span_ids"] = children[str(span["span_id"])]
    return spans, []


def hierarchical_analysis_issues(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for group in analysis.get("groups", []):
        _, group_issues = derive_hierarchy(group)
        issues.extend(
            {**issue, "group_id": group.get("group_id")} for issue in group_issues
        )
    return issues


def hard_spans(group: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        span
        for span in group.get("protected_spans", [])
        if span.get("protection_level") == "hard"
    ]


def augment_structural_boundaries(analysis: dict[str, Any]) -> dict[str, Any]:
    """Derive high-value boundaries only from A1's own sibling structure.

    This does not recognize words or phrases. It makes the model's declared
    hierarchy operational so A2 cannot overlook a boundary between adjacent
    strong-soft siblings.
    """

    augmented = copy.deepcopy(analysis)
    for group in augmented.get("groups", []):
        spans, issues = derive_hierarchy(group)
        if issues:
            continue
        forbidden = {
            int(item["after_alignment"])
            for item in group.get("forbidden_boundaries", [])
            if isinstance(item, dict) and isinstance(item.get("after_alignment"), int)
        }
        existing_rows = {
            int(item["after_alignment"]): item
            for item in group.get("preferred_boundaries", [])
            if isinstance(item, dict) and isinstance(item.get("after_alignment"), int)
        }
        existing = set(existing_rows)
        sibling_sets: dict[str | None, list[dict[str, Any]]] = {}
        for span in spans:
            if span.get("protection_level") not in {"strong_soft", "outer_soft"}:
                continue
            sibling_sets.setdefault(span.get("parent_span_id"), []).append(span)
        additions: list[dict[str, Any]] = []
        for siblings in sibling_sets.values():
            siblings.sort(
                key=lambda span: (
                    int(span["alignment_start"]),
                    int(span["alignment_end"]),
                )
            )
            for left, right in zip(siblings, siblings[1:]):
                cut = int(left["alignment_end"])
                if (
                    cut + 1 != int(right["alignment_start"])
                    or "strong_soft"
                    not in {
                        str(left.get("protection_level")),
                        str(right.get("protection_level")),
                    }
                    or cut in forbidden
                ):
                    continue
                if any(
                    int(span["alignment_start"]) <= cut < int(span["alignment_end"])
                    for span in spans
                    if span.get("protection_level") == "hard"
                ):
                    continue
                if cut in existing:
                    existing_rows[cut]["priority"] = "high"
                    continue
                additions.append(
                    {
                        "after_alignment": cut,
                        "priority": "high",
                        "relation": "adjacent_compositional_siblings",
                        "reason": "A1 已声明的相邻完整句法成分边界",
                    }
                )
                existing.add(cut)
        group.setdefault("preferred_boundaries", []).extend(additions)
        group["preferred_boundaries"].sort(
            key=lambda item: int(item["after_alignment"])
        )
    return augmented


def cuts_fingerprint(plan: dict[str, Any]) -> dict[str, Any]:
    meaning = [
        [int(group["alignment_start"]), int(group["alignment_end"])]
        for group in plan.get("groups", [])
    ]
    cues = [
        {
            "group": [int(group["alignment_start"]), int(group["alignment_end"])],
            "cuts": [int(value) for value in group.get("line_breaks_after", [])],
        }
        for group in plan.get("groups", [])
    ]

    def digest(value: Any) -> str:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    return {
        "schema_version": "substar.segmentation.boundary-fingerprint.v1",
        "meaning_group_boundary_hash": digest(meaning),
        "cue_boundary_hash": digest(cues),
        "meaning_groups": meaning,
        "cue_boundaries": cues,
    }


def assert_fingerprint_unchanged(
    before: dict[str, Any],
    after_plan: dict[str, Any],
) -> None:
    after = cuts_fingerprint(after_plan)
    for key in ("meaning_group_boundary_hash", "cue_boundary_hash"):
        if before.get(key) != after.get(key):
            raise ValueError(f"A3 冻结边界在后续阶段发生变化：{key}")


def candidate_coverage_report(
    analysis: dict[str, Any],
    candidates: dict[str, Any],
    *,
    hard_character_limit: int = 55,
) -> dict[str, Any]:
    by_group = {
        str(group.get("group_id", "")): group
        for group in candidates.get("groups", [])
    }
    missing: list[dict[str, Any]] = []
    eligible = 0
    covered = 0
    omission_exemptions: list[dict[str, Any]] = []
    for analysis_group in analysis.get("groups", []):
        group_id = str(analysis_group.get("group_id", ""))
        rows = list(by_group.get(group_id, {}).get("candidates", []))
        plans = [
            {int(value) for value in row.get("cut_after_alignment", [])}
            for row in rows
        ]
        group_start = int(analysis_group.get("alignment_start", -1))
        group_end = int(analysis_group.get("alignment_end", -1))
        forbidden = {
            int(item["after_alignment"])
            for item in analysis_group.get("forbidden_boundaries", [])
            if isinstance(item, dict) and isinstance(item.get("after_alignment"), int)
        }
        for boundary in analysis_group.get("preferred_boundaries", []):
            if not isinstance(boundary, dict):
                continue
            cut = boundary.get("after_alignment")
            relation = str(boundary.get("relation", "")).strip().lower()
            if (
                not isinstance(cut, int)
                or cut < group_start
                or cut >= group_end
                or cut in forbidden
                or boundary.get("priority") != "high"
                or relation
                in {
                    "sentence_end",
                    "utterance_end",
                    "speaker_turn",
                    "speaker_change",
                }
            ):
                continue
            eligible += 1
            adopted = any(cut in plan for plan in plans)
            omitted = any(cut not in plan for plan in plans)
            if adopted and not omitted and rows:
                omission_hard_illegal = True
                for row in rows:
                    row_cuts = [
                        int(value)
                        for value in row.get("cut_after_alignment", [])
                    ]
                    cues = [str(value) for value in row.get("cues", [])]
                    if cut not in row_cuts:
                        omission_hard_illegal = False
                        break
                    position = row_cuts.index(cut)
                    if position + 1 >= len(cues):
                        omission_hard_illegal = False
                        break
                    merged_length = len(
                        (cues[position].rstrip() + " " + cues[position + 1].lstrip())
                    )
                    if merged_length <= hard_character_limit:
                        omission_hard_illegal = False
                        break
                if omission_hard_illegal:
                    omitted = True
                    omission_exemptions.append(
                        {
                            "group_id": group_id,
                            "after_alignment": cut,
                            "reason": "移除此边界会使所有现有候选超过字符硬上限",
                        }
                    )
            if adopted and omitted:
                covered += 1
            else:
                missing.append(
                    {
                        "group_id": group_id,
                        "after_alignment": cut,
                        "adopted": adopted,
                        "omitted": omitted,
                        "reason": boundary.get("reason", ""),
                    }
                )
    return {
        "schema_version": "substar.segmentation.candidate-coverage.v1",
        "eligible_high_value_boundaries": eligible,
        "covered_high_value_boundaries": covered,
        "coverage_rate": 1.0 if eligible == 0 else covered / eligible,
        "omission_exemptions": omission_exemptions,
        "missing": missing,
        "needs_supplement": bool(missing),
    }
