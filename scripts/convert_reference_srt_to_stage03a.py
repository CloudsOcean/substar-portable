from __future__ import annotations

import argparse
import json
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from run_stage03a import extract_master, split_sentences


@dataclass
class Cue:
    index: int
    timing: str
    top: str
    bottom: str


def parse_srt(text: str) -> list[Cue]:
    cues: list[Cue] = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = [line.strip() for line in block.splitlines()]
        if len(lines) < 4 or "-->" not in lines[1]:
            continue
        payload = lines[2:]
        # The reference contract is exactly English top / Chinese bottom. If a
        # track was accidentally wrapped, preserve all extra text in bottom so
        # the structural anomaly remains visible in the report.
        cues.append(Cue(int(lines[0]), lines[1], payload[0], " ".join(payload[1:])))
    return cues


def match_norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in value if char.isalnum())


def display_normalize(value: str) -> str:
    out: list[str] = []
    for i, char in enumerate(value):
        prev_char = value[i - 1] if i else ""
        next_char = value[i + 1] if i + 1 < len(value) else ""
        if char == "." and prev_char.isdigit() and next_char.isdigit():
            out.append(char)
        elif char in {",", "，", "、"}:
            out.append(" ")
        elif char in {".", "。"}:
            continue
        else:
            out.append(char)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def fuzzy_at_cursor(candidate: str, master: str, cursor: int) -> tuple[float, int, int]:
    if not candidate:
        return 0.0, cursor, cursor
    search_end = min(len(master), cursor + max(220, len(candidate) * 4))
    exact = master.find(candidate, cursor, search_end)
    if exact >= 0:
        gap_penalty = min(0.15, (exact - cursor) / 1000)
        return 1.0 - gap_penalty, exact, exact + len(candidate)

    best = (0.0, cursor, min(len(master), cursor + len(candidate)))
    for start in range(cursor, min(search_end, cursor + 90) + 1):
        for delta in range(-10, 11, 2):
            end = min(len(master), start + max(1, len(candidate) + delta))
            window = master[start:end]
            ratio = SequenceMatcher(None, candidate, window, autojunk=False).ratio()
            ratio -= min(0.1, (start - cursor) / 1000)
            if ratio > best[0]:
                best = (ratio, start, end)
    return best


def choose_sources(cues: list[Cue], master: str) -> tuple[list[dict], list[dict]]:
    master_n = match_norm(master)
    cursor = 0
    selected: list[dict] = []
    mismatches: list[dict] = []
    for cue in cues:
        candidates = [("top", cue.top), ("bottom", cue.bottom)]
        scored = []
        for track, value in candidates:
            score, start, end = fuzzy_at_cursor(match_norm(value), master_n, cursor)
            scored.append((score, start, end, track, value))
        score, start, end, track, value = max(scored, key=lambda item: item[0])
        selected.append(
            {
                "cue": cue.index,
                "source": value,
                "source_display": display_normalize(value),
                "target": cue.bottom if track == "top" else cue.top,
                "track": track,
                "score": round(score, 4),
                "start": start,
                "end": end,
            }
        )
        if score < 0.92:
            mismatches.append(
                {"cue": cue.index, "track": track, "score": round(score, 4), "text": value}
            )
        cursor = max(cursor, end)
    return selected, mismatches


def sentence_ranges(master: str) -> list[tuple[int, int]]:
    master_n = match_norm(master)
    cursor = 0
    ranges: list[tuple[int, int]] = []
    for sentence in split_sentences(master):
        piece = match_norm(sentence)
        start = master_n.find(piece, cursor)
        if start < 0:
            start = cursor
        end = start + len(piece)
        ranges.append((start, end))
        cursor = end
    return ranges


def sentence_id(position: int, ranges: list[tuple[int, int]]) -> int:
    for index, (start, end) in enumerate(ranges):
        if start <= position < end:
            return index
    return len(ranges)


def build_stage03a(selected: list[dict], master: str) -> tuple[str, int]:
    ranges = sentence_ranges(master)
    lines: list[str] = []
    group_count = 0
    for i, item in enumerate(selected):
        lines.append(item["source_display"])
        if i == len(selected) - 1:
            group_count += 1
            continue
        current_sentence = sentence_id(item["end"] - 1, ranges)
        next_sentence = sentence_id(selected[i + 1]["start"], ranges)
        same_target = match_norm(item["target"]) == match_norm(selected[i + 1]["target"])
        if current_sentence != next_sentence and not same_target:
            lines.append("")
            group_count += 1
    return "\n".join(lines).strip() + "\n", group_count


def parse_timestamp(value: str) -> int:
    hours, minutes, rest = value.split(":")
    seconds, milliseconds = rest.split(",")
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(milliseconds)


