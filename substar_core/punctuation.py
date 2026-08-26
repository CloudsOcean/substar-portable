from __future__ import annotations

from typing import Any


PUNCTUATION_RULE_KEYS = (
    "top_remove",
    "top_space",
    "bottom_remove",
    "bottom_space",
)


def normalize_punctuation_rules(value: Any) -> dict[str, str]:
    raw = value if isinstance(value, dict) else {}
    result: dict[str, str] = {}
    for key in PUNCTUATION_RULE_KEYS:
        # Preserve entry order while removing duplicate characters. Whitespace
        # itself is never a selectable symbol.
        result[key] = "".join(
            dict.fromkeys(char for char in str(raw.get(key, "")) if not char.isspace())
        )
    for line in ("top", "bottom"):
        remove_key = f"{line}_remove"
        space_key = f"{line}_space"
        remove = set(result[remove_key])
        result[space_key] = "".join(
            char for char in result[space_key] if char not in remove
        )
    return result


def project_punctuation(text: str, rules: Any, line: str) -> str:
    if line not in {"top", "bottom"}:
        raise ValueError("line 必须是 top 或 bottom")
    normalized = normalize_punctuation_rules(rules)
    removed = set(normalized[f"{line}_remove"])
    spaced = set(normalized[f"{line}_space"])
    value = "".join(
        " " if char in spaced else "" if char in removed else char
        for char in str(text)
    )
    return " ".join(value.split())
