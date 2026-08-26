from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_stage1_pipeline import (  # noqa: E402
    Stage1PipelineError,
    _call_model_with_one_content_retry,
    _candidate_diversity_report,
    _direct_plan_from_blind_decision,
    _merge_candidate_supplement,
    assert_schema_version,
    blind_candidates,
    resolve_api_key,
    source_punctuation_kwargs,
    stage_system,
    write_json,
    write_two_level_artifacts,
)
from substar_core.stage1 import (  # noqa: E402
    comparison_normalize,
    extract_alignment,
    extract_master,
    project_annotations,
)
from substar_core.stage1_adaptive_chunking import (  # noqa: E402
    AdaptiveAnalysisChunk,
    build_adaptive_analysis_chunks,
    seam_seed_windows,
)
from substar_core.stage1_chunking import (  # noqa: E402
    SegmentationChunk,
    _unit_original_ranges,
    render_chunk_material,
)
from substar_core.stage1_direct import evaluate_direct_plan, merge_direct_plans  # noqa: E402
from substar_core.stage1_hierarchy import (  # noqa: E402
    augment_structural_boundaries,
    candidate_coverage_report,
    cuts_fingerprint,
    hierarchical_analysis_issues,
)


def _slice_material(
    master: str,
    units: list[Any],
    start: int,
    end: int,
) -> str:
    positions = {int(unit.index): position for position, unit in enumerate(units)}
    start_position = positions[start]
    end_position = positions[end]
    ranges = _unit_original_ranges(master, units)
    char_start = 0 if start_position == 0 else ranges[start_position][0]
    char_end = (
        len(master)
        if end_position == len(units) - 1
        else ranges[end_position + 1][0]
    )
    selected = units[start_position : end_position + 1]
    chunk = SegmentationChunk(
        chunk_id=f"range_{start}_{end}",
        master_text=master[char_start:char_end].strip(),
        units=selected,
        context_before="",
        context_after="",
        start_seconds=float(selected[0].start),
        end_seconds=float(selected[-1].end),
    )
    return render_chunk_material(chunk)


def _analysis_issues(
    analysis: dict[str, Any],
    *,
    start: int,
    end: int,
    master: str | None = None,
    units: list[Any] | None = None,
) -> list[dict[str, Any]]:
    issues = hierarchical_analysis_issues(analysis)
    expected = start
    for group in analysis.get("groups", []):
        group_start = group.get("alignment_start")
        group_end = group.get("alignment_end")
        if group_start != expected or not isinstance(group_end, int) or group_end < expected:
            issues.append(
                {
                    "code": "analysis_index_coverage",
                    "group_id": group.get("group_id"),
                    "expected_start": expected,
                    "actual_start": group_start,
                    "actual_end": group_end,
                }
            )
            break
        expected = group_end + 1
    if expected != end + 1:
        issues.append(
            {
                "code": "analysis_index_coverage",
                "expected_final": end,
                "actual_final": expected - 1,
            }
        )
    if master is not None and units is not None:
        hard_limit = int(
            source_punctuation_kwargs().get("english_hard_limit", 55)
        )
        for group in analysis.get("groups", []):
            group_start = int(group.get("alignment_start", -1))
            group_end = int(group.get("alignment_end", -1))
            if group_start < start or group_end > end or group_end < group_start:
                continue
            expected_text = _slice_master_text(
                master, units, group_start, group_end
            )
            if comparison_normalize(str(group.get("source_text", ""))) != comparison_normalize(
                expected_text
            ):
                issues.append(
                    {
                        "code": "analysis_source_text_mismatch",
                        "group_id": group.get("group_id"),
                        "alignment_start": group_start,
                        "alignment_end": group_end,
                    }
                )
            source_text = str(group.get("source_text", ""))
            terminal_count = len(
                re.findall(r"(?:[.!?。！？]+)(?:[\"'”’)]*)", source_text)
            )
            if len(source_text) > 280 and terminal_count >= 3:
                issues.append(
                    {
                        "code": "suspected_multi_center_group",
                        "group_id": group.get("group_id"),
                        "character_count": len(source_text),
                        "terminal_count": terminal_count,
                        "reason": "长组包含多个明确句末，需由 A1 复核是否存在多个独立话语中心",
                    }
                )
            spans = list(group.get("protected_spans", []))
            for span in spans:
                if span.get("protection_level") != "strong_soft":
                    continue
                span_start = int(span.get("alignment_start", -1))
                span_end = int(span.get("alignment_end", -1))
                if span_start < group_start or span_end > group_end:
                    continue
                span_text = _slice_master_text(
                    master,
                    units,
                    span_start,
                    span_end,
                )
                if len(span_text) <= hard_limit:
                    continue
                children = [
                    child
                    for child in spans
                    if child is not span
                    and span_start <= int(child.get("alignment_start", -1))
                    and int(child.get("alignment_end", -1)) <= span_end
                    and (
                        int(child.get("alignment_start", -1)) > span_start
                        or int(child.get("alignment_end", -1)) < span_end
                    )
                ]
                if len(children) < 2:
                    issues.append(
                        {
                            "code": "undecomposed_oversized_strong_span",
                            "group_id": group.get("group_id"),
                            "span_id": span.get("span_id"),
                            "character_count": len(span_text),
                            "hard_limit": hard_limit,
                            "child_span_count": len(children),
                        }
                    )
    return issues


def _groups_intersecting(
    analysis: dict[str, Any], start: int, end: int
) -> list[dict[str, Any]]:
    return [
        copy.deepcopy(group)
        for group in analysis.get("groups", [])
        if int(group["alignment_start"]) <= end
        and start <= int(group["alignment_end"])
    ]


def _groups_tiling(
    analysis: dict[str, Any], start: int, end: int
) -> list[dict[str, Any]]:
    if start > end:
        return []
    groups = [
        copy.deepcopy(group)
        for group in analysis.get("groups", [])
        if start <= int(group["alignment_start"])
        and int(group["alignment_end"]) <= end
    ]
    expected = start
    for group in groups:
        if int(group["alignment_start"]) != expected:
            raise Stage1PipelineError(
                f"A1 稳定区不能连续覆盖：期望 {expected}，得到 {group['alignment_start']}"
            )
        expected = int(group["alignment_end"]) + 1
    if expected != end + 1:
        raise Stage1PipelineError(
            f"A1 稳定区未覆盖至 {end}，实际至 {expected - 1}"
        )
    return groups


def _expand_seam_window(
    seed: dict[str, Any],
    left: dict[str, Any],
    right: dict[str, Any],
) -> tuple[int, int]:
    """Bridge from a left group start to a right group end.

    Those two endpoints guarantee that the untouched left and right stable
    regions remain complete A1 groups. The seam itself is re-analysed, so it
    does not need to tile either non-binding proposal.
    """

    boundary = int(seed["technical_boundary_after"])
    left_owner = _groups_intersecting(left, boundary, boundary)
    right_owner = _groups_intersecting(right, boundary + 1, boundary + 1)
    if not left_owner or not right_owner:
        raise Stage1PipelineError("技术接缝不在左右 A1 核心覆盖内")
    start = int(left_owner[-1]["alignment_start"])
    end = int(right_owner[0]["alignment_end"])
    if start > end:
        raise Stage1PipelineError("接缝桥接窗口倒置")
    return start, end


def _proposal_signature(groups: list[dict[str, Any]]) -> dict[str, Any]:
    boundaries = [int(group["alignment_end"]) for group in groups[:-1]]
    protected = sorted(
        (
            int(span["alignment_start"]),
            int(span["alignment_end"]),
            str(span.get("protection_level", "")),
            str(span.get("category", "")),
        )
        for group in groups
        for span in group.get("protected_spans", [])
        if span.get("protection_level") in {"hard", "strong_soft"}
    )
    edits = sorted(
        (
            key,
            int(edit["alignment_start"]),
            int(edit["alignment_end"]),
            str(edit.get("proposal", "")),
        )
        for group in groups
        for key in ("deletion_candidates", "correction_candidates")
        for edit in group.get(key, [])
    )
    return {"boundaries": boundaries, "protected": protected, "edits": edits}


def _allowed_seam_boundaries(
    proposals: list[list[dict[str, Any]]],
    window_start: int,
    window_end: int,
) -> set[int]:
    allowed = {
        int(group["alignment_end"])
        for groups in proposals
        for group in groups[:-1]
    }
    allowed.update(
        int(boundary["after_alignment"])
        for groups in proposals
        for group in groups
        for boundary in group.get("preferred_boundaries", [])
        if isinstance(boundary, dict)
        and isinstance(boundary.get("after_alignment"), int)
    )
    return {
        boundary
        for boundary in allowed
        if window_start <= boundary < window_end
    }


def _renumber_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = copy.deepcopy(groups)
    for position, group in enumerate(result, start=1):
        group["group_id"] = f"g{position:04d}"
        for span_position, span in enumerate(group.get("protected_spans", []), start=1):
            span["span_id"] = f"g{position:04d}_p{span_position:03d}"
    return result


def _chunk_groups(groups: list[dict[str, Any]], maximum: int) -> list[list[dict[str, Any]]]:
    return [groups[position : position + maximum] for position in range(0, len(groups), maximum)]


