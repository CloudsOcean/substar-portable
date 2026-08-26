from __future__ import annotations

import argparse
import difflib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


TIMECODE_RE = re.compile(
    r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{3})"
)
CHAR_RE = re.compile(r"[0-9a-z\u3400-\u9fff]", re.IGNORECASE)
KNOWN_SRT_PATHS = (
    "offline_boundary/substar_bilingual.srt",
    "substar_bilingual_final.srt",
    "substar_bilingual.srt",
    "substar_bilingual_reviewed.srt",
)
KNOWN_PLAN_PATHS = (
    "stage1_direct_plan.json",
    "p3_direct_plan.json",
    "p3_initial_plan.json",
)
KNOWN_P1_PATHS = (
    "p1_protection.json",
    "p1_analysis.json",
    "result/p1.json",
)


@dataclass(frozen=True)
class Cue:
    number: int
    start: float
    end: float
    lines: tuple[str, ...]


def seconds(value: str) -> float:
    match = TIMECODE_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"无效时间码：{value}")
    return (
        int(match["h"]) * 3600
        + int(match["m"]) * 60
        + int(match["s"])
        + int(match["ms"]) / 1000
    )


def parse_srt(path: Path) -> tuple[list[Cue], list[dict[str, Any]]]:
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    blocks = [block for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]
    cues: list[Cue] = []
    errors: list[dict[str, Any]] = []
    for block_number, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        if len(lines) < 3 or "-->" not in lines[1]:
            errors.append(
                {
                    "code": "malformed_srt_block",
                    "block": block_number,
                }
            )
            continue
        try:
            number = int(lines[0].strip())
            start_text, end_text = [
                item.strip() for item in lines[1].split("-->", 1)
            ]
            start = seconds(start_text)
            end = seconds(end_text)
        except (ValueError, IndexError) as exc:
            errors.append(
                {
                    "code": "malformed_srt_block",
                    "block": block_number,
                    "detail": str(exc),
                }
            )
            continue
        cues.append(
            Cue(
                number=number,
                start=start,
                end=end,
                lines=tuple(line.strip() for line in lines[2:] if line.strip()),
            )
        )
    return cues, errors


def source_line(cue: Cue) -> str:
    if not cue.lines:
        return ""
    scored: list[tuple[int, int, str]] = []
    for line in cue.lines:
        latin = len(re.findall(r"[A-Za-z]", line))
        han = len(re.findall(r"[\u3400-\u9fff]", line))
        scored.append((latin - han, latin, line))
    return max(scored)[2]


def normalized_characters(text: str) -> str:
    return "".join(CHAR_RE.findall(text.casefold()))


def character_stream_and_boundaries(cues: list[Cue]) -> tuple[str, list[int]]:
    stream = ""
    boundaries: list[int] = []
    for index, cue in enumerate(cues):
        stream += normalized_characters(source_line(cue))
        if index + 1 < len(cues):
            boundaries.append(len(stream))
    return stream, boundaries


def coordinate_anchors(reference: str, candidate: str) -> list[tuple[int, int]]:
    matcher = difflib.SequenceMatcher(
        None,
        candidate,
        reference,
        autojunk=False,
    )
    anchors = [(0, 0)]
    for block in matcher.get_matching_blocks():
        anchors.append((block.a, block.b))
        anchors.append((block.a + block.size, block.b + block.size))
    anchors.append((len(candidate), len(reference)))
    return sorted(set(anchors))


def map_coordinate(position: int, anchors: list[tuple[int, int]]) -> int:
    for index in range(1, len(anchors)):
        right_candidate, right_reference = anchors[index]
        if position > right_candidate:
            continue
        left_candidate, left_reference = anchors[index - 1]
        if right_candidate == left_candidate:
            return right_reference
        fraction = (
            (position - left_candidate)
            / (right_candidate - left_candidate)
        )
        return round(
            left_reference
            + fraction * (right_reference - left_reference)
        )
    return anchors[-1][1]


