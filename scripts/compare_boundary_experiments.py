from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evaluate_srt_with_mask import (
    SrtCue,
    boundary_metrics,
    in_ranges,
    read_srt,
    source_line,
    structural_audit,
)


def scored(
    cues: list[SrtCue],
    include: list[dict[str, Any]],
    exclude: list[dict[str, Any]],
) -> list[SrtCue]:
    return [
        cue
        for cue in cues
        if in_ranges((cue.start + cue.end) / 2, include)
        and not in_ranges((cue.start + cue.end) / 2, exclude)
    ]


def greedy_matches(
    expected: list[float],
    actual: list[float],
    tolerance: float,
) -> dict[int, int]:
    used: set[int] = set()
    matches: dict[int, int] = {}
    for reference_index, reference in enumerate(expected):
        candidates = [
            (abs(value - reference), index)
            for index, value in enumerate(actual)
            if index not in used and abs(value - reference) <= tolerance
        ]
        if not candidates:
            continue
        _, actual_index = min(candidates)
        used.add(actual_index)
        matches[reference_index] = actual_index
    return matches


def boundary_context(
    cues: list[SrtCue],
    boundary_index: int,
) -> dict[str, Any]:
    left = cues[boundary_index]
    right = (
        cues[boundary_index + 1]
        if boundary_index + 1 < len(cues)
        else None
    )
    return {
        "time": round(left.end, 3),
        "left": source_line(left),
        "right": source_line(right) if right else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="比较两个切分实验相对人工稿的边界得失"
    )
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--mask", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tolerance-ms", type=int, default=500)
    parser.add_argument("--english-hard-limit", type=int, default=55)
    args = parser.parse_args()

    mask = json.loads(args.mask.read_text(encoding="utf-8"))
    include = mask.get("include_ranges", mask.get("include", []))
    exclude = mask.get("exclude_ranges", mask.get("exclude", []))
    reference_all = read_srt(args.reference)
    baseline_all = read_srt(args.baseline)
    candidate_all = read_srt(args.candidate)
    reference = scored(reference_all, include, exclude)
    baseline = scored(baseline_all, include, exclude)
    candidate = scored(candidate_all, include, exclude)
    reference_boundaries = [cue.end for cue in reference[:-1]]
    baseline_boundaries = [cue.end for cue in baseline[:-1]]
    candidate_boundaries = [cue.end for cue in candidate[:-1]]
    tolerance = args.tolerance_ms / 1000
    baseline_matches = greedy_matches(
        reference_boundaries, baseline_boundaries, tolerance
    )
    candidate_matches = greedy_matches(
        reference_boundaries, candidate_boundaries, tolerance
    )
    gained = sorted(set(candidate_matches) - set(baseline_matches))
    lost = sorted(set(baseline_matches) - set(candidate_matches))
    retained = sorted(set(baseline_matches) & set(candidate_matches))

    def outcome(
        reference_index: int,
        match_index: int,
        cues: list[SrtCue],
    ) -> dict[str, Any]:
        item = boundary_context(reference, reference_index)
        matched = boundary_context(cues, match_index)
        return {
            "reference": item,
            "matched": matched,
            "absolute_delta_ms": round(
                abs(item["time"] - matched["time"]) * 1000
            ),
        }

    baseline_metric = boundary_metrics(
        reference_boundaries, baseline_boundaries, tolerance
    )
    candidate_metric = boundary_metrics(
        reference_boundaries, candidate_boundaries, tolerance
    )
    report = {
        "schema_version": (
            "substar.evaluation.boundary-experiment-comparison.v1"
        ),
        "tolerance_ms": args.tolerance_ms,
        "baseline_metrics": baseline_metric,
        "candidate_metrics": candidate_metric,
        "delta": {
            "matched": candidate_metric["matched"]
            - baseline_metric["matched"],
            "actual_boundaries": candidate_metric["actual"]
            - baseline_metric["actual"],
            "precision": round(
                candidate_metric["precision"]
                - baseline_metric["precision"],
                4,
            ),
            "recall": round(
                candidate_metric["recall"]
                - baseline_metric["recall"],
                4,
            ),
            "f1": round(
                candidate_metric["f1"] - baseline_metric["f1"],
                4,
            ),
        },
        "reference_boundary_outcomes": {
            "retained_count": len(retained),
            "gained_count": len(gained),
            "lost_count": len(lost),
            "gained": [
                outcome(index, candidate_matches[index], candidate)
                for index in gained
            ],
            "lost": [
                outcome(index, baseline_matches[index], baseline)
                for index in lost
            ],
        },
        "full_delivery_structural_audit": {
            "baseline": structural_audit(
                baseline_all, args.english_hard_limit
            ),
            "candidate": structural_audit(
                candidate_all, args.english_hard_limit
            ),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"complete f1_delta={report['delta']['f1']} "
        f"gained={len(gained)} lost={len(lost)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
