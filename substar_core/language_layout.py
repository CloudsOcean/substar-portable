from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable


_HAN = r"\u3400-\u9fff\uf900-\ufaff"
_KANA = r"\u3040-\u30ff\u31f0-\u31ff"
_HANGUL = r"\u1100-\u11ff\u3130-\u318f\uac00-\ud7af"
_CJK = _HAN + _KANA
_CHARACTER_BASED = _HAN + _KANA + _HANGUL


def normalize_language(language: str | None) -> str:
    value = str(language or "").strip().lower().replace("_", "-")
    if value.startswith("zh"):
        return "zh"
    if value.startswith("ja"):
        return "ja"
    if value.startswith("ko"):
        return "ko"
    if value.startswith("en"):
        return "en"
    if value.startswith("mixed"):
        return "mixed"
    return ""


def detect_language(text: str, fallback: str = "en") -> str:
    value = str(text or "")
    if re.search(f"[{_KANA}]", value):
        return "ja"
    if re.search(f"[{_HANGUL}]", value):
        return "ko"
    if re.search(f"[{_HAN}]", value) and re.search(r"[A-Za-z]", value):
        return "mixed"
    if re.search(f"[{_HAN}]", value):
        return "zh"
    return normalize_language(fallback) or "en"


def format_text(text: str, language: str | None = None) -> str:
    """Apply display spacing without changing stored tokens or their timing."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    resolved = normalize_language(language) or detect_language(value)
    if resolved in {"zh", "ja", "mixed"}:
        value = re.sub(f"([{_CJK}])\s+(?=[{_CJK}])", r"\1", value)
    return value


def layout_tokens(tokens: Iterable[str], language: str | None = None) -> str:
    values = [str(token).strip() for token in tokens if str(token).strip()]
    resolved = normalize_language(language) or detect_language("".join(values))
    if resolved not in {"zh", "ja", "mixed"}:
        return format_text(" ".join(values), resolved)
    output = values[0] if values else ""
    for value in values[1:]:
        previous = next(
            (char for char in reversed(output) if not _is_punctuation_or_symbol(char) and not char.isspace()),
            "",
        )
        current = next(
            (char for char in value if not _is_punctuation_or_symbol(char) and not char.isspace()),
            "",
        )
        starts_with_punctuation = bool(value and _is_punctuation_or_symbol(value[0]))
        previous_is_cjk = bool(previous and re.fullmatch(f"[{_CJK}]", previous))
        current_is_cjk = bool(current and re.fullmatch(f"[{_CJK}]", current))
        separator = "" if starts_with_punctuation or (previous_is_cjk and current_is_cjk) else " "
        output += separator + value
    return format_text(output, resolved)


def _is_punctuation_or_symbol(value: str) -> bool:
    return bool(value) and unicodedata.category(value).startswith(("P", "S"))


def character_count(text: str, language: str | None = None) -> int:
    value = format_text(text, language)
    return len(value)


def editor_token_fragments(text: str) -> list[str]:
    """Project a provider token to editor-safe language units.

    Han, kana and Hangul are character-addressable in the editor. Alphabetic,
    numeric and other word-level runs stay intact so their virtual line keeps
    word gaps and never exposes accidental mid-word Cue boundaries.
    """

    fragments: list[str] = []
    word: list[str] = []

    def flush() -> None:
        if word:
            fragments.append("".join(word))
            word.clear()

    for char in str(text or "").strip():
        if char.isspace():
            flush()
        elif re.fullmatch(f"[{_CHARACTER_BASED}]", char):
            flush()
            fragments.append(char)
        else:
            word.append(char)
    flush()
    return [fragment for fragment in fragments if fragment]