def build_display_track_stage03a(cues: list[Cue]) -> tuple[str, int]:
    """Convert the fixed English-top display track to the minimal 03A shape.

    This is intentionally a display-track reference, not a claim about which
    spoken language was original in code-switched cues.
    """
    continuation_starts = {
        "and", "but", "because", "where", "who", "which", "that", "to", "for",
        "with", "from", "of", "in", "on", "at", "as", "when", "while", "if",
        "than", "then", "so", "or", "calls", "covering", "documentary", "is",
        "it", "you", "we", "they", "still", "surrounded", "ancient", "go",
    }
    hanging_ends = {
        "a", "an", "the", "and", "but", "or", "of", "to", "for", "with",
        "from", "in", "on", "at", "some", "this", "that", "these", "those",
    }
    lines: list[str] = []
    group_count = 0
    for i, cue in enumerate(cues):
        lines.append(display_normalize(cue.top))
        if i == len(cues) - 1:
            group_count += 1
            continue
        next_cue = cues[i + 1]
        current_end = parse_timestamp(cue.timing.split(" --> ")[1])
        next_start = parse_timestamp(next_cue.timing.split(" --> ")[0])
        gap = next_start - current_end
        next_first = (next_cue.top.strip().split(maxsplit=1) or [""])[0].casefold()
        current_last = (cue.top.strip().split() or [""])[-1].casefold().strip("!?\"'”’")
        same_target = match_norm(cue.bottom) == match_norm(next_cue.bottom)
        continues = next_first in continuation_starts or current_last in hanging_ends
        if not (same_target or (gap <= 350 and continues)):
            lines.append("")
            group_count += 1
    return "\n".join(lines).strip() + "\n", group_count


def forbidden_punctuation(value: str) -> list[str]:
    found: list[str] = []
    for i, char in enumerate(value):
        if char not in {",", "，", "。", ".", "、"}:
            continue
        if char == ".":
            prev_char = value[i - 1] if i else ""
            next_char = value[i + 1] if i + 1 < len(value) else ""
            if prev_char.isdigit() and next_char.isdigit():
                continue
        found.append(char)
    return found


def line_stats(lines: list[str], language: str) -> dict:
    if language == "en":
        counts = [len(re.sub(r"\s+", "", line)) for line in lines]
        soft, hard = 50, 55
    else:
        counts = [len(re.findall(r"[\u3400-\u9fff]", line)) for line in lines]
        soft, hard = 14, 18
    return {
        "count": len(lines),
        "max": max(counts, default=0),
        "soft_exceptions": sum(soft < count <= hard for count in counts),
        "hard_violations": sum(count > hard for count in counts),
    }


def sequential_boundaries(lines: list[str], master: str) -> set[int]:
    master_n = match_norm(master)
    cursor = 0
    result: set[int] = set()
    for line in lines[:-1]:
        piece = match_norm(line)
        score, start, end = fuzzy_at_cursor(piece, master_n, cursor)
        if score < 0.65:
            end = min(len(master_n), cursor + len(piece))
        cursor = end
        result.add(end)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("srt", type=Path)
    parser.add_argument("material", type=Path)
    parser.add_argument("current_draft", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()

    cues = parse_srt(args.srt.read_text(encoding="utf-8-sig"))
    master = extract_master(args.material.read_text(encoding="utf-8-sig"))
    selected, mismatches = choose_sources(cues, master)
    converted, group_count = build_display_track_stage03a(cues)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(converted, encoding="utf-8")

    reference_source_lines = [display_normalize(cue.top) for cue in cues]
    current_lines = [
        line.strip()
        for line in args.current_draft.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    ref_boundaries = sequential_boundaries(reference_source_lines, master)
    current_boundaries = sequential_boundaries(current_lines, master)
    exact = len(ref_boundaries & current_boundaries)
    within_five = sum(
        1 for boundary in current_boundaries if any(abs(boundary - ref) <= 5 for ref in ref_boundaries)
    )

    top_lines = [cue.top for cue in cues]
    bottom_lines = [cue.bottom for cue in cues]
    ref_joined = "".join(match_norm(cue.top) for cue in cues)
    master_n = match_norm(master)
    coverage_similarity = SequenceMatcher(None, master_n, ref_joined, autojunk=False).ratio()

    report = {
        "reference_srt": {
            "cue_count": len(cues),
            "translation_group_count": group_count,
            "conversion_track": "fixed_english_top_display_track",
            "english_track_master_similarity": round(coverage_similarity, 5),
            "low_match_cue_count": len(mismatches),
            "low_match_cue_examples": mismatches[:30],
            "english_limits": line_stats(top_lines, "en"),
            "chinese_limits": line_stats(bottom_lines, "zh"),
            "lower_punctuation_cues": [
                {
                    "cue": cue.index,
                    "top": forbidden_punctuation(cue.top),
                    "bottom": forbidden_punctuation(cue.bottom),
                }
                for cue in cues
                if forbidden_punctuation(cue.top) or forbidden_punctuation(cue.bottom)
            ],
            "upper_punctuation_cues": [
                cue.index for cue in cues if re.search(r"[“”‘’]", cue.top + cue.bottom)
            ],
        },
        "current_stage03a": {
            "cue_count": len(current_lines),
            "english_limits": line_stats(current_lines, "en"),
        },
        "boundary_comparison": {
            "reference_boundary_count": len(ref_boundaries),
            "current_boundary_count": len(current_boundaries),
            "exact_matches": exact,
            "current_boundaries_within_5_normalized_characters": within_five,
        },
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
