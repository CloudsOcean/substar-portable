from __future__ import annotations

import argparse
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
    _direct_report,
    source_punctuation_kwargs,
    write_json,
    write_two_level_artifacts,
)
from substar_core.stage1 import extract_alignment, extract_master  # noqa: E402
from substar_core.stage1_direct import evaluate_direct_plan  # noqa: E402


LEFT_DANGLING = {
    "a", "an", "the", "and", "or", "but", "because", "if", "when", "while",
    "with", "without", "of", "to", "for", "from", "in", "on", "at", "by",
    "my", "your", "our", "their", "this", "that", "these", "those",
}
RIGHT_CLITIC = {"'s", "'re", "'ve", "'ll", "'d", "n't"}


def overlimit_metric(result: Any) -> tuple[int, int, int]:
    rows = [
        issue["detail"]
        for issue in result.issues
        if str(issue.get("code", "")).startswith("draft_")
        and "_over_" in str(issue.get("code", ""))
    ]
    if not rows:
        return (0, 0, 0)
    overages = [
        max(
            0,
            int(row.get("length", 0))
            - (55 if "english" in str(row.get("code", "")) else 24),
        )
        for row in rows
    ]
    return (len(rows), sum(overages), max(overages))


def inside_protected(group: dict[str, Any], boundary: int) -> bool:
    return any(
        isinstance(span, dict)
        and int(span.get("alignment_start", boundary + 1))
        <= boundary
        < int(span.get("alignment_end", boundary))
        for span in group.get("protected_spans", [])
    )


def offending_ranges(
    result: Any, plan: dict[str, Any]
) -> dict[int, list[tuple[int, int]]]:
    ranges: dict[int, list[tuple[int, int]]] = {}
    for issue in result.issues:
        code = str(issue.get("code", ""))
        if not code.startswith("draft_") or "_over_" not in code:
            continue
        detail = issue.get("detail", {})
        group_position = int(detail.get("group", 0)) - 1
        cue_position = int(detail.get("cue", 0)) - 1
        if not 0 <= group_position < len(plan.get("groups", [])):
            continue
        group = plan["groups"][group_position]
        boundaries = [
            int(group["alignment_start"]) - 1,
            *sorted(int(value) for value in group.get("line_breaks_after", [])),
            int(group["alignment_end"]),
        ]
        if not 0 <= cue_position < len(boundaries) - 1:
            continue
        ranges.setdefault(group_position, []).append(
            (boundaries[cue_position] + 1, boundaries[cue_position + 1])
        )
    return ranges


def boundary_penalty(units_by_index: dict[int, Any], boundary: int) -> tuple[int, int]:
    left = str(units_by_index[boundary].text).casefold().strip()
    right = str(units_by_index[boundary + 1].text).casefold().strip()
    semantic = 0
    if left in LEFT_DANGLING:
        semantic += 100
    if right in RIGHT_CLITIC:
        semantic += 100
    # A preposition beginning the right-hand phrase is often natural; do not
    # punish it. Balance is deliberately a weak final tie-breaker.
    return semantic, abs(len(left) - len(right))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="冻结现有计划，只为消除字符硬超限增加最少量行内切点"
    )
    parser.add_argument("material", type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    material = args.material.read_text(encoding="utf-8")
    master = extract_master(material)
    units = extract_alignment(material)
    units_by_index = {int(unit.index): unit for unit in units}
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    result = evaluate_direct_plan(
        master, units, plan, review_confidence=0.72, **source_punctuation_kwargs()
    )
    actions: list[dict[str, Any]] = []

    for _ in range(128):
        current_metric = overlimit_metric(result)
        if current_metric == (0, 0, 0):
            break
        active_ranges = offending_ranges(result, plan)
        best: tuple[Any, ...] | None = None
        for group_position, ranges in active_ranges.items():
            group = plan["groups"][group_position]
            existing = {int(value) for value in group.get("line_breaks_after", [])}
            for start, end in ranges:
                for boundary in range(start, end):
                    if (
                        boundary in existing
                        or boundary not in units_by_index
                        or boundary + 1 not in units_by_index
                        or inside_protected(group, boundary)
                    ):
                        continue
                    candidate = copy.deepcopy(plan)
                    candidate_group = candidate["groups"][group_position]
                    candidate_group["line_breaks_after"] = sorted(existing | {boundary})
                    candidate_result = evaluate_direct_plan(
                        master,
                        units,
                        candidate,
                        review_confidence=0.72,
                        **source_punctuation_kwargs(),
                    )
                    metric = overlimit_metric(candidate_result)
                    if metric >= current_metric:
                        continue
                    semantic, weak_balance = boundary_penalty(units_by_index, boundary)
                    rank = (metric, semantic, weak_balance, boundary)
                    if best is None or rank < best[0]:
                        best = (
                            rank,
                            candidate,
                            candidate_result,
                            group["group_id"],
                            boundary,
                        )
        if best is None:
            break
        _, plan, result, group_id, boundary = best
        actions.append(
            {
                "type": "hard_limit_split_only",
                "group_id": group_id,
                "line_break_after": boundary,
                "remaining_overlimit_metric": overlimit_metric(result),
            }
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.output_dir / "stage1_direct_plan.json", plan)
    write_json(args.output_dir / "hard_limit_actions.json", actions)
    write_two_level_artifacts(args.output_dir, master, units, plan)
    (args.output_dir / "stage03A_source_draft.txt").write_text(
        result.draft, encoding="utf-8"
    )
    write_json(
        args.output_dir / "stage03A_validation_report.json",
        _direct_report(result, repaired=True, attempts=1),
    )
    print(
        json.dumps(
            {
                "valid": result.valid,
                "hard_limit_actions": len(actions),
                "remaining_metric": overlimit_metric(result),
                "issues": len(result.issues),
                "review_notices": len(result.review_notices),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result.valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
