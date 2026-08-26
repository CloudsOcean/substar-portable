from __future__ import annotations

import argparse
import difflib
import json
import re
from pathlib import Path
from typing import Any

from evaluate_srt_with_mask import SrtCue, in_ranges, read_srt, source_line


CHAR_RE = re.compile(r"[0-9a-z\u3400-\u9fff]", re.IGNORECASE)


def normalized_characters(text: str) -> str:
    return "".join(CHAR_RE.findall(text.casefold()))


def selected(
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


def character_stream_and_boundaries(
    cues: list[SrtCue],
) -> tuple[str, list[int]]:
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


def map_coordinate(
    position: int,
    anchors: list[tuple[int, int]],
) -> int:
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


def greedy_exact_matches(
    reference: list[int],
    candidate: list[int],
) -> int:
    available = set(candidate)
    matched = 0
    for boundary in reference:
        if boundary in available:
            available.remove(boundary)
            matched += 1
    return matched


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只按规范化字符流中的换行位置评价切分相似度"
    )
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--mask", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    mask = json.loads(args.mask.read_text(encoding="utf-8"))
    include = mask.get("include_ranges", mask.get("include", []))
    exclude = mask.get("exclude_ranges", mask.get("exclude", []))
    reference_cues = selected(read_srt(args.reference), include, exclude)
    candidate_cues = selected(read_srt(args.candidate), include, exclude)
    reference_stream, reference_boundaries = (
        character_stream_and_boundaries(reference_cues)
    )
    candidate_stream, candidate_boundaries = (
        character_stream_and_boundaries(candidate_cues)
    )
    anchors = coordinate_anchors(reference_stream, candidate_stream)
    mapped_candidate = [
        map_coordinate(boundary, anchors)
        for boundary in candidate_boundaries
    ]
    exact = greedy_exact_matches(reference_boundaries, mapped_candidate)
    precision = (
        exact / len(mapped_candidate) if mapped_candidate else 0.0
    )
    recall = (
        exact / len(reference_boundaries)
        if reference_boundaries
        else 0.0
    )
    similarity = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    report = {
        "schema_version": (
            "substar.evaluation.character-boundary-similarity.v1"
        ),
        "normalization": (
            "casefold; keep only letters, digits and CJK; align character "
            "streams; compare newline character offsets"
        ),
        "reference_boundary_count": len(reference_boundaries),
        "candidate_boundary_count": len(mapped_candidate),
        "exact_character_boundary_matches": exact,
        "character_boundary_similarity": round(similarity, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"character_boundary_similarity="
        f"{report['character_boundary_similarity']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
