from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


QWEN_PROMPT_MAX_CHARACTERS = 400
QWEN_HOTWORD_MAX_COUNT = 2000
QWEN_SUPER_HOTWORD_MAX_COUNT = 50


def _valid_text_length(text: str) -> bool:
    if any(ord(char) > 127 for char in text):
        return len(text) <= 15
    return len(text.split()) <= 7


def normalize_qwen_hotwords(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        rows: Sequence[Any] = [
            {"text": text, "weight": weight} for text, weight in value.items()
        ]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        rows = value
    else:
        raise ValueError("临时热词必须是数组或词语到权重的映射")
    if len(rows) > QWEN_HOTWORD_MAX_COUNT:
        raise ValueError(f"临时热词最多 {QWEN_HOTWORD_MAX_COUNT} 个")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    super_count = 0
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise ValueError(f"第 {index} 个临时热词格式无效")
        text = str(row.get("text", "")).strip()
        if not text:
            raise ValueError(f"第 {index} 个临时热词不能为空")
        if not _valid_text_length(text):
            raise ValueError(f"热词“{text}”超过 Qwen 单词长度限制")
        try:
            weight = int(row.get("weight", 4))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"热词“{text}”的权重必须是整数") from exc
        if weight not in {1, 2, 3, 4, 5, 50}:
            raise ValueError(f"热词“{text}”的权重必须为 1–5 或 50")
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        if weight == 50:
            super_count += 1
            if super_count > QWEN_SUPER_HOTWORD_MAX_COUNT:
                raise ValueError(
                    f"权重 50 的超级热词最多 {QWEN_SUPER_HOTWORD_MAX_COUNT} 个"
                )
        result.append({"text": text, "weight": weight})
    return result


def qwen_hotword_mapping(value: Any) -> dict[str, int]:
    return {row["text"]: row["weight"] for row in normalize_qwen_hotwords(value)}


def prioritize_generated_qwen_hotwords(
    value: Any,
    *,
    user_prompt: str,
) -> list[dict[str, Any]]:
    """Give directly named terms super weight and inferred terms strong weight."""

    rows = normalize_qwen_hotwords(value)
    source = str(user_prompt or "").casefold()
    super_count = 0
    prioritized: list[dict[str, Any]] = []
    for item in rows:
        directly_named = item["text"].casefold() in source
        if directly_named and super_count < QWEN_SUPER_HOTWORD_MAX_COUNT:
            weight = 50
            super_count += 1
        else:
            weight = 5
        prioritized.append({"text": item["text"], "weight": weight})
    return prioritized