def _a2_issues(
    analysis: dict[str, Any],
    candidates: dict[str, Any],
    *,
    master: str | None = None,
    units: list[Any] | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    hard_limit = int(source_punctuation_kwargs().get("english_hard_limit", 55))
    expected = [str(group["group_id"]) for group in analysis.get("groups", [])]
    actual = [
        str(group.get("group_id", "")) for group in candidates.get("groups", [])
    ]
    if actual != expected:
        issues.append(
            {
                "code": "candidate_group_coverage",
                "expected": expected,
                "actual": actual,
            }
        )
        return issues
    analysis_by_id = {
        str(group["group_id"]): group for group in analysis.get("groups", [])
    }
    for candidate_group in candidates.get("groups", []):
        group_id = str(candidate_group["group_id"])
        source = analysis_by_id[group_id]
        start = int(source["alignment_start"])
        end = int(source["alignment_end"])
        if not candidate_group.get("candidates"):
            issues.append(
                {
                    "code": "candidate_group_empty",
                    "group_id": group_id,
                }
            )
            continue
        hard = [
            span
            for span in source.get("protected_spans", [])
            if span.get("protection_level") == "hard"
        ]
        strong = [
            span
            for span in source.get("protected_spans", [])
            if span.get("protection_level") == "strong_soft"
        ]
        for candidate in candidate_group.get("candidates", []):
            cuts = [int(value) for value in candidate.get("cut_after_alignment", [])]
            candidate_id = candidate.get("candidate_id")
            if cuts != sorted(set(cuts)) or any(
                cut < start or cut >= end for cut in cuts
            ):
                issues.append(
                    {
                        "code": "invalid_candidate_cuts",
                        "group_id": group_id,
                        "candidate_id": candidate_id,
                        "cuts": cuts,
                        "range": [start, end],
                    }
                )
            if len(candidate.get("cues", [])) != len(cuts) + 1:
                issues.append(
                    {
                        "code": "candidate_cue_count",
                        "group_id": group_id,
                        "candidate_id": candidate_id,
                    }
                )
            raw_cues = [
                project_annotations(str(cue))[0]
                for cue in candidate.get("cues", [])
            ]
            expected_cues = raw_cues
            if master is not None and units is not None and len(raw_cues) == len(cuts) + 1:
                expected_cues = []
                segment_start = start
                for segment_end in cuts + [end]:
                    expected_cues.append(
                        _slice_master_text(
                            master,
                            units,
                            segment_start,
                            segment_end,
                        )
                    )
                    segment_start = segment_end + 1
                for cue_position, (raw_cue, expected_cue) in enumerate(
                    zip(raw_cues, expected_cues),
                    start=1,
                ):
                    if comparison_normalize(raw_cue) != comparison_normalize(
                        expected_cue
                    ):
                        issues.append(
                            {
                                "code": "candidate_cue_cut_mismatch",
                                "group_id": group_id,
                                "candidate_id": candidate_id,
                                "cue_position": cue_position,
                                "expected_text": expected_cue,
                            }
                        )
            for cue_position, expected_cue in enumerate(expected_cues, start=1):
                length = len(expected_cue)
                if length > hard_limit:
                    issues.append(
                        {
                            "code": "candidate_line_too_long",
                            "group_id": group_id,
                            "candidate_id": candidate_id,
                            "cue_position": cue_position,
                            "character_count_including_spaces_and_punctuation": length,
                            "hard_limit": hard_limit,
                        }
                    )
                words = re.findall(
                    r"[A-Za-z]+(?:['’-][A-Za-z]+)*",
                    expected_cue,
                )
                if (
                    len(words) == 1
                    and not re.search(r"[.!?…][\"'”’)]?$", expected_cue.strip())
                    and len(expected_cues) > 1
                ):
                    mergeable_left = (
                        cue_position > 1
                        and len(
                            expected_cues[cue_position - 2].rstrip()
                            + " "
                            + expected_cue.lstrip()
                        )
                        <= hard_limit
                    )
                    mergeable_right = (
                        cue_position < len(expected_cues)
                        and len(
                            expected_cue.rstrip()
                            + " "
                            + expected_cues[cue_position].lstrip()
                        )
                        <= hard_limit
                    )
                    if mergeable_left or mergeable_right:
                        issues.append(
                            {
                                "code": "mergeable_single_word_residual",
                                "group_id": group_id,
                                "candidate_id": candidate_id,
                                "cue_position": cue_position,
                                "text": expected_cue,
                                "mergeable_left": mergeable_left,
                                "mergeable_right": mergeable_right,
                            }
                        )
            if comparison_normalize(" ".join(raw_cues)) != comparison_normalize(
                str(source.get("source_text", ""))
            ):
                issues.append(
                    {
                        "code": "candidate_source_coverage",
                        "group_id": group_id,
                        "candidate_id": candidate_id,
                    }
                )
            for cut in cuts:
                if any(
                    int(span["alignment_start"]) <= cut
                    < int(span["alignment_end"])
                    for span in hard
                ):
                    issues.append(
                        {
                            "code": "hard_span_cut",
                            "group_id": group_id,
                            "candidate_id": candidate_id,
                            "cut": cut,
                        }
                    )
                for span in strong:
                    if not (
                        int(span["alignment_start"])
                        <= cut
                        < int(span["alignment_end"])
                    ):
                        continue
                    declared = any(
                        str(item.get("span_id", "")) == str(span.get("span_id", ""))
                        and int(item.get("after_alignment", -1)) == cut
                        for item in candidate.get("strong_soft_splits", [])
                        if isinstance(item, dict)
                    )
                    if not declared:
                        issues.append(
                            {
                                "code": "undeclared_strong_soft_split",
                                "group_id": group_id,
                                "candidate_id": candidate_id,
                                "span_id": span.get("span_id"),
                                "cut": cut,
                            }
                        )
            if candidate.get("hard_violations"):
                issues.append(
                    {
                        "code": "declared_hard_violation",
                        "group_id": group_id,
                        "candidate_id": candidate_id,
                    }
                )
    return issues


def _normalize_redundant_terminal_cuts(
    analysis: dict[str, Any],
    candidates: dict[str, Any],
    *,
    master: str = "",
    units: list[Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Enforce non-negotiable cut syntax without searching for new cuts.

    A group terminus is not an internal cut, and a cut inside a hard span was
    never eligible. Removing either is deterministic schema enforcement; cue
    text is rebuilt exactly from the surviving alignment boundaries.
    """

    normalized = copy.deepcopy(candidates)
    ends = {
        str(group["group_id"]): int(group["alignment_end"])
        for group in analysis.get("groups", [])
    }
    actions: list[dict[str, Any]] = []
    for candidate_group in normalized.get("groups", []):
        group_id = str(candidate_group.get("group_id", ""))
        end = ends.get(group_id)
        if end is None:
            continue
        source_group = next(
            group
            for group in analysis.get("groups", [])
            if str(group["group_id"]) == group_id
        )
        start = int(source_group["alignment_start"])
        forbidden = {
            cut
            for span in source_group.get("protected_spans", [])
            if span.get("protection_level") == "hard"
            for cut in range(
                int(span["alignment_start"]),
                int(span["alignment_end"]),
            )
        }
        for candidate in candidate_group.get("candidates", []):
            cuts = [int(value) for value in candidate.get("cut_after_alignment", [])]
            removed_terminal = end if cuts and cuts[-1] == end else None
            legal_cuts = [
                cut for cut in cuts if cut != end and cut not in forbidden
            ]
            removed_hard = sorted(set(cuts) & forbidden)
            if legal_cuts != cuts:
                candidate["cut_after_alignment"] = legal_cuts
                if removed_hard:
                    if units is None:
                        raise Stage1PipelineError(
                            "规范化 hard span 切点需要 alignment units"
                        )
                    boundaries = [start - 1, *legal_cuts, end]
                    candidate["cues"] = [
                        _slice_master_text(
                            master,
                            units,
                            boundaries[position] + 1,
                            boundaries[position + 1],
                        )
                        for position in range(len(boundaries) - 1)
                    ]
                    candidate["hard_violations"] = []
                actions.append(
                    {
                        "group_id": group_id,
                        "candidate_id": candidate.get("candidate_id"),
                        "removed_terminal_cut": removed_terminal,
                        "removed_hard_span_cuts": removed_hard,
                    }
                )
    return normalized, actions


def _downgrade_impossible_hard_spans(
    analysis: dict[str, Any],
    *,
    master: str,
    units: list[Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Downgrade hard spans that cannot fit under the configured hard limit."""

    normalized = copy.deepcopy(analysis)
    hard_limit = int(source_punctuation_kwargs().get("english_hard_limit", 55))
    actions: list[dict[str, Any]] = []
    for group in normalized.get("groups", []):
        for span in group.get("protected_spans", []):
            if span.get("protection_level") != "hard":
                continue
            span_text = _slice_master_text(
                master,
                units,
                int(span["alignment_start"]),
                int(span["alignment_end"]),
            )
            character_count = len(span_text)
            if character_count <= hard_limit:
                continue
            span["protection_level"] = "strong_soft"
            span["reason"] = (
                str(span.get("reason", ""))
                + f"；长度 {character_count} 超过硬上限 {hard_limit}，"
                "自动降为 strong_soft"
            ).strip("；")
            actions.append(
                {
                    "group_id": group.get("group_id"),
                    "span_id": span.get("span_id"),
                    "character_count": character_count,
                    "hard_limit": hard_limit,
                    "action": "hard_to_strong_soft",
                }
            )
    return normalized, actions


def _relax_undecomposed_strong_spans(
    analysis: dict[str, Any],
    issues: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Bounded A1 delivery fallback after two failed recursive analyses."""

    normalized = copy.deepcopy(analysis)
    targets = {
        (str(issue.get("group_id", "")), str(issue.get("span_id", "")))
        for issue in issues
        if issue.get("code") == "undecomposed_oversized_strong_span"
    }
    actions: list[dict[str, Any]] = []
    for group in normalized.get("groups", []):
        group_id = str(group.get("group_id", ""))
        for span in group.get("protected_spans", []):
            key = (group_id, str(span.get("span_id", "")))
            if key not in targets:
                continue
            span["protection_level"] = "outer_soft"
            span["reason"] = (
                str(span.get("reason", ""))
                + "；两次 A1 均未递归分解，降为 outer_soft 以避免错误锁死"
            ).strip("；")
            actions.append(
                {
                    "group_id": group_id,
                    "span_id": span.get("span_id"),
                    "action": "strong_soft_to_outer_soft",
                }
            )
    return normalized, actions


def _filter_invalid_candidates(
    analysis: dict[str, Any],
    candidates: dict[str, Any],
    *,
    master: str,
    units: list[Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Drop malformed alternatives without changing any surviving candidate."""

    filtered = copy.deepcopy(candidates)
    analysis_by_id = {
        str(group["group_id"]): group for group in analysis.get("groups", [])
    }
    dropped: list[dict[str, Any]] = []
    for candidate_group in filtered.get("groups", []):
        group_id = str(candidate_group.get("group_id", ""))
        source_group = analysis_by_id.get(group_id)
        if source_group is None:
            continue
        survivors: list[dict[str, Any]] = []
        for candidate in candidate_group.get("candidates", []):
            trial = {
                "groups": [
                    {
                        "group_id": group_id,
                        "candidates": [candidate],
                    }
                ]
            }
            trial_issues = _a2_issues(
                {"groups": [source_group]},
                trial,
                master=master,
                units=units,
            )
            if trial_issues:
                dropped.append(
                    {
                        "group_id": group_id,
                        "candidate_id": candidate.get("candidate_id"),
                        "issues": trial_issues,
                    }
                )
            else:
                survivors.append(candidate)
        candidate_group["candidates"] = survivors
    return filtered, dropped


def _batch_hard_issues(result: Any) -> list[dict[str, Any]]:
    """Ignore only global numbering while validating an isolated later batch."""

    return [
        issue
        for issue in result.issues
        if str(issue.get("code", "")) != "group_id_order"
    ]


def _deterministic_candidate_fallback(
    *,
    analysis: dict[str, Any],
    candidates: dict[str, Any],
    blinded: dict[str, Any],
    master: str,
    units: list[Any],
    start: int,
    end: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Choose a hard-valid supplied candidate without inventing new cuts.

    This is a bounded delivery fallback for two failed A3 calls. It does not
    optimize, move or synthesize boundaries.
    """

    original_by_group = {
        str(group["group_id"]): group for group in candidates.get("groups", [])
    }
    blind_by_group = {
        str(group["group_id"]): group for group in blinded.get("groups", [])
    }
    strategy_order = {
        "phrase_integrity": 0,
        "boundary_coverage": 1,
        "prosody_timing": 2,
    }
    chosen_groups: list[dict[str, Any]] = []
    for analysis_group in analysis.get("groups", []):
        group_id = str(analysis_group["group_id"])
        originals = list(original_by_group[group_id].get("candidates", []))
        originals.sort(
            key=lambda item: strategy_order.get(str(item.get("strategy", "")), 9)
        )
        blind_rows = list(blind_by_group[group_id].get("candidates", []))
        selected: dict[str, Any] | None = None
        for original in originals:
            fingerprint = tuple(
                int(value) for value in original.get("cut_after_alignment", [])
            )
            trial = next(
                (
                    row
                    for row in blind_rows
                    if tuple(
                        int(value)
                        for value in row.get("cut_after_alignment", [])
                    )
                    == fingerprint
                ),
                None,
            )
            if trial is None:
                continue
            trial_choice = {
                "group_id": group_id,
                "selected_candidate_id": trial["candidate_id"],
                "cues": list(trial["cues"]),
                "scores": {},
                "weakest_cue_check": "deterministic hard validation",
                "rejected": [],
            }
            trial_analysis = {
                **analysis,
                "groups": [analysis_group],
            }
            trial_decision = {
                "groups": [trial_choice],
            }
            trial_plan = _direct_plan_from_blind_decision(
                trial_analysis,
                {"groups": [blind_by_group[group_id]]},
                trial_decision,
            )
            group_start = int(analysis_group["alignment_start"])
            group_end = int(analysis_group["alignment_end"])
            group_units = [
                unit
                for unit in units
                if group_start <= int(unit.index) <= group_end
            ]
            trial_result = evaluate_direct_plan(
                _slice_master_text(master, units, group_start, group_end),
                group_units,
                trial_plan,
                **source_punctuation_kwargs(),
            )
            if not _batch_hard_issues(trial_result):
                selected = trial
                break
        if selected is None:
            raise Stage1PipelineError(
                f"确定性 A3 兜底无法映射 {group_id} 的候选"
            )
        chosen_groups.append(
            {
                "group_id": group_id,
                "selected_candidate_id": selected["candidate_id"],
                "cues": list(selected["cues"]),
                "scores": {
                    "protection_integrity": 0,
                    "language_chunks": 0,
                    "meaning_rhetoric": 0,
                    "alignment_timing": 0,
                    "company_style": 0,
                    "visual_balance": 0,
                },
                "weakest_cue_check": "A3 两次失败后的有限交付兜底；未新建或移动切点",
                "rejected": [],
            }
        )
    decision = {
        "schema_version": "substar.stage1.decision.v2",
        "groups": chosen_groups,
        "final_draft": "\n".join(
            cue for group in chosen_groups for cue in group["cues"]
        ),
    }
    plan = _direct_plan_from_blind_decision(analysis, blinded, decision)
    local_units = [unit for unit in units if start <= int(unit.index) <= end]
    result = evaluate_direct_plan(
        _slice_master_text(master, units, start, end),
        local_units,
        plan,
        **source_punctuation_kwargs(),
    )
    fallback_issues = _batch_hard_issues(result)
    if fallback_issues:
        raise Stage1PipelineError(
            "确定性 A3 兜底仍不合法："
            + json.dumps(fallback_issues[:8], ensure_ascii=False)
        )
    return decision, plan


def _run_boundary_gate(
    *,
    analysis: dict[str, Any],
    blinded: dict[str, Any],
    decision: dict[str, Any],
    master: str,
    units: list[Any],
    start: int,
    end: int,
    material: str,
    args: argparse.Namespace,
    api_key: str,
    batch_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Adversarially review A3, but only by selecting an existing candidate."""

    try:
        gated, telemetry = _call_model_with_one_content_retry(
            base_url=args.base_url,
            api_key=api_key,
            model=args.model,
            system_prompt=stage_system("gate", material),
            user_payload="\n\n".join(
                [
                    "# INPUT_MATERIAL\n" + material,
                    "# GLOBAL_A1_ANALYSIS_BATCH\n"
                    + json.dumps(analysis, ensure_ascii=False),
                    "# BLINDED_CANDIDATES\n"
                    + json.dumps(blinded, ensure_ascii=False),
                    "# CURRENT_A3_DECISION\n"
                    + json.dumps(decision, ensure_ascii=False),
                ]
            ),
            timeout=min(args.timeout, 300),
            max_tokens=args.max_tokens,
            json_mode=True,
            thinking_mode="enabled",
            reasoning_effort=args.reasoning_effort,
            request_attempts=args.http_attempts,
        )
        write_json(batch_dir / "stage1_boundary_gate_raw.json", gated)
        assert_schema_version("gate", gated)
        gated_plan = _direct_plan_from_blind_decision(
            analysis,
            blinded,
            gated,
        )
        local_units = [
            unit for unit in units if start <= int(unit.index) <= end
        ]
        result = evaluate_direct_plan(
            _slice_master_text(master, units, start, end),
            local_units,
            gated_plan,
            **source_punctuation_kwargs(),
        )
        issues = _batch_hard_issues(result)
        if issues:
            raise Stage1PipelineError(
                json.dumps(issues[:8], ensure_ascii=False)
            )
        before = {
            str(group["group_id"]): str(group["selected_candidate_id"])
            for group in decision.get("groups", [])
        }
        after = {
            str(group["group_id"]): str(group["selected_candidate_id"])
            for group in gated.get("groups", [])
        }
        telemetry["changed_groups"] = [
            group_id
            for group_id in before
            if before.get(group_id) != after.get(group_id)
        ]
        telemetry["retained_a3"] = not telemetry["changed_groups"]
        write_json(batch_dir / "stage1_boundary_gate.json", gated)
        write_json(batch_dir / "api_call_03A4.json", telemetry)
        return gated, gated_plan, telemetry
    except (OSError, ValueError, Stage1PipelineError) as exc:
        plan = _direct_plan_from_blind_decision(
            analysis,
            blinded,
            decision,
        )
        telemetry = {
            "bounded_failure": True,
            "error": str(exc),
            "retained_a3": True,
            "changed_groups": [],
        }
        write_json(batch_dir / "api_call_03A4.json", telemetry)
        return decision, plan, telemetry


def _deterministic_a2_delivery_candidate(
    group: dict[str, Any],
    *,
    master: str,
    units: list[Any],
) -> dict[str, Any]:
    """Create one hard-valid A2 candidate after all bounded API attempts fail."""

    start = int(group["alignment_start"])
    end = int(group["alignment_end"])
    hard_limit = int(source_punctuation_kwargs().get("english_hard_limit", 55))
    hard_forbidden = {
        cut
        for span in group.get("protected_spans", [])
        if span.get("protection_level") == "hard"
        for cut in range(
            int(span["alignment_start"]),
            int(span["alignment_end"]),
        )
    }
    strong_forbidden = {
        cut
        for span in group.get("protected_spans", [])
        if span.get("protection_level") == "strong_soft"
        for cut in range(
            int(span["alignment_start"]),
            int(span["alignment_end"]),
        )
    }
    preferred = {
        int(row["after_alignment"]): str(row.get("priority", "normal"))
        for row in group.get("preferred_boundaries", [])
        if isinstance(row, dict)
        and isinstance(row.get("after_alignment"), int)
    }
    priority_bonus = {"high": 30, "medium": 20, "normal": 10, "low": 5}
    cuts: list[int] = []
    cue_start = start
    while cue_start <= end:
        if len(_slice_master_text(master, units, cue_start, end)) <= hard_limit:
            break
        choices: list[tuple[tuple[int, int, int], int]] = []
        for cut in range(cue_start, end):
            if cut in hard_forbidden:
                continue
            length = len(_slice_master_text(master, units, cue_start, cut))
            if length > hard_limit:
                break
            score = (
                -100 if cut in strong_forbidden else 0,
                priority_bonus.get(preferred.get(cut, ""), 0),
                length,
            )
            choices.append((score, cut))
        if not choices:
            raise Stage1PipelineError(
                f"{group['group_id']} 不存在满足字符硬上限的确定性切点"
            )
        cut = max(choices)[1]
        cuts.append(cut)
        cue_start = cut + 1
    boundaries = [start - 1, *cuts, end]
    return {
        "candidate_id": f"{group['group_id']}_deterministic_delivery",
        "strategy": "deterministic_delivery_fallback",
        "cues": [
            _slice_master_text(
                master,
                units,
                boundaries[position] + 1,
                boundaries[position + 1],
            )
            for position in range(len(boundaries) - 1)
        ],
        "cut_after_alignment": cuts,
        "strong_soft_splits": [
            {
                "span_id": str(span.get("span_id", "")),
                "after_alignment": cut,
                "reason": "两轮整批与一次局部 API 均失败后，为满足字符硬上限的确定性交付兜底",
            }
            for cut in cuts
            for span in group.get("protected_spans", [])
            if span.get("protection_level") == "strong_soft"
            and int(span["alignment_start"]) <= cut < int(span["alignment_end"])
        ],
        "hard_violations": [],
    }


def _call_a1(
    chunk: AdaptiveAnalysisChunk,
    args: argparse.Namespace,
    api_key: str,
    output_dir: Path,
    master: str,
    units: list[Any],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    chunk_dir = output_dir / "a1_chunks" / chunk.chunk_id
    chunk_dir.mkdir(parents=True, exist_ok=True)
    result_path = chunk_dir / "stage1_analysis.json"
    error = ""
    analysis: dict[str, Any] = {}
    telemetry: dict[str, Any] = {}
    bounded_candidate: tuple[
        dict[str, Any], dict[str, Any], list[dict[str, Any]]
    ] | None = None

    def validate_scope(
        candidate: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        assert_schema_version("analysis", candidate)
        candidate, protection_downgrades = _downgrade_impossible_hard_spans(
            candidate,
            master=master,
            units=units,
        )
        candidate = augment_structural_boundaries(candidate)
        scope_issues = _analysis_issues(
            candidate,
            start=chunk.analysis_start,
            end=chunk.analysis_end,
            master=master,
            units=units,
        )
        scope_telemetry: dict[str, Any] = {
            "protection_downgrades": protection_downgrades
        }
        if not scope_issues:
            return candidate, scope_telemetry
        groups = list(candidate.get("groups", []))
        actual_start = int(groups[0]["alignment_start"]) if groups else -1
        actual_end = int(groups[-1]["alignment_end"]) if groups else -1
        degraded_issues = (
            _analysis_issues(
                candidate,
                start=actual_start,
                end=actual_end,
                master=master,
                units=units,
            )
            if groups
            else scope_issues
        )
        core_covered = (
            actual_start <= chunk.core_start and actual_end >= chunk.core_end
        )
        if not core_covered or degraded_issues:
            raise Stage1PipelineError(
                json.dumps(scope_issues[:8], ensure_ascii=False)
            )
        scope_telemetry = {
            "degraded_overlap_scope": True,
            "requested_analysis_range": [
                chunk.analysis_start,
                chunk.analysis_end,
            ],
            "actual_analysis_range": [actual_start, actual_end],
        }
        return candidate, scope_telemetry

    def remember_bounded_candidate(
        candidate: dict[str, Any],
        candidate_telemetry: dict[str, Any],
    ) -> None:
        nonlocal bounded_candidate
        try:
            assert_schema_version("analysis", candidate)
            prepared, protection_downgrades = _downgrade_impossible_hard_spans(
                copy.deepcopy(candidate),
                master=master,
                units=units,
            )
            prepared = augment_structural_boundaries(prepared)
            issues = _analysis_issues(
                prepared,
                start=chunk.analysis_start,
                end=chunk.analysis_end,
                master=master,
                units=units,
            )
        except (OSError, ValueError, Stage1PipelineError):
            return
        if not issues or any(
            issue.get("code")
            not in {
                "undecomposed_oversized_strong_span",
                "suspected_multi_center_group",
            }
            for issue in issues
        ):
            return
        score = (
            sum(
                issue.get("code") == "suspected_multi_center_group"
                for issue in issues
            ),
            len(issues),
        )
        if bounded_candidate is not None:
            current_issues = bounded_candidate[2]
            current_score = (
                sum(
                    issue.get("code") == "suspected_multi_center_group"
                    for issue in current_issues
                ),
                len(current_issues),
            )
            if score >= current_score:
                return
        bounded_candidate = (
            prepared,
            {
                **candidate_telemetry,
                "protection_downgrades": protection_downgrades,
            },
            issues,
        )

    if args.resume:
        resume_paths = [result_path]
        resume_paths.extend(
            sorted(
                chunk_dir.glob("stage1_analysis_raw_*.json"),
                reverse=True,
            )
        )
        resume_errors: list[str] = []
        for resume_path in resume_paths:
            if not resume_path.exists():
                continue
            resumed: dict[str, Any] = {}
            try:
                resumed = json.loads(resume_path.read_text(encoding="utf-8"))
                analysis, scope_telemetry = validate_scope(resumed)
                telemetry = {
                    "resumed": True,
                    "resumed_from": resume_path.name,
                    **scope_telemetry,
                }
                write_json(result_path, analysis)
                write_json(chunk_dir / "api_call_03A1.json", telemetry)
                return chunk.chunk_id, analysis, telemetry
            except (OSError, ValueError, Stage1PipelineError) as exc:
                if resumed:
                    remember_bounded_candidate(
                        resumed,
                        {
                            "resumed": True,
                            "resumed_from": resume_path.name,
                        },
                    )
                resume_errors.append(f"{resume_path.name}: {exc}")
        if resume_errors:
            error = "；".join(resume_errors[-2:])
        if bounded_candidate is not None:
            analysis, telemetry, final_issues = bounded_candidate
            analysis, relaxations = _relax_undecomposed_strong_spans(
                analysis,
                final_issues,
            )
            telemetry["bounded_hierarchy_relaxation"] = relaxations
            telemetry["semantic_attempt"] = 2
            write_json(result_path, analysis)
            write_json(chunk_dir / "api_call_03A1.json", telemetry)
            return chunk.chunk_id, analysis, telemetry

    for attempt in range(1, 3):
        try:
            analysis, telemetry = _call_model_with_one_content_retry(
                base_url=args.base_url,
                api_key=api_key,
                model=args.model,
                system_prompt=stage_system("analysis", chunk.material),
                user_payload="# INPUT_MATERIAL\n"
                + "# REQUIRED_ALIGNMENT_COVERAGE\n"
                + f"必须输出连续且完整的 alignment {chunk.analysis_start}-{chunk.analysis_end}；"
                + f"第一组必须从 {chunk.analysis_start} 开始，最后一组必须到 {chunk.analysis_end} 结束。"
                + "core 仅表示后续所有权，不是本次输出边界；重叠观察区同样必须输出。\n\n"
                + chunk.material
                + (
                    "\n\n# PROGRAM_REJECTION\n" + error
                    if error
                    else ""
                ),
                timeout=min(args.timeout, 300),
                max_tokens=(
                    args.max_tokens
                    if attempt == 1
                    else min(64000, args.max_tokens * 2)
                ),
                json_mode=True,
                thinking_mode="enabled",
                reasoning_effort=args.reasoning_effort,
                request_attempts=args.http_attempts,
            )
            telemetry["semantic_attempt"] = attempt
            write_json(
                chunk_dir / f"stage1_analysis_raw_{attempt}.json",
                analysis,
            )
            try:
                analysis, scope_telemetry = validate_scope(analysis)
            except Stage1PipelineError:
                remember_bounded_candidate(analysis, telemetry)
                raise
            telemetry.update(scope_telemetry)
            break
        except (OSError, ValueError, Stage1PipelineError) as exc:
            error = str(exc)
    else:
        if bounded_candidate is not None:
            analysis, telemetry, final_issues = bounded_candidate
        else:
            final_issues = _analysis_issues(
                analysis,
                start=chunk.analysis_start,
                end=chunk.analysis_end,
                master=master,
                units=units,
            )
        non_decomposition = [
            issue
            for issue in final_issues
            if issue.get("code")
            not in {
                "undecomposed_oversized_strong_span",
                "suspected_multi_center_group",
            }
        ]
        if analysis and not non_decomposition:
            analysis, relaxations = _relax_undecomposed_strong_spans(
                analysis,
                final_issues,
            )
            telemetry["bounded_hierarchy_relaxation"] = relaxations
            telemetry["semantic_attempt"] = 2
        else:
            raise Stage1PipelineError(
                f"{chunk.chunk_id} A1 两次均无效：{error}"
            )
    write_json(result_path, analysis)
    write_json(chunk_dir / "api_call_03A1.json", telemetry)
    return chunk.chunk_id, analysis, telemetry


def _resolve_seam(
    *,
    seed: dict[str, Any],
    left_analysis: dict[str, Any],
    right_analysis: dict[str, Any],
    master: str,
    units: list[Any],
    args: argparse.Namespace,
    api_key: str,
    output_dir: Path,
) -> dict[str, Any]:
    start, end = _expand_seam_window(seed, left_analysis, right_analysis)
    left_groups = _groups_intersecting(left_analysis, start, end)
    right_groups = _groups_intersecting(right_analysis, start, end)
    signatures = [
        _proposal_signature(left_groups),
        _proposal_signature(right_groups),
    ]
    seam_id = str(seed["seam_id"])
    record: dict[str, Any] = {
        **seed,
        "window_start": start,
        "window_end": end,
        "left_proposal": left_groups,
        "right_proposal": right_groups,
        "agreement": signatures[0] == signatures[1],
    }
    left_exact = (
        int(left_groups[0]["alignment_start"]) == start
        and int(left_groups[-1]["alignment_end"]) == end
    )
    right_exact = (
        int(right_groups[0]["alignment_start"]) == start
        and int(right_groups[-1]["alignment_end"]) == end
    )
    if record["agreement"] and left_exact and right_exact:
        record["resolution"] = "deterministic_consensus"
        record["groups"] = left_groups
        return record

    allowed = _allowed_seam_boundaries(
        [left_groups, right_groups], start, end
    )
    seam_material = _slice_material(master, units, start, end)
    payload = "\n\n".join(
        [
            "# A1_SEAM_TASK",
            f"只覆盖 alignment {start}-{end}。意义组外边界只能从以下候选选择："
            + json.dumps(sorted(allowed), ensure_ascii=False)
            + "。最后一个组自然结束于 window_end，不把 window_end 写成候选。",
            "请综合两个非绑定提案重新输出一个完整的 substar.stage1.analysis.v2。"
            "不得输出窗口外内容。分层保护仍须完整。",
            "# INPUT_MATERIAL\n" + seam_material,
            "# LEFT_PROPOSAL\n" + json.dumps(left_groups, ensure_ascii=False),
            "# RIGHT_PROPOSAL\n" + json.dumps(right_groups, ensure_ascii=False),
        ]
    )
    seam_dir = output_dir / "a1_seams" / seam_id
    seam_dir.mkdir(parents=True, exist_ok=True)
    error = ""

    if args.resume:
        resumed_path = seam_dir / "stage1_analysis.json"
        if resumed_path.exists():
            try:
                resumed = json.loads(resumed_path.read_text(encoding="utf-8"))
                assert_schema_version("analysis", resumed)
                resumed = augment_structural_boundaries(resumed)
                resumed_issues = _analysis_issues(
                    resumed,
                    start=start,
                    end=end,
                    master=master,
                    units=units,
                )
                resumed_boundaries = {
                    int(group["alignment_end"])
                    for group in resumed.get("groups", [])[:-1]
                }
                invented = sorted(resumed_boundaries - allowed)
                if invented:
                    resumed_issues.append(
                        {
                            "code": "invented_seam_boundary",
                            "boundaries": invented,
                        }
                    )
                if resumed_issues:
                    raise Stage1PipelineError(
                        json.dumps(resumed_issues[:8], ensure_ascii=False)
                    )
                record["resolution"] = "resumed_api_adjudication"
                record["groups"] = resumed["groups"]
                record["attempts"] = 0
                return record
            except (OSError, ValueError, Stage1PipelineError) as exc:
                error = f"已有接缝结果不适用于当前窗口：{exc}"

    for attempt in range(1, 3):
        try:
            analysis, telemetry = _call_model_with_one_content_retry(
                base_url=args.base_url,
                api_key=api_key,
                model=args.model,
                system_prompt=stage_system("analysis", seam_material),
                user_payload=payload
                + (
                    "\n\n# PROGRAM_REJECTION\n" + error
                    if error
                    else ""
                ),
                timeout=min(args.timeout, 300),
                max_tokens=(
                    args.max_tokens
                    if attempt == 1
                    else min(64000, args.max_tokens * 2)
                ),
                json_mode=True,
                thinking_mode="enabled",
                reasoning_effort=args.reasoning_effort,
                request_attempts=args.http_attempts,
            )
            assert_schema_version("analysis", analysis)
            analysis = augment_structural_boundaries(analysis)
            issues = _analysis_issues(
                analysis,
                start=start,
                end=end,
                master=master,
                units=units,
            )
            selected_boundaries = {
                int(group["alignment_end"])
                for group in analysis.get("groups", [])[:-1]
            }
            invented = sorted(selected_boundaries - allowed)
            if invented:
                issues.append(
                    {"code": "invented_seam_boundary", "boundaries": invented}
                )
            if issues:
                raise Stage1PipelineError(json.dumps(issues[:8], ensure_ascii=False))
            write_json(seam_dir / "stage1_analysis.json", analysis)
            write_json(seam_dir / "api_call_03A1_S.json", telemetry)
            record["resolution"] = "api_adjudication"
            record["groups"] = analysis["groups"]
            record["attempts"] = attempt
            return record
        except (OSError, ValueError, Stage1PipelineError) as exc:
            error = str(exc)
    inherited_spans: list[dict[str, Any]] = []
    seen_spans: set[tuple[int, int, str, str]] = set()
    for group in left_groups + right_groups:
        for span in group.get("protected_spans", []):
            span_start = int(span["alignment_start"])
            span_end = int(span["alignment_end"])
            key = (
                span_start,
                span_end,
                str(span.get("protection_level", "")),
                str(span.get("category", "")),
            )
            if start <= span_start <= span_end <= end and key not in seen_spans:
                seen_spans.add(key)
                inherited_spans.append(copy.deepcopy(span))
    record["resolution"] = "bounded_single_group_fallback"
    record["error"] = error
    record["groups"] = [
        {
            "group_id": f"{seam_id}_fallback",
            "source_text": _slice_master_text(master, units, start, end),
            "alignment_start": start,
            "alignment_end": end,
            "protected_spans": inherited_spans,
            "parallel_structures": [],
            "preferred_boundaries": [
                {
                    "after_alignment": boundary,
                    "priority": "medium",
                    "relation": "inherited_seam_candidate",
                    "reason": "接缝裁决失败后保留的既有候选边界",
                }
                for boundary in sorted(allowed)
            ],
            "forbidden_boundaries": [],
            "deletion_candidates": [],
            "correction_candidates": [],
            "insertion_candidates": [],
            "notes": ["接缝裁决两次失败，合并为单一意义组交由 A2/A3 处理"],
        }
    ]
    return record


def _run_a2_a3_batch(
    *,
    number: int,
    groups: list[dict[str, Any]],
    master: str,
    units: list[Any],
    args: argparse.Namespace,
    api_key: str,
    output_dir: Path,
) -> tuple[int, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    batch_dir = output_dir / "layout_batches" / f"b{number:04d}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    start = int(groups[0]["alignment_start"])
    end = int(groups[-1]["alignment_end"])
    material = _slice_material(master, units, start, end)
    analysis = {
        "schema_version": "substar.stage1.analysis.v2",
        "source_language": "Auto",
        "groups": groups,
        "coverage_check": {"complete": True, "ordered": True, "notes": []},
    }
    resume_candidates_path = batch_dir / "stage1_candidates.json"
    resume_decision_path = batch_dir / "stage1_decision.json"
    resume_plan_path = batch_dir / "stage1_a3_frozen_plan.json"
    if (
        args.resume
        and resume_candidates_path.exists()
        and resume_decision_path.exists()
        and resume_plan_path.exists()
    ):
        candidates = json.loads(resume_candidates_path.read_text(encoding="utf-8"))
        decision = json.loads(resume_decision_path.read_text(encoding="utf-8"))
        plan = json.loads(resume_plan_path.read_text(encoding="utf-8"))
        candidate_issues = _a2_issues(
            analysis, candidates, master=master, units=units
        )
        local_units = [unit for unit in units if start <= int(unit.index) <= end]
        result = evaluate_direct_plan(
            _slice_master_text(master, units, start, end),
            local_units,
            plan,
            **source_punctuation_kwargs(),
        )
        if not candidate_issues and not _batch_hard_issues(result):
            diversity = _candidate_diversity_report(analysis, candidates)
            return number, candidates, decision, plan, {
                "resumed": True,
                "a2": {"resumed": True},
                "a3": {"resumed": True},
                "supplement_used": (
                    batch_dir / "stage1_candidates_supplement.json"
                ).exists(),
                "coverage": diversity,
            }
    a2_error = ""
    candidates: dict[str, Any] = {}
    a2_telemetry: dict[str, Any] = {}
    a2_normalizations: list[dict[str, Any]] = []
    a2_dropped_candidates: list[dict[str, Any]] = []
    a2_local_repairs: list[dict[str, Any]] = []
    a2_resumed = False
    a2_partial_resume = False
    best_partial: tuple[
        dict[str, Any],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ] | None = None
    best_partial_score = -1
    if args.resume and (
        resume_candidates_path.exists()
        or any(batch_dir.glob("stage1_candidates_raw_*.json"))
    ):
        raw_resume_sources = sorted(
            batch_dir.glob("stage1_candidates_raw_*.json"),
            reverse=True,
        )
        resume_sources = raw_resume_sources + (
            [resume_candidates_path] if resume_candidates_path.exists() else []
        )
        for resume_source in resume_sources:
            resumed_candidates = json.loads(
                resume_source.read_text(encoding="utf-8")
            )
            resumed_candidates, resumed_actions = _normalize_redundant_terminal_cuts(
                analysis, resumed_candidates, master=master, units=units
            )
            resumed_candidates, resumed_dropped = _filter_invalid_candidates(
                analysis,
                resumed_candidates,
                master=master,
                units=units,
            )
            resumed_issues = _a2_issues(
                analysis, resumed_candidates, master=master, units=units
            )
            partial_score = sum(
                bool(group.get("candidates"))
                for group in resumed_candidates.get("groups", [])
            )
            if partial_score > best_partial_score:
                best_partial = (
                    resumed_candidates,
                    resumed_actions,
                    resumed_dropped,
                )
                best_partial_score = partial_score
            if not resumed_issues:
                candidates = resumed_candidates
                a2_normalizations = resumed_actions
                a2_dropped_candidates = resumed_dropped
                a2_telemetry = {
                    "resumed": True,
                    "resume_source": str(resume_source),
                }
                a2_resumed = True
                break
            if not a2_error:
                a2_error = json.dumps(resumed_issues[:8], ensure_ascii=False)
        if (
            not a2_resumed
            and best_partial is not None
            and len(raw_resume_sources) >= 2
        ):
            candidates, a2_normalizations, a2_dropped_candidates = best_partial
            a2_telemetry = {
                "resumed_partial_after_two_attempts": True,
            }
            a2_partial_resume = True
    if not a2_resumed or a2_partial_resume:
        for a2_attempt in (
            range(0) if a2_partial_resume else range(1, 3)
        ):
            try:
                candidates, a2_telemetry = _call_model_with_one_content_retry(
                    base_url=args.base_url,
                    api_key=api_key,
                    model=args.model,
                    system_prompt=stage_system("candidates", material),
                    user_payload="# INPUT_MATERIAL\n"
                    + material
                    + "\n\n# GLOBAL_A1_ANALYSIS_BATCH\n"
                    + json.dumps(analysis, ensure_ascii=False)
                    + "\n\n# REQUIRED_GROUP_IDS\n"
                    + json.dumps([group["group_id"] for group in groups], ensure_ascii=False)
                    + "\n必须为每个 group_id 分别返回 candidates，禁止合并、遗漏或重新编号意义组。"
                    + (
                        "\n\n# PROGRAM_REJECTION\n" + a2_error
                        if a2_error
                        else ""
                    ),
                    timeout=min(args.timeout, 300),
                    max_tokens=(
                        args.max_tokens
                        if a2_attempt == 1
                        else min(64000, args.max_tokens * 2)
                    ),
                    json_mode=True,
                    thinking_mode="enabled",
                    reasoning_effort=args.reasoning_effort,
                    request_attempts=args.http_attempts,
                )
                a2_telemetry["semantic_attempt"] = a2_attempt
                write_json(
                    batch_dir / f"stage1_candidates_raw_{a2_attempt}.json",
                    candidates,
                )
                assert_schema_version("candidates", candidates)
                candidates, a2_normalizations = _normalize_redundant_terminal_cuts(
                    analysis, candidates, master=master, units=units
                )
                candidates, a2_dropped_candidates = _filter_invalid_candidates(
                    analysis,
                    candidates,
                    master=master,
                    units=units,
                )
                issues = _a2_issues(
                    analysis, candidates, master=master, units=units
                )
                if issues:
                    raise Stage1PipelineError(json.dumps(issues[:8], ensure_ascii=False))
                break
            except (OSError, ValueError, Stage1PipelineError) as exc:
                a2_error = str(exc)
        else:
            empty_group_ids = [
                str(group.get("group_id", ""))
                for group in candidates.get("groups", [])
                if not group.get("candidates")
            ]
            candidate_groups_by_id = {
                str(group.get("group_id", "")): group
                for group in candidates.get("groups", [])
            }
            analysis_groups_by_id = {
                str(group["group_id"]): group
                for group in analysis.get("groups", [])
            }
            for group_id in empty_group_ids:
                source_group = analysis_groups_by_id[group_id]
                group_material = _slice_material(
                    master,
                    units,
                    int(source_group["alignment_start"]),
                    int(source_group["alignment_end"]),
                )
                single_analysis = {
                    "schema_version": "substar.stage1.analysis.v2",
                    "source_language": analysis.get("source_language", "Auto"),
                    "groups": [source_group],
                    "coverage_check": {
                        "complete": True,
                        "ordered": True,
                        "notes": [],
                    },
                }
                rejection = [
                    item
                    for item in a2_dropped_candidates
                    if item.get("group_id") == group_id
                ]
                if a2_partial_resume:
                    fallback_candidate = _deterministic_a2_delivery_candidate(
                        source_group,
                        master=master,
                        units=units,
                    )
                    candidate_groups_by_id[group_id]["candidates"] = [
                        fallback_candidate
                    ]
                    fallback_trial = {
                        "schema_version": "substar.stage1.candidates.v2",
                        "groups": [
                            {
                                "group_id": group_id,
                                "candidates": [fallback_candidate],
                            }
                        ],
                    }
                    a2_local_repairs.append(
                        {
                            "group_id": group_id,
                            "status": "deterministic_delivery_after_resumed_attempts",
                        }
                    )
                    write_json(
                        batch_dir
                        / f"stage1_candidates_fallback_{group_id}.json",
                        fallback_trial,
                    )
                    continue
                try:
                    repaired, repair_telemetry = _call_model_with_one_content_retry(
                        base_url=args.base_url,
                        api_key=api_key,
                        model=args.model,
                        system_prompt=stage_system("candidates", group_material),
                        user_payload="\n\n".join(
                            [
                                "# INPUT_MATERIAL\n" + group_material,
                                "# GLOBAL_A1_ANALYSIS_BATCH\n"
                                + json.dumps(single_analysis, ensure_ascii=False),
                                "# REQUIRED_GROUP_IDS\n"
                                + json.dumps([group_id], ensure_ascii=False),
                                "# PROGRAM_REJECTION\n"
                                + json.dumps(rejection, ensure_ascii=False),
                                "# LOCAL_REPAIR_TASK\n此前整批两次尝试后该组没有留下合法候选。"
                                "只重做此 group_id，直接满足上述程序拒绝项；"
                                "一次调用仍应尽量给出三种候选。",
                            ]
                        ),
                        timeout=min(args.timeout, 300),
                        max_tokens=min(64000, args.max_tokens * 2),
                        json_mode=True,
                        thinking_mode="enabled",
                        reasoning_effort=args.reasoning_effort,
                        request_attempts=args.http_attempts,
                    )
                    assert_schema_version("candidates", repaired)
                    repaired, repair_normalizations = (
                        _normalize_redundant_terminal_cuts(
                            single_analysis,
                            repaired,
                            master=master,
                            units=units,
                        )
                    )
                    repaired, repair_dropped = _filter_invalid_candidates(
                        single_analysis,
                        repaired,
                        master=master,
                        units=units,
                    )
                    repair_issues = _a2_issues(
                        single_analysis,
                        repaired,
                        master=master,
                        units=units,
                    )
                    if repair_issues:
                        raise Stage1PipelineError(
                            json.dumps(repair_issues[:8], ensure_ascii=False)
                        )
                    candidate_groups_by_id[group_id]["candidates"] = list(
                        repaired["groups"][0]["candidates"]
                    )
                    a2_normalizations.extend(repair_normalizations)
                    a2_dropped_candidates.extend(repair_dropped)
                    a2_local_repairs.append(
                        {
                            "group_id": group_id,
                            "status": "completed",
                            **repair_telemetry,
                        }
                    )
                    write_json(
                        batch_dir / f"stage1_candidates_local_{group_id}.json",
                        repaired,
                    )
                except (OSError, ValueError, Stage1PipelineError) as exc:
                    fallback_candidate = _deterministic_a2_delivery_candidate(
                        source_group,
                        master=master,
                        units=units,
                    )
                    fallback_trial = {
                        "schema_version": "substar.stage1.candidates.v2",
                        "groups": [
                            {
                                "group_id": group_id,
                                "candidates": [fallback_candidate],
                            }
                        ],
                    }
                    fallback_issues = _a2_issues(
                        single_analysis,
                        fallback_trial,
                        master=master,
                        units=units,
                    )
                    if not fallback_issues:
                        candidate_groups_by_id[group_id]["candidates"] = [
                            fallback_candidate
                        ]
                        a2_local_repairs.append(
                            {
                                "group_id": group_id,
                                "status": "deterministic_delivery_fallback",
                                "api_error": str(exc),
                            }
                        )
                        write_json(
                            batch_dir
                            / f"stage1_candidates_fallback_{group_id}.json",
                            fallback_trial,
                        )
                        continue
                    a2_local_repairs.append(
                        {
                            "group_id": group_id,
                            "status": "failed",
                            "error": str(exc),
                            "fallback_issues": fallback_issues[:8],
                        }
                    )
            remaining_issues = _a2_issues(
                analysis,
                candidates,
                master=master,
                units=units,
            )
            if remaining_issues:
                raise Stage1PipelineError(
                    f"布局批次 b{number:04d} A2 有限局部补救后仍无效："
                    + json.dumps(remaining_issues[:8], ensure_ascii=False)
                )
    diversity = _candidate_diversity_report(analysis, candidates)
    supplement_used = False
    if (
        diversity["needs_supplement"]
        and not a2_resumed
        and not a2_local_repairs
    ):
        try:
            supplement, supplement_telemetry = _call_model_with_one_content_retry(
                base_url=args.base_url,
                api_key=api_key,
                model=args.model,
                system_prompt=stage_system("candidates", material),
                user_payload="\n\n".join(
                    [
                        "# INPUT_MATERIAL\n" + material,
                        "# GLOBAL_A1_ANALYSIS_BATCH\n"
                        + json.dumps(analysis, ensure_ascii=False),
                        "# EXISTING_CANDIDATES\n"
                        + json.dumps(candidates, ensure_ascii=False),
                        "# COVERAGE_REPORT\n"
                        + json.dumps(diversity, ensure_ascii=False),
                        "# SUPPLEMENT_TASK\n只补齐报告指出的候选差异或高价值边界正反覆盖；"
                        "最多本次一次，不得重复已有切点数组。",
                    ]
                ),
                timeout=min(args.timeout, 300),
                max_tokens=args.max_tokens,
                json_mode=True,
                thinking_mode="enabled",
                reasoning_effort=args.reasoning_effort,
                request_attempts=args.http_attempts,
            )
            assert_schema_version("candidates", supplement)
            supplement, supplement_normalizations = _normalize_redundant_terminal_cuts(
                analysis, supplement, master=master, units=units
            )
            a2_normalizations.extend(supplement_normalizations)
            merged_candidates = _merge_candidate_supplement(candidates, supplement)
            merged_candidates, supplement_dropped = _filter_invalid_candidates(
                analysis,
                merged_candidates,
                master=master,
                units=units,
            )
            a2_dropped_candidates.extend(supplement_dropped)
            supplement_issues = _a2_issues(
                analysis,
                merged_candidates,
                master=master,
                units=units,
            )
            if supplement_issues:
                raise Stage1PipelineError(
                    f"布局批次 b{number:04d} A2 补充后破坏合法性："
                    + json.dumps(supplement_issues[:8], ensure_ascii=False)
                )
            candidates = merged_candidates
            write_json(batch_dir / "stage1_candidates_supplement.json", supplement)
            write_json(
                batch_dir / "api_call_03A2_supplement.json",
                supplement_telemetry,
            )
            supplement_used = True
            diversity = _candidate_diversity_report(analysis, candidates)
        except (OSError, ValueError, Stage1PipelineError) as exc:
            write_json(
                batch_dir / "api_call_03A2_supplement.json",
                {
                    "bounded_failure": True,
                    "error": str(exc),
                    "proceeded_with_existing_candidates": True,
                },
            )
    expected_group_ids = [str(group["group_id"]) for group in groups]
    actual_group_ids = [
        str(group.get("group_id", "")) for group in candidates.get("groups", [])
    ]
    if actual_group_ids != expected_group_ids:
        raise Stage1PipelineError(
            f"布局批次 b{number:04d} A2 未完整覆盖 group_id："
            f"expected={expected_group_ids} actual={actual_group_ids}"
        )
    write_json(batch_dir / "stage1_candidates.json", candidates)
    write_json(batch_dir / "stage1_candidate_coverage.json", diversity)
    write_json(batch_dir / "api_call_03A2.json", a2_telemetry)
    write_json(
        batch_dir / "stage1_candidate_normalizations.json",
        {
            "schema_version": "substar.stage1.candidate-normalizations.v1",
            "actions": a2_normalizations,
            "dropped_candidates": a2_dropped_candidates,
            "local_repairs": a2_local_repairs,
        },
    )

    blinded = blind_candidates(candidates, 20260726 + number)
    write_json(batch_dir / "stage1_candidates_blinded.json", blinded)
    error = ""
    for attempt in range(1, 3):
        try:
            decision, a3_telemetry = _call_model_with_one_content_retry(
                base_url=args.base_url,
                api_key=api_key,
                model=args.model,
                system_prompt=stage_system("decision", material),
                user_payload="\n\n".join(
                    [
                        "# INPUT_MATERIAL\n" + material,
                        "# GLOBAL_A1_ANALYSIS_BATCH\n"
                        + json.dumps(analysis, ensure_ascii=False),
                        "# BLINDED_CANDIDATES\n"
                        + json.dumps(blinded, ensure_ascii=False),
                        (
                            "# PROGRAM_REJECTION\n" + error
                            if error
                            else ""
                        ),
                    ]
                ),
                timeout=min(args.timeout, 300),
                max_tokens=(
                    args.max_tokens
                    if attempt == 1
                    else min(64000, args.max_tokens * 2)
                ),
                json_mode=True,
                thinking_mode="enabled",
                reasoning_effort=args.reasoning_effort,
                request_attempts=args.http_attempts,
            )
            write_json(
                batch_dir / f"stage1_decision_raw_{attempt}.json",
                decision,
            )
            assert_schema_version("decision", decision)
            plan = _direct_plan_from_blind_decision(analysis, blinded, decision)
            local_units = [
                unit for unit in units if start <= int(unit.index) <= end
            ]
            result = evaluate_direct_plan(
                _slice_master_text(master, units, start, end),
                local_units,
                plan,
                **source_punctuation_kwargs(),
            )
            batch_issues = _batch_hard_issues(result)
            if batch_issues:
                raise Stage1PipelineError(
                    json.dumps(batch_issues[:8], ensure_ascii=False)
                )
            decision, plan, a4_telemetry = _run_boundary_gate(
                analysis=analysis,
                blinded=blinded,
                decision=decision,
                master=master,
                units=units,
                start=start,
                end=end,
                material=material,
                args=args,
                api_key=api_key,
                batch_dir=batch_dir,
            )
            write_json(batch_dir / "stage1_decision.json", decision)
            write_json(batch_dir / "stage1_a3_frozen_plan.json", plan)
            write_json(
                batch_dir / "stage1_boundary_fingerprint.json",
                cuts_fingerprint(plan),
            )
            write_json(batch_dir / "api_call_03A3.json", a3_telemetry)
            return number, candidates, decision, plan, {
                "a2": a2_telemetry,
                "a3": a3_telemetry,
                "a4": a4_telemetry,
                "supplement_used": supplement_used,
                "local_repairs": a2_local_repairs,
                "coverage": diversity,
            }
        except (OSError, ValueError, Stage1PipelineError) as exc:
            error = str(exc)
    decision, plan = _deterministic_candidate_fallback(
        analysis=analysis,
        candidates=candidates,
        blinded=blinded,
        master=master,
        units=units,
        start=start,
        end=end,
    )
    decision, plan, a4_telemetry = _run_boundary_gate(
        analysis=analysis,
        blinded=blinded,
        decision=decision,
        master=master,
        units=units,
        start=start,
        end=end,
        material=material,
        args=args,
        api_key=api_key,
        batch_dir=batch_dir,
    )
    write_json(batch_dir / "stage1_decision.json", decision)
    write_json(batch_dir / "stage1_a3_frozen_plan.json", plan)
    write_json(batch_dir / "stage1_boundary_fingerprint.json", cuts_fingerprint(plan))
    write_json(
        batch_dir / "api_call_03A3.json",
        {
            "bounded_fallback": True,
            "attempts": 2,
            "last_error": error,
            "optimizer_called": False,
        },
    )
    return number, candidates, decision, plan, {
        "a2": a2_telemetry,
        "a3": {
            "bounded_fallback": True,
            "attempts": 2,
            "last_error": error,
        },
        "a4": a4_telemetry,
        "supplement_used": supplement_used,
        "local_repairs": a2_local_repairs,
        "coverage": diversity,
    }


def _slice_master_text(master: str, units: list[Any], start: int, end: int) -> str:
    positions = {int(unit.index): position for position, unit in enumerate(units)}
    ranges = _unit_original_ranges(master, units)
    left = positions[start]
    right = positions[end]
    char_start = 0 if left == 0 else ranges[left][0]
    char_end = len(master) if right == len(units) - 1 else ranges[right + 1][0]
    return master[char_start:char_end].strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="分层保护、A1 前置接缝和冻结 A3 的 Stage1 实验"
    )
    parser.add_argument("--material", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--api-key-env", default="SUBSTAR_LLM_API_KEY")
    parser.add_argument("--target-seconds", type=float, default=360.0)
    parser.add_argument("--overlap-seconds", type=float, default=40.0)
    parser.add_argument("--layout-batch-groups", type=int, default=4)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--max-tokens", type=int, default=24000)
    parser.add_argument("--reasoning-effort", default="high")
    parser.add_argument("--http-attempts", type=int, default=2)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    api_key, key_source = resolve_api_key(args.api_key_env)
    if not api_key:
        raise Stage1PipelineError("未配置 Stage1 LLM API key")

    material = args.material.read_text(encoding="utf-8-sig")
    master = extract_master(material)
    units = extract_alignment(material)
    chunks = build_adaptive_analysis_chunks(
        material,
        target_seconds=args.target_seconds,
        overlap_seconds=args.overlap_seconds,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        args.output_dir / "stage1_overlap_manifest.json",
        {
            "schema_version": "substar.stage1.adaptive-chunks.v1",
            "target_seconds": args.target_seconds,
            "overlap_seconds": args.overlap_seconds,
            "key_source": key_source,
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "core_start": chunk.core_start,
                    "core_end": chunk.core_end,
                    "analysis_start": chunk.analysis_start,
                    "analysis_end": chunk.analysis_end,
                }
                for chunk in chunks
            ],
        },
    )

    analyses: dict[str, dict[str, Any]] = {}
    a1_telemetry: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(args.workers, len(chunks))
    ) as executor:
        futures = [
            executor.submit(
                _call_a1,
                chunk,
                args,
                api_key,
                args.output_dir,
                master,
                units,
            )
            for chunk in chunks
        ]
        for future in concurrent.futures.as_completed(futures):
            chunk_id, analysis, telemetry = future.result()
            analyses[chunk_id] = analysis
            a1_telemetry.append({"chunk_id": chunk_id, **telemetry})

    seeds = seam_seed_windows(chunks)
    for seed in seeds:
        left_groups = analyses[str(seed["left_chunk"])].get("groups", [])
        right_groups = analyses[str(seed["right_chunk"])].get("groups", [])
        left_start = int(left_groups[0]["alignment_start"])
        left_end = int(left_groups[-1]["alignment_end"])
        right_start = int(right_groups[0]["alignment_start"])
        right_end = int(right_groups[-1]["alignment_end"])
        actual_overlap_start = max(left_start, right_start)
        actual_overlap_end = min(left_end, right_end)
        if actual_overlap_start <= actual_overlap_end:
            seed["window_start"] = actual_overlap_start
            seed["window_end"] = actual_overlap_end
        else:
            boundary = int(seed["technical_boundary_after"])
            seed["window_start"] = max(left_start, min(left_end, boundary))
            seed["window_end"] = min(
                right_end,
                max(right_start, boundary + 1),
            )
    seams: list[dict[str, Any]] = []
    if seeds:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(args.workers, len(seeds))
        ) as executor:
            futures = [
                executor.submit(
                    _resolve_seam,
                    seed=seed,
                    left_analysis=analyses[str(seed["left_chunk"])],
                    right_analysis=analyses[str(seed["right_chunk"])],
                    master=master,
                    units=units,
                    args=args,
                    api_key=api_key,
                    output_dir=args.output_dir,
                )
                for seed in seeds
            ]
            seams = [future.result() for future in futures]
        seams.sort(key=lambda item: int(item["window_start"]))
    write_json(
        args.output_dir / "stage1_seam_consensus.json",
        {"schema_version": "substar.stage1.a1-seams.v1", "seams": seams},
    )

    global_groups: list[dict[str, Any]] = []
    for position, chunk in enumerate(chunks):
        stable_start = (
            int(units[0].index)
            if position == 0
            else int(seams[position - 1]["window_end"]) + 1
        )
        stable_end = (
            int(units[-1].index)
            if position == len(chunks) - 1
            else int(seams[position]["window_start"]) - 1
        )
        global_groups.extend(
            _groups_tiling(analyses[chunk.chunk_id], stable_start, stable_end)
        )
        if position < len(seams):
            global_groups.extend(copy.deepcopy(seams[position]["groups"]))
    global_groups = _renumber_groups(global_groups)
    global_analysis = {
        "schema_version": "substar.stage1.analysis.v2",
        "source_language": ",".join(
            dict.fromkeys(
                str(analyses[chunk.chunk_id].get("source_language", "Auto"))
                for chunk in chunks
            )
        ),
        "groups": global_groups,
        "coverage_check": {
            "complete": True,
            "ordered": True,
            "notes": ["A1 自适应重叠提案经前置接缝裁决后合并"],
        },
    }
    issues = _analysis_issues(
        global_analysis,
        start=int(units[0].index),
        end=int(units[-1].index),
        master=master,
        units=units,
    )
    multi_center_ids = {
        str(issue.get("group_id"))
        for issue in issues
        if issue.get("code") == "suspected_multi_center_group"
    }
    if multi_center_ids:
        units_by_index = {int(unit.index): unit for unit in units}
        groups_by_id = {
            str(group.get("group_id")): group for group in global_groups
        }

        def refine_multi_center(
            group_id: str,
        ) -> tuple[str, dict[str, Any], dict[str, Any]]:
            group = groups_by_id[group_id]
            group_start = int(group["alignment_start"])
            group_end = int(group["alignment_end"])
            selected_units = [
                units_by_index[index]
                for index in range(group_start, group_end + 1)
            ]
            refinement = AdaptiveAnalysisChunk(
                chunk_id=f"refine_{group_id}",
                core_start=group_start,
                core_end=group_end,
                analysis_start=group_start,
                analysis_end=group_end,
                start_seconds=float(selected_units[0].start),
                end_seconds=float(selected_units[-1].end),
                material=_slice_material(
                    master,
                    units,
                    group_start,
                    group_end,
                ),
                units=selected_units,
            )
            return _call_a1(
                chunk=refinement,
                args=args,
                api_key=api_key,
                output_dir=args.output_dir / "a1_refinements",
                master=master,
                units=units,
            )

        refinements: dict[str, dict[str, Any]] = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(args.workers, len(multi_center_ids))
        ) as executor:
            futures = {
                executor.submit(refine_multi_center, group_id): group_id
                for group_id in sorted(multi_center_ids)
            }
            for future in concurrent.futures.as_completed(futures):
                _, refined, _ = future.result()
                refinements[futures[future]] = refined
        refined_groups: list[dict[str, Any]] = []
        for group in global_groups:
            group_id = str(group.get("group_id"))
            if group_id in refinements:
                refined_groups.extend(
                    copy.deepcopy(refinements[group_id].get("groups", []))
                )
            else:
                refined_groups.append(copy.deepcopy(group))
        global_groups = _renumber_groups(refined_groups)
        global_analysis["groups"] = global_groups
        issues = _analysis_issues(
            global_analysis,
            start=int(units[0].index),
            end=int(units[-1].index),
            master=master,
            units=units,
        )
    hard_issues = [
        issue
        for issue in issues
        if issue.get("code") != "suspected_multi_center_group"
    ]
    if hard_issues:
        raise Stage1PipelineError(
            "全片 A1 合并失败："
            + json.dumps(hard_issues[:8], ensure_ascii=False)
        )
    if issues:
        write_json(
            args.output_dir / "stage1_analysis_warnings.json",
            {
                "schema_version": "substar.stage1.analysis-warnings.v1",
                "issues": issues,
                "policy": "一次局部 A1 复核后仅保留疑似多中心告警；硬错误禁止进入 A2",
            },
        )
    write_json(args.output_dir / "stage1_analysis.json", global_analysis)

    batches = _chunk_groups(global_groups, max(1, args.layout_batch_groups))
    completed: dict[int, tuple[dict, dict, dict, dict]] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(args.workers, len(batches))
    ) as executor:
        futures = [
            executor.submit(
                _run_a2_a3_batch,
                number=number,
                groups=batch,
                master=master,
                units=units,
                args=args,
                api_key=api_key,
                output_dir=args.output_dir,
            )
            for number, batch in enumerate(batches, start=1)
        ]
        for future in concurrent.futures.as_completed(futures):
            number, candidates, decision, plan, telemetry = future.result()
            completed[number] = (candidates, decision, plan, telemetry)

    candidate_groups: list[dict[str, Any]] = []
    decision_groups: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []
    layout_telemetry: list[dict[str, Any]] = []
    for number in range(1, len(batches) + 1):
        candidates, decision, plan, telemetry = completed[number]
        candidate_groups.extend(candidates["groups"])
        decision_groups.extend(decision["groups"])
        plans.append(plan)
        layout_telemetry.append({"batch": number, **telemetry})
    final_plan = merge_direct_plans(plans)
    result = evaluate_direct_plan(
        master,
        units,
        final_plan,
        **source_punctuation_kwargs(),
    )
    if not result.valid:
        raise Stage1PipelineError(
            "冻结 A3 全片计划未通过硬校验："
            + json.dumps(result.issues[:10], ensure_ascii=False)
        )
    write_json(
        args.output_dir / "stage1_candidates.json",
        {"schema_version": "substar.stage1.candidates.v2", "groups": candidate_groups},
    )
    write_json(
        args.output_dir / "stage1_decision.json",
        {
            "schema_version": "substar.stage1.decision.v2",
            "groups": decision_groups,
            "final_draft": result.draft.strip(),
        },
    )
    write_json(args.output_dir / "stage1_a3_frozen_plan.json", final_plan)
    write_json(args.output_dir / "stage1_direct_plan.json", final_plan)
    write_json(
        args.output_dir / "stage1_boundary_fingerprint.json",
        cuts_fingerprint(final_plan),
    )
    write_two_level_artifacts(args.output_dir, master, units, final_plan)
    (args.output_dir / "stage03A_source_draft.txt").write_text(
        result.draft, encoding="utf-8"
    )
    write_json(
        args.output_dir / "stage1_final_validation.json",
        {
            "schema_version": "substar.stage1.hierarchical-validation.v1",
            "valid": result.valid,
            "issues": result.issues,
            "review_notices": result.review_notices,
            "optimizer_called": False,
            "a3_boundary_retention": 1.0,
            "a1_telemetry": a1_telemetry,
            "layout_telemetry": layout_telemetry,
            "seam_count": len(seams),
            "seam_api_count": sum(
                item.get("resolution") == "api_adjudication" for item in seams
            ),
            "seam_fallback_count": sum(
                str(item.get("resolution", "")).endswith("fallback")
                for item in seams
            ),
            "candidate_coverage": candidate_coverage_report(
                global_analysis,
                {"groups": candidate_groups},
                hard_character_limit=int(
                    source_punctuation_kwargs().get("english_hard_limit", 55)
                ),
            ),
        },
    )
    print(
        f"valid=true groups={len(global_groups)} seams={len(seams)} "
        f"layout_batches={len(batches)} optimizer_called=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