def match_numeric_boundaries(
    expected: list[float | int],
    actual: list[float | int],
    tolerance: float,
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Return a deterministic one-to-one nearest matching.

    Reference positions are processed in order.  This is appropriate for
    monotonic subtitle boundaries and prevents one candidate boundary from
    satisfying two reference boundaries.
    """

    used: set[int] = set()
    matches: list[tuple[int, int]] = []
    missed: list[int] = []
    for expected_index, reference in enumerate(expected):
        choices = [
            (abs(float(value) - float(reference)), actual_index)
            for actual_index, value in enumerate(actual)
            if actual_index not in used
            and abs(float(value) - float(reference)) <= tolerance
        ]
        if not choices:
            missed.append(expected_index)
            continue
        _, actual_index = min(choices)
        used.add(actual_index)
        matches.append((expected_index, actual_index))
    extras = [index for index in range(len(actual)) if index not in used]
    return matches, missed, extras


def metrics(
    expected: list[float | int],
    actual: list[float | int],
    tolerance: float,
) -> dict[str, Any]:
    matches, missed, extras = match_numeric_boundaries(
        expected, actual, tolerance
    )
    precision = len(matches) / len(actual) if actual else float(not expected)
    recall = len(matches) / len(expected) if expected else float(not actual)
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "expected": len(expected),
        "actual": len(actual),
        "matched": len(matches),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "missed_reference_indexes": missed,
        "extra_candidate_indexes": extras,
    }


def character_boundary_report(
    reference: list[Cue],
    candidate: list[Cue],
) -> dict[str, Any]:
    reference_stream, reference_boundaries = character_stream_and_boundaries(
        reference
    )
    candidate_stream, candidate_boundaries = character_stream_and_boundaries(
        candidate
    )
    anchors = coordinate_anchors(reference_stream, candidate_stream)
    mapped = [
        map_coordinate(boundary, anchors) for boundary in candidate_boundaries
    ]
    exact = metrics(reference_boundaries, mapped, 0)
    return {
        "normalization": (
            "casefold; keep letters, digits and CJK; align character streams; "
            "compare cue-boundary character offsets"
        ),
        "reference_character_count": len(reference_stream),
        "candidate_character_count": len(candidate_stream),
        "stream_similarity": round(
            difflib.SequenceMatcher(
                None, reference_stream, candidate_stream, autojunk=False
            ).ratio(),
            4,
        ),
        "exact": exact,
        "character_boundary_similarity": exact["f1"],
    }


def srt_hard_errors(
    cues: list[Cue],
    parse_errors: list[dict[str, Any]],
    hard_limit: int,
) -> list[dict[str, Any]]:
    errors = list(parse_errors)
    previous_end = -1.0
    for expected_number, cue in enumerate(cues, start=1):
        source = source_line(cue)
        if cue.number != expected_number:
            errors.append(
                {
                    "code": "non_contiguous_numbering",
                    "cue": cue.number,
                    "expected": expected_number,
                }
            )
        if cue.end <= cue.start:
            errors.append(
                {
                    "code": "non_positive_duration",
                    "cue": cue.number,
                    "start": cue.start,
                    "end": cue.end,
                }
            )
        if cue.start < previous_end - 0.001:
            errors.append(
                {
                    "code": "overlapping_time",
                    "cue": cue.number,
                    "start": cue.start,
                    "previous_end": previous_end,
                }
            )
        previous_end = max(previous_end, cue.end)
        if not source:
            errors.append({"code": "missing_source_text", "cue": cue.number})
        if len(source) > hard_limit:
            errors.append(
                {
                    "code": "source_over_hard_limit",
                    "cue": cue.number,
                    "characters": len(source),
                    "limit": hard_limit,
                    "text": source,
                }
            )
    if not cues:
        errors.append({"code": "no_srt_cues"})
    return errors


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def first_existing(root: Path, relative_paths: Iterable[str]) -> Path | None:
    for relative in relative_paths:
        candidate = root / relative
        if candidate.is_file():
            return candidate
    return None


def discover_srt(route: Path) -> Path:
    if route.is_file():
        if route.suffix.lower() != ".srt":
            raise ValueError(f"route 文件不是 SRT：{route}")
        return route
    known = first_existing(route, KNOWN_SRT_PATHS)
    if known:
        return known
    candidates = sorted(route.rglob("*.srt"))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError(f"route 内没有 SRT：{route}")
    raise ValueError(
        f"route 内有多个未知 SRT，无法安全猜测：{route}；"
        "请把 --route 指向具体 SRT 文件"
    )


def plan_hard_errors(
    plan: dict[str, Any] | None,
    p1: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], set[int]]:
    if plan is None:
        return [], set()
    errors: list[dict[str, Any]] = []
    groups = plan.get("groups")
    if not isinstance(groups, list) or not groups:
        return [{"code": "plan_missing_groups"}], set()
    previous_end: int | None = None
    all_cuts: set[int] = set()
    for position, group in enumerate(groups, start=1):
        try:
            start = int(group["alignment_start"])
            end = int(group["alignment_end"])
            cuts = [int(value) for value in group.get("line_breaks_after", [])]
        except (KeyError, TypeError, ValueError):
            errors.append({"code": "invalid_plan_group", "group": position})
            continue
        if start > end:
            errors.append(
                {"code": "reversed_plan_group", "group": position}
            )
        if previous_end is not None and start != previous_end + 1:
            errors.append(
                {
                    "code": "plan_coverage_gap_or_overlap",
                    "group": position,
                    "expected_start": previous_end + 1,
                    "actual_start": start,
                }
            )
        if cuts != sorted(set(cuts)):
            errors.append(
                {"code": "non_monotonic_or_duplicate_cuts", "group": position}
            )
        for cut in cuts:
            if not start <= cut < end:
                errors.append(
                    {
                        "code": "cut_outside_group",
                        "group": position,
                        "cut": cut,
                        "start": start,
                        "end": end,
                    }
                )
            all_cuts.add(cut)
        previous_end = end

    plan_coverage = plan.get("coverage_check")
    if isinstance(plan_coverage, dict):
        if plan_coverage.get("complete") is False:
            errors.append({"code": "plan_declares_incomplete_coverage"})
        if plan_coverage.get("ordered") is False:
            errors.append({"code": "plan_declares_unordered_coverage"})
    p1_coverage = p1.get("coverage_check") if p1 else None
    if isinstance(p1_coverage, dict):
        try:
            expected_start = int(p1_coverage["alignment_start"])
            expected_end = int(p1_coverage["alignment_end"])
            actual_start = int(groups[0]["alignment_start"])
            actual_end = int(groups[-1]["alignment_end"])
            if actual_start != expected_start or actual_end != expected_end:
                errors.append(
                    {
                        "code": "plan_does_not_cover_p1_range",
                        "expected_start": expected_start,
                        "expected_end": expected_end,
                        "actual_start": actual_start,
                        "actual_end": actual_end,
                    }
                )
        except (KeyError, TypeError, ValueError):
            errors.append({"code": "invalid_p1_coverage_check"})

    hard_spans: list[dict[str, Any]] = []
    if p1:
        hard_spans.extend(
            span
            for span in p1.get("spans", [])
            if str(
                span.get("protection_level", span.get("level", ""))
            ) == "hard"
        )
    for group in groups:
        hard_spans.extend(
            span
            for span in group.get("protected_spans", [])
            if str(span.get("protection_level", "hard")) == "hard"
        )
    for cut in sorted(all_cuts):
        for span in hard_spans:
            try:
                span_start = int(span["alignment_start"])
                span_end = int(span["alignment_end"])
            except (KeyError, TypeError, ValueError):
                continue
            if span_start <= cut < span_end:
                errors.append(
                    {
                        "code": "hard_protected_span_cut",
                        "cut": cut,
                        "span_id": span.get("span_id"),
                        "span_start": span_start,
                        "span_end": span_end,
                    }
                )
                break
    return errors, all_cuts


def p1_statistics(
    p1: dict[str, Any] | None,
    plan_cuts: set[int],
    hard_limit: int,
) -> dict[str, Any] | None:
    if p1 is None:
        return None
    spans = p1.get("spans", [])
    if not isinstance(spans, list):
        return {"present": True, "invalid": True, "span_count": 0}
    levels: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    attachments: Counter[str] = Counter()
    invalid_spans: list[dict[str, Any]] = []
    internal_by_level: dict[str, set[int]] = {}
    normalized: list[tuple[int, int, str, str | None]] = []
    oversized_hard_or_strong = 0
    hard_cut_count = 0
    for position, span in enumerate(spans, start=1):
        try:
            start = int(span["alignment_start"])
            end = int(span["alignment_end"])
        except (KeyError, TypeError, ValueError):
            invalid_spans.append(
                {"position": position, "code": "invalid_span_indexes"}
            )
            continue
        level = str(span.get("protection_level", span.get("level", "")))
        if start > end or level not in {"hard", "strong_soft", "outer_soft"}:
            invalid_spans.append(
                {
                    "position": position,
                    "code": "invalid_span",
                    "start": start,
                    "end": end,
                    "level": level,
                }
            )
            continue
        levels[level] += 1
        categories[str(span.get("category", "unknown"))] += 1
        attachments[str(span.get("attachment", "unknown"))] += 1
        internal_by_level.setdefault(level, set()).update(range(start, end))
        normalized.append((start, end, level, span.get("span_id")))
        char_count = span.get("source_char_count")
        if (
            level in {"hard", "strong_soft"}
            and isinstance(char_count, (int, float))
            and char_count > hard_limit
        ):
            oversized_hard_or_strong += 1
        if level == "hard":
            hard_cut_count += sum(1 for cut in plan_cuts if start <= cut < end)

    crossing_pairs = 0
    for left_index, left in enumerate(normalized):
        for right in normalized[left_index + 1 :]:
            left_start, left_end = left[:2]
            right_start, right_end = right[:2]
            overlaps = left_start <= right_end and right_start <= left_end
            nested = (
                left_start <= right_start <= right_end <= left_end
                or right_start <= left_start <= left_end <= right_end
            )
            if overlaps and not nested:
                crossing_pairs += 1

    preferred = p1.get("preferred_breaks_after", [])
    forbidden = p1.get("forbidden_breaks_after", [])
    preferred_positions = {
        int(item["after_alignment"])
        for item in preferred
        if isinstance(item, dict) and "after_alignment" in item
    }
    forbidden_positions = {
        int(item["after_alignment"])
        for item in forbidden
        if isinstance(item, dict) and "after_alignment" in item
    }
    hard_positions = internal_by_level.get("hard", set())
    coverage = p1.get("coverage_check")
    covered_gap_count = len(set().union(*internal_by_level.values())) if internal_by_level else 0
    total_gap_count = None
    if isinstance(coverage, dict):
        try:
            total_gap_count = max(
                0,
                int(coverage["alignment_end"])
                - int(coverage["alignment_start"]),
            )
        except (KeyError, TypeError, ValueError):
            total_gap_count = None
    return {
        "present": True,
        "span_count": len(spans),
        "level_counts": dict(levels),
        "category_counts": dict(categories),
        "attachment_counts": dict(attachments),
        "invalid_spans": invalid_spans,
        "crossing_span_pair_count": crossing_pairs,
        "protected_internal_boundary_counts": {
            level: len(boundaries)
            for level, boundaries in sorted(internal_by_level.items())
        },
        "covered_internal_boundary_count": covered_gap_count,
        "coverage_ratio": (
            round(covered_gap_count / total_gap_count, 4)
            if total_gap_count
            else None
        ),
        "preferred_break_count": len(preferred_positions),
        "forbidden_break_count": len(forbidden_positions),
        "hard_preferred_contradiction_count": len(
            hard_positions & preferred_positions
        ),
        "oversized_hard_or_strong_count": oversized_hard_or_strong,
        "final_plan_hard_cut_count": hard_cut_count,
        "coverage_check": coverage,
    }


def route_artifacts(route: Path) -> tuple[Path, Path | None, Path | None]:
    srt = discover_srt(route)
    if route.is_file():
        return srt, None, None
    return (
        srt,
        first_existing(route, KNOWN_PLAN_PATHS),
        first_existing(route, KNOWN_P1_PATHS),
    )


def parse_route_argument(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("route 必须写成 NAME=PATH")
    name, raw_path = value.split("=", 1)
    if not name.strip() or not raw_path.strip():
        raise argparse.ArgumentTypeError("route 必须写成 NAME=PATH")
    return name.strip(), Path(raw_path.strip())


def audit_route(
    name: str,
    route: Path,
    reference_cues: list[Cue],
    *,
    hard_limit: int,
    exact_ms: int,
    tolerance_ms: int,
) -> dict[str, Any]:
    srt_path, plan_path, p1_path = route_artifacts(route)
    candidate_cues, parse_errors = parse_srt(srt_path)
    plan = read_json(plan_path) if plan_path else None
    p1 = read_json(p1_path) if p1_path else None
    hard_errors = srt_hard_errors(candidate_cues, parse_errors, hard_limit)
    plan_errors, plan_cuts = plan_hard_errors(plan, p1)
    hard_errors.extend(plan_errors)
    protection = p1_statistics(p1, plan_cuts, hard_limit)
    if protection:
        if protection.get("invalid"):
            hard_errors.append({"code": "invalid_p1_spans_container"})
        for item in protection.get("invalid_spans", []):
            hard_errors.append({"code": "invalid_p1_span", **item})
        if protection.get("crossing_span_pair_count", 0):
            hard_errors.append(
                {
                    "code": "crossing_p1_spans",
                    "pair_count": protection["crossing_span_pair_count"],
                }
            )
        coverage = protection.get("coverage_check")
        if isinstance(coverage, dict) and coverage.get("complete") is not True:
            hard_errors.append({"code": "p1_coverage_not_complete"})

    reference_boundaries = [cue.end for cue in reference_cues[:-1]]
    candidate_boundaries = [cue.end for cue in candidate_cues[:-1]]
    exact = metrics(
        reference_boundaries,
        candidate_boundaries,
        exact_ms / 1000,
    )
    tolerant = metrics(
        reference_boundaries,
        candidate_boundaries,
        tolerance_ms / 1000,
    )
    relative_edits = {
        "tolerance_ms": tolerance_ms,
        "delete_reference_boundary_count": len(
            tolerant["missed_reference_indexes"]
        ),
        "add_candidate_boundary_count": len(
            tolerant["extra_candidate_indexes"]
        ),
        "total_boundary_add_delete_count": (
            len(tolerant["missed_reference_indexes"])
            + len(tolerant["extra_candidate_indexes"])
        ),
        "note": (
            "这些是相对人工参考的边界差异，不是硬错误；"
            "其中可包含合理风格差异。"
        ),
    }
    return {
        "route": name,
        "route_path": str(route.resolve()),
        "candidate_srt": str(srt_path.resolve()),
        "plan": str(plan_path.resolve()) if plan_path else None,
        "p1": str(p1_path.resolve()) if p1_path else None,
        "cue_count": len(candidate_cues),
        "hard_error_count": len(hard_errors),
        "hard_errors": hard_errors,
        "character_boundary": character_boundary_report(
            reference_cues, candidate_cues
        ),
        "cue_boundary_time": {
            "exact": {"tolerance_ms": exact_ms, **exact},
            "tolerant": {"tolerance_ms": tolerance_ms, **tolerant},
        },
        "relative_reference_boundary_edits": relative_edits,
        "p1_protection": protection,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "离线审计多条 Stage1 route；边界差异只计相似度/编辑量，"
            "不当作硬错误"
        )
    )
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument(
        "--route",
        action="append",
        required=True,
        type=parse_route_argument,
        metavar="NAME=PATH",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--english-hard-limit", type=int, default=55)
    parser.add_argument("--exact-ms", type=int, default=1)
    parser.add_argument("--tolerance-ms", type=int, default=500)
    args = parser.parse_args()
    if args.exact_ms < 0 or args.tolerance_ms < args.exact_ms:
        parser.error("必须满足 0 <= exact-ms <= tolerance-ms")
    route_names = [name for name, _ in args.route]
    if len(route_names) != len(set(route_names)):
        parser.error("route NAME 不得重复")

    reference_cues, reference_errors = parse_srt(args.reference)
    if reference_errors or not reference_cues:
        raise SystemExit(f"人工参考 SRT 无法解析：{reference_errors}")
    routes = [
        audit_route(
            name,
            path,
            reference_cues,
            hard_limit=args.english_hard_limit,
            exact_ms=args.exact_ms,
            tolerance_ms=args.tolerance_ms,
        )
        for name, path in args.route
    ]
    report = {
        "schema_version": "substar.evaluation.three-routes.v1",
        "reference": str(args.reference.resolve()),
        "reference_cue_count": len(reference_cues),
        "configuration": {
            "english_hard_limit": args.english_hard_limit,
            "english_count_spaces": True,
            "english_count_punctuation": True,
            "exact_tolerance_ms": args.exact_ms,
            "tolerant_match_ms": args.tolerance_ms,
        },
        "interpretation": {
            "hard_errors": (
                "只包含格式、覆盖、时间、硬字符上限和 hard 保护契约错误。"
            ),
            "boundary_differences": (
                "相对人工参考的增删和不匹配仅是编辑/相似度指标；"
                "合理风格差异不计为硬错。"
            ),
        },
        "routes": routes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        "complete "
        + " ".join(
            f"{route['route']}:hard={route['hard_error_count']},"
            f"f1={route['cue_boundary_time']['tolerant']['f1']}"
            for route in routes
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
