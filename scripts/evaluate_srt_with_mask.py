from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TIMECODE = re.compile(
    r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2}),(?P<ms>\d{3})"
)


@dataclass(frozen=True)
class SrtCue:
    cue_id: int
    start: float
    end: float
    lines: tuple[str, ...]


def seconds(value: str) -> float:
    match = TIMECODE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"无效 SRT 时间码：{value}")
    return (
        int(match["h"]) * 3600
        + int(match["m"]) * 60
        + int(match["s"])
        + int(match["ms"]) / 1000
    )


def read_srt(path: Path) -> list[SrtCue]:
    cues: list[SrtCue] = []
    body = path.read_text(encoding="utf-8-sig").strip()
    for block in re.split(r"\r?\n\s*\r?\n", body):
        lines = block.splitlines()
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start_text, end_text = [item.strip() for item in lines[1].split("-->", 1)]
        cues.append(
            SrtCue(
                cue_id=int(lines[0].strip()),
                start=seconds(start_text),
                end=seconds(end_text),
                lines=tuple(line.strip() for line in lines[2:] if line.strip()),
            )
        )
    return cues


def in_ranges(value: float, ranges: list[dict[str, Any]]) -> bool:
    return any(float(item["start"]) <= value < float(item["end"]) for item in ranges)


def source_line(cue: SrtCue) -> str:
    if not cue.lines:
        return ""
    scored = []
    for line in cue.lines:
        latin = len(re.findall(r"[A-Za-z]", line))
        han = len(re.findall(r"[\u3400-\u9fff]", line))
        scored.append((latin - han, latin, line))
    return max(scored)[2]


def target_line(cue: SrtCue) -> str:
    source = source_line(cue)
    return next((line for line in cue.lines if line != source), "")


def match_boundaries(
    expected: list[float],
    actual: list[float],
    tolerance: float,
) -> tuple[int, list[float], list[float]]:
    used: set[int] = set()
    matched = 0
    misses: list[float] = []
    for reference in expected:
        candidates = [
            (abs(value - reference), index)
            for index, value in enumerate(actual)
            if index not in used and abs(value - reference) <= tolerance
        ]
        if not candidates:
            misses.append(reference)
            continue
        _, index = min(candidates)
        used.add(index)
        matched += 1
    extras = [value for index, value in enumerate(actual) if index not in used]
    return matched, misses, extras


def boundary_metrics(expected: list[float], actual: list[float], tolerance: float) -> dict:
    matched, misses, extras = match_boundaries(expected, actual, tolerance)
    precision = matched / len(actual) if actual else float(not expected)
    recall = matched / len(expected) if expected else float(not actual)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tolerance_ms": round(tolerance * 1000),
        "expected": len(expected),
        "actual": len(actual),
        "matched": matched,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "missed_times": [round(value, 3) for value in misses],
        "extra_times": [round(value, 3) for value in extras],
    }


def structural_audit(cues: list[SrtCue], english_hard_limit: int) -> dict:
    bad_ids: list[int] = []
    reversed_or_empty: list[int] = []
    overlaps: list[int] = []
    previous_end = 0.0
    for cue in cues:
        source = source_line(cue)
        target = target_line(cue)
        if len(source) > english_hard_limit:
            bad_ids.append(cue.cue_id)
        if cue.end <= cue.start or not source or not target:
            reversed_or_empty.append(cue.cue_id)
        if cue.start < previous_end - 0.001:
            overlaps.append(cue.cue_id)
        previous_end = max(previous_end, cue.end)
    return {
        "cue_count": len(cues),
        "english_hard_limit": english_hard_limit,
        "source_over_limit_ids": bad_ids,
        "invalid_or_missing_bilingual_ids": reversed_or_empty,
        "overlap_ids": overlaps,
        "passed": not bad_ids and not reversed_or_empty and not overlaps,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="按评分掩码比较 Substar SRT")
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--mask", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--english-hard-limit", type=int, default=55)
    args = parser.parse_args()

    mask = json.loads(args.mask.read_text(encoding="utf-8"))
    include = mask.get("include_ranges", mask.get("include", []))
    exclude = mask.get("exclude_ranges", mask.get("exclude", []))
    reference_all = read_srt(args.reference)
    candidate_all = read_srt(args.candidate)

    def scored(cues: list[SrtCue]) -> list[SrtCue]:
        return [
            cue
            for cue in cues
            if in_ranges((cue.start + cue.end) / 2, include)
            and not in_ranges((cue.start + cue.end) / 2, exclude)
        ]

    reference = scored(reference_all)
    candidate = scored(candidate_all)
    reference_boundaries = [cue.end for cue in reference[:-1]]
    candidate_boundaries = [cue.end for cue in candidate[:-1]]
    report = {
        "schema_version": "substar.evaluation.masked-srt.v1",
        "reference": str(args.reference.resolve()),
        "candidate": str(args.candidate.resolve()),
        "mask": str(args.mask.resolve()),
        "scored_duration_seconds": sum(
            float(item["end"]) - float(item["start"]) for item in include
        ),
        "semantic_score_excludes": exclude,
        "boundary_metrics": {
            "500ms": boundary_metrics(reference_boundaries, candidate_boundaries, 0.5),
            "1000ms": boundary_metrics(reference_boundaries, candidate_boundaries, 1.0),
        },
        "scored_cue_counts": {
            "reference": len(reference),
            "candidate": len(candidate),
        },
        "full_delivery_structural_audit": structural_audit(
            candidate_all, args.english_hard_limit
        ),
        "interpretation": (
            "边界指标只衡量入选十分钟；结构审计覆盖完整节目。"
            "高风险片段不参与语义优劣结论，但仍必须形成合法双语SRT。"
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "complete "
        f"f1_500ms={report['boundary_metrics']['500ms']['f1']} "
        f"delivery_passed={report['full_delivery_structural_audit']['passed']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
