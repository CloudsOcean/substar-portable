from __future__ import annotations

import argparse
import difflib
import json
import re
from pathlib import Path


CORRECTION_RE = re.compile(r"^([^/\s]+)/([^/\s]+)/$")


def raw_token(token: str) -> str:
    if token.endswith("//"):
        return token[:-2]
    correction = CORRECTION_RE.match(token)
    if correction:
        return correction.group(1)
    return token


def normalize_token(token: str) -> str:
    value = raw_token(token)
    value = re.sub(r"[，。,.、]", "", value)
    return value


def parse(path: Path, ignore_parenthetical_insertions: bool) -> tuple[list[str], set[int], set[int]]:
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    if ignore_parenthetical_insertions:
        text = re.sub(r"\([^()\n]+\)", "", text)
    tokens: list[str] = []
    cue_boundaries: set[int] = set()
    group_boundaries: set[int] = set()
    blocks = [block for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]
    for block_index, block in enumerate(blocks):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        for line_index, line in enumerate(lines):
            for item in line.split():
                token = normalize_token(item)
                if token:
                    tokens.append(token)
            if line_index < len(lines) - 1:
                cue_boundaries.add(len(tokens))
        if block_index < len(blocks) - 1:
            group_boundaries.add(len(tokens))
    return tokens, cue_boundaries, group_boundaries


def clipped(boundaries: set[int], token_count: int) -> set[int]:
    return {boundary for boundary in boundaries if 0 < boundary < token_count}


def map_predicted_positions_to_gold(
    gold_tokens: list[str], predicted_tokens: list[str]
) -> tuple[dict[int, int], list[tuple[str, int, int, int, int]]]:
    matcher = difflib.SequenceMatcher(
        a=gold_tokens,
        b=predicted_tokens,
        autojunk=False,
    )
    mapping: dict[int, int] = {0: 0}
    opcodes = matcher.get_opcodes()
    for tag, gold_start, gold_end, pred_start, pred_end in opcodes:
        if tag == "equal":
            for offset in range(gold_end - gold_start + 1):
                mapping[pred_start + offset] = gold_start + offset
        else:
            mapping[pred_start] = gold_start
            mapping[pred_end] = gold_end
    return mapping, opcodes


def score(
    gold: set[int], predicted: set[int], position_map: dict[int, int]
) -> dict[str, float | int]:
    mapped_predicted = {position_map[item] for item in predicted if item in position_map}
    matches = len(gold & mapped_predicted)
    denominator = len(gold) + len(predicted)
    return {
        "gold_boundaries": len(gold),
        "predicted_boundaries": len(predicted),
        "matched_boundaries": matches,
        "precision": matches / len(predicted) if predicted else 1.0,
        "recall": matches / len(gold) if gold else 1.0,
        "boundary_edit_rate": (denominator - 2 * matches) / denominator if denominator else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="比较 Stage 1 草案的 cue/意义群边界")
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--predicted", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-parenthetical", action="store_true")
    args = parser.parse_args()

    gold_tokens, gold_cues, gold_groups = parse(args.gold, not args.keep_parenthetical)
    pred_tokens, pred_cues, pred_groups = parse(args.predicted, not args.keep_parenthetical)
    # Gold may only cover the first half of a longer predicted draft. A small
    # tail allowance absorbs U.S. -> U / S and similarly local tokenization
    # differences without allowing the later programme to match by accident.
    comparison_pred_tokens = pred_tokens[: len(gold_tokens) + 64]
    position_map, opcodes = map_predicted_positions_to_gold(gold_tokens, comparison_pred_tokens)
    gold_limit = len(gold_tokens)
    trailing_insert_starts = [
        pred_start
        for tag, gold_start, gold_end, pred_start, _ in opcodes
        if tag == "insert" and gold_start == gold_end == gold_limit
    ]
    pred_limit = min(trailing_insert_starts) if trailing_insert_starts else len(comparison_pred_tokens)
    gold_cues_eval = clipped(gold_cues, gold_limit)
    gold_groups_eval = clipped(gold_groups, gold_limit)
    pred_cues_eval = clipped(pred_cues, pred_limit)
    pred_groups_eval = clipped(pred_groups, pred_limit)
    non_equal_tokens = sum(
        max(gold_end - gold_start, pred_end - pred_start)
        for tag, gold_start, gold_end, pred_start, pred_end in opcodes
        if tag != "equal"
    )
    result = {
        "schema_version": "substar.stage1.boundary-evaluation.v1",
        "gold": str(args.gold),
        "predicted": str(args.predicted),
        "gold_tokens": len(gold_tokens),
        "predicted_tokens": len(pred_tokens),
        "evaluated_gold_tokens": gold_limit,
        "evaluated_predicted_tokens": pred_limit,
        "token_alignment_non_equal_span": non_equal_tokens,
        "all_cue_boundaries": score(
            gold_cues_eval | gold_groups_eval,
            pred_cues_eval | pred_groups_eval,
            position_map,
        ),
        "within_group_cue_boundaries": score(gold_cues_eval, pred_cues_eval, position_map),
        "semantic_group_boundaries": score(gold_groups_eval, pred_groups_eval, position_map),
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
