"""旧字符贪心基线。

仅用于和真实 03A1/03A2/03A3 流程做回归比较，不得作为生产成稿器。
生产入口见 scripts/run_stage1_pipeline.py。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def extract_master(material: str) -> str:
    match = re.search(
        r"^## MASTER_TRANSCRIPT\s*\r?\n\s*```text\s*\r?\n(.*?)\r?\n```",
        material,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise ValueError("找不到 MASTER_TRANSCRIPT text 代码块")
    return match.group(1).strip()


def split_sentences(text: str) -> list[str]:
    result: list[str] = []
    start = 0
    length = len(text)
    i = 0
    while i < length:
        char = text[i]
        if char not in ".?!。！？":
            i += 1
            continue

        # Consume ellipses as one terminator.
        end = i + 1
        while end < length and text[end] == char and char == ".":
            end += 1

        # Do not split decimal points or an isolated dot inside an identifier.
        prev_char = text[i - 1] if i else ""
        next_char = text[end] if end < length else ""
        if char == "." and prev_char.isdigit() and next_char.isdigit():
            i = end
            continue

        # Include a closing quote/bracket in the current sentence.
        while end < length and text[end] in "\"'”’）)]":
            end += 1

        # A sentence can be followed by whitespace, an uppercase letter, CJK,
        # or the end of the transcript. This also repairs ASR text such as
        # "room.Voila" where the space is missing.
        look = text[end] if end < length else ""
        is_cjk = bool(look and "\u3400" <= look <= "\u9fff")
        if end == length or look.isspace() or look.isupper() or is_cjk:
            piece = text[start:end].strip()
            if piece:
                result.append(piece)
            start = end
            while start < length and text[start].isspace():
                start += 1
            i = start
            continue
        i = end

    tail = text[start:].strip()
    if tail:
        result.append(tail)
    return result


def visible_length(value: str) -> int:
    return len(re.sub(r"\s+", " ", value).strip())


def english_count(value: str) -> int:
    return len(re.sub(r"\s+", "", value))


def han_count(value: str) -> int:
    return len(re.findall(r"[\u3400-\u9fff]", value))


def exceeds_hard_limit(value: str) -> bool:
    return english_count(value) > 55 or han_count(value) > 18


def display_normalize(value: str) -> str:
    result: list[str] = []
    for index, char in enumerate(value):
        previous = value[index - 1] if index else ""
        following = value[index + 1] if index + 1 < len(value) else ""
        if char == "." and previous.isdigit() and following.isdigit():
            result.append(char)
        elif char in {",", "，", "、"}:
            result.append(" ")
        elif char in {".", "。"}:
            continue
        else:
            result.append(char)
    return re.sub(r"\s+", " ", "".join(result)).strip()


def candidate_score(left: str, right: str) -> float:
    left_len = english_count(display_normalize(left))
    right_len = english_count(display_normalize(right))
    score = -max(0, left_len - 50) * 0.3
    if left.rstrip().endswith((",", ";", ":", "，", "；", "：")):
        score += 16
    first = (right.lstrip().split(maxsplit=1) or [""])[0].lower().strip("\"'“‘(")
    if first in {
        "and", "but", "because", "where", "who", "which", "while", "when",
        "so", "then", "if", "although", "though", "or", "with", "for",
    }:
        score += 11
    if left_len < 18:
        score -= (18 - left_len) * 3
    if right_len < 12:
        score -= (12 - right_len) * 3
    return score


def split_cues(sentence: str) -> list[str]:
    remaining = sentence.strip()
    cues: list[str] = []
    while exceeds_hard_limit(display_normalize(remaining)):
        candidates: list[tuple[float, int]] = []
        for match in re.finditer(r"\s+", remaining):
            cut = match.start()
            left = remaining[:cut].rstrip()
            right = remaining[match.end():].lstrip()
            left_display = display_normalize(left)
            if (
                not exceeds_hard_limit(left_display)
                and english_count(left_display) >= 10
                and english_count(display_normalize(right)) >= 5
            ):
                candidates.append((candidate_score(left, right), cut))

        # Mixed/Chinese passages may not contain usable spaces. Prefer visible
        # punctuation before falling back to a conservative character boundary.
        if not candidates:
            for match in re.finditer(r"[，；：、]", remaining):
                cut = match.end()
                left_display = display_normalize(remaining[:cut])
                if not exceeds_hard_limit(left_display) and han_count(left_display) >= 6:
                    candidates.append((20 - abs(18 - cut), cut))

        if candidates:
            _, cut = max(candidates, key=lambda item: item[0])
        else:
            legal: list[int] = []
            for possible in range(1, len(remaining)):
                if remaining[possible - 1].isascii() and remaining[possible].isascii():
                    if not (remaining[possible - 1].isspace() or remaining[possible].isspace()):
                        continue
                if not exceeds_hard_limit(display_normalize(remaining[:possible])):
                    legal.append(possible)
            if not legal:
                raise RuntimeError(f"找不到满足硬上限的合法边界：{remaining}")
            cut = max(legal)

        left = remaining[:cut].strip()
        right = remaining[cut:].strip()
        if not left or not right:
            break
        cues.append(display_normalize(left))
        remaining = right

    if remaining:
        cues.append(display_normalize(remaining))
    return cues


def build_draft(master: str) -> str:
    groups: list[str] = []
    for sentence in split_sentences(master):
        groups.append("\n".join(split_cues(sentence)))
    return "\n\n".join(groups).strip() + "\n"


def normalized_characters(value: str) -> str:
    return re.sub(r"\s+", "", value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("material", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    master = extract_master(args.material.read_text(encoding="utf-8-sig"))
    draft = build_draft(master)
    if normalized_characters(draft) != normalized_characters(display_normalize(master)):
        raise RuntimeError("覆盖校验失败：草案与主稿的显示标点归一化结果不一致")

    for line in (line for line in draft.splitlines() if line.strip()):
        if exceeds_hard_limit(line):
            raise RuntimeError(f"字符硬上限校验失败：{line}")
        if re.search(r"[，。,.、]", line):
            # Decimal points are already preserved by display_normalize and are
            # the only permitted period exception in this draft generator.
            if not re.search(r"\d\.\d", line):
                raise RuntimeError(f"下标点校验失败：{line}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(draft, encoding="utf-8")
    nonempty = [line for line in draft.splitlines() if line.strip()]
    groups = [part for part in re.split(r"\n\s*\n", draft.strip()) if part.strip()]
    print("mode=negative_baseline_only")
    print(f"output={args.output}")
    print(f"master_characters={len(normalized_characters(display_normalize(master)))}")
    print(f"cue_lines={len(nonempty)}")
    print(f"translation_groups={len(groups)}")
    print("coverage=1.0")


if __name__ == "__main__":
    main()
