from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any


WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?|\d+(?:\.\d+)?")
TERMINAL_RE = re.compile(r"[.!?。！？][\"'”’)]*$")

# These are broad grammatical classes used only as review signals. They are
# deliberately not rewrite rules and never move a boundary by themselves.
UNFINISHED_END = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "because",
    "but",
    "by",
    "for",
    "from",
    "if",
    "in",
    "into",
    "of",
    "on",
    "or",
    "than",
    "the",
    "their",
    "to",
    "when",
    "while",
    "whose",
    "with",
}
UNFINISHED_CONTRACTIONS = {
    "he's",
    "i'm",
    "it's",
    "she's",
    "that's",
    "there's",
    "they're",
    "we're",
    "what's",
    "you're",
}
INDEPENDENT_SHORT = {
    "absolutely",
    "agreed",
    "amazing",
    "correct",
    "exactly",
    "goodbye",
    "great",
    "hello",
    "hi",
    "incredible",
    "maybe",
    "no",
    "okay",
    "ok",
    "perfect",
    "please",
    "really",
    "right",
    "sorry",
    "sure",
    "thanks",
    "true",
    "unbelievable",
    "what",
    "whoa",
    "wow",
    "yeah",
    "yes",
}


def visible_length(text: str) -> int:
    return len(text)


def normalize_word(value: str) -> str:
    return value.casefold().replace("’", "'")


def parse_draft(path: Path) -> list[list[str]]:
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    return [
        [line.strip() for line in block.splitlines() if line.strip()]
        for block in re.split(r"\n\s*\n", text.strip())
        if block.strip()
    ]


def percentile(values: list[int], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(ordered[low])
    return ordered[low] * (high - position) + ordered[high] * (position - low)


def short_is_independent(text: str, words: list[str]) -> bool:
    normalized = " ".join(normalize_word(word) for word in words)
    if normalized in INDEPENDENT_SHORT:
        return True
    if text.rstrip().endswith(("?", "!", "？", "！")):
        return True
    if len(words) == 1 and words[0][:1].isupper():
        return True
    return False


def assess(path: Path, hard_limit: int) -> dict[str, Any]:
    groups = parse_draft(path)
    rows: list[dict[str, Any]] = []
    for group_index, group in enumerate(groups, start=1):
        for line_index, text in enumerate(group, start=1):
            words = WORD_RE.findall(text)
            rows.append(
                {
                    "group": group_index,
                    "line": line_index,
                    "text": text,
                    "words": words,
                    "word_count": len(words),
                    "length": visible_length(text),
                    "terminal": bool(TERMINAL_RE.search(text.rstrip())),
                }
            )

    high_risk: list[dict[str, Any]] = []
    mergeable_fragments: list[dict[str, Any]] = []
    short_review: list[dict[str, Any]] = []
    unbalanced_groups: list[dict[str, Any]] = []

    for position, row in enumerate(rows):
        words = row["words"]
        if not words:
            continue
        last = normalize_word(words[-1])
        if (
            last in UNFINISHED_END or last in UNFINISHED_CONTRACTIONS
        ) and not row["terminal"]:
            high_risk.append(
                {
                    "group": row["group"],
                    "line": row["line"],
                    "text": row["text"],
                    "reason": "unfinished_function_end",
                    "final_word": words[-1],
                }
            )

        is_short = row["word_count"] <= 3 or row["length"] <= 12
        if not is_short or short_is_independent(row["text"], words):
            continue
        candidates: list[dict[str, Any]] = []
        for neighbour_position, direction in (
            (position - 1, "previous"),
            (position + 1, "next"),
        ):
            if not 0 <= neighbour_position < len(rows):
                continue
            neighbour = rows[neighbour_position]
            # Cross-group adjacency may represent a real sentence, speaker or
            # translation-unit boundary. Count it separately through explicit
            # dangling evidence; do not label every short complete sentence as
            # mechanically mergeable with unrelated neighbouring content.
            if neighbour["group"] != row["group"]:
                continue
            combined = (
                f"{neighbour['text']} {row['text']}"
                if direction == "previous"
                else f"{row['text']} {neighbour['text']}"
            )
            if visible_length(combined) <= hard_limit:
                candidates.append(
                    {
                        "direction": direction,
                        "combined_length": visible_length(combined),
                        "neighbour": neighbour["text"],
                    }
                )
        item = {
            "group": row["group"],
            "line": row["line"],
            "text": row["text"],
            "word_count": row["word_count"],
            "length": row["length"],
            "merge_options": candidates,
        }
        short_review.append(item)
        if candidates:
            mergeable_fragments.append(item)

    for group_index, group in enumerate(groups, start=1):
        lengths = [visible_length(line) for line in group]
        if len(lengths) < 2 or max(lengths) < 28:
            continue
        shortest = min(lengths)
        longest = max(lengths)
        if shortest <= 12 or shortest / longest < 0.32:
            unbalanced_groups.append(
                {
                    "group": group_index,
                    "lengths": lengths,
                    "lines": group,
                }
            )

    lengths = [int(row["length"]) for row in rows]
    over_hard = [row for row in rows if int(row["length"]) > hard_limit]
    # A transparent comparison score, not a product acceptance gate. Severe
    # grammatical tails dominate; fragments and extreme imbalance are weaker.
    penalty = (
        len(high_risk) * 8
        + len(mergeable_fragments) * 3
        + len(unbalanced_groups) * 1
        + len(over_hard) * 100
    )
    return {
        "schema_version": "substar.stage1.quality-evaluation.v1",
        "draft": str(path.resolve()),
        "hard_limit": hard_limit,
        "group_count": len(groups),
        "cue_count": len(rows),
        "length": {
            "mean": round(sum(lengths) / len(lengths), 3) if lengths else 0,
            "p10": round(percentile(lengths, 0.10), 3),
            "median": round(percentile(lengths, 0.50), 3),
            "p90": round(percentile(lengths, 0.90), 3),
            "maximum": max(lengths, default=0),
            "hard_violations": len(over_hard),
        },
        "issue_counts": {
            "high_risk_dangling": len(high_risk),
            "short_review": len(short_review),
            "mergeable_fragments": len(mergeable_fragments),
            "unbalanced_groups": len(unbalanced_groups),
        },
        "comparison_penalty": penalty,
        "issues": {
            "high_risk_dangling": high_risk,
            "short_review": short_review,
            "mergeable_fragments": mergeable_fragments,
            "unbalanced_groups": unbalanced_groups,
            "hard_violations": over_hard,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="评估 Stage 1 草案的泛化切分质量")
    parser.add_argument("draft", type=Path)
    parser.add_argument("--hard-limit", type=int, default=55)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = assess(args.draft, args.hard_limit)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
