from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

from .language_layout import format_text


LanguageClass = Literal[
    "zh", "en", "ja", "ko", "mixed_zh", "mixed_en", "neutral"
]


BASELINE_PUNCTUATION = frozenset(",，.。、")
RAISED_PUNCTUATION = frozenset(
    "\"'“”‘’()（）[]【】{}《》〈〉「」『』—–-?!？！"
)


@dataclass(frozen=True)
class SubtitlePolicy:
    display_order: str = "en_zh"
    top_raised_punctuation: str = "preserve"
    top_baseline_punctuation: str = "preserve"
    bottom_raised_punctuation: str = "preserve"
    bottom_baseline_punctuation: str = "normalize"
    english_hard_limit: int = 55
    english_count_spaces: bool = True
    english_count_punctuation: bool = True
    chinese_hard_limit: int = 24
    mixed_hard_limit: int = 25
    japanese_hard_limit: int = 25
    korean_hard_limit: int = 32
    target_visual_width_limit: int = 48
    minimum_cue_duration_ms: int = 400
    maximum_cue_duration_ms: int = 7000
    maximum_cps_latin: float = 20.0
    maximum_cps_cjk: float = 12.0
    tail_padding_ms: int = 120
    snap_threshold_ms: int = 500

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "SubtitlePolicy":
        values = {
            field: settings.get(field, getattr(cls(), field))
            for field in cls.__dataclass_fields__
        }
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        }

    def fingerprint(self) -> str:
        payload = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def english_length(self, text: str) -> int:
        return count_latin_characters(
            text,
            count_spaces=self.english_count_spaces,
            count_punctuation=self.english_count_punctuation,
        )

    def line_length(self, text: str, language: LanguageClass | None = None) -> int:
        return count_visible_characters(
            text,
            count_spaces=self.english_count_spaces,
            count_punctuation=self.english_count_punctuation,
        )

    def hard_limit(self, text: str, language: LanguageClass | None = None) -> int:
        language = language or classify_language(text)
        if str(language).lower() in {"mixed", "mixed_zh", "mixed_en"}:
            return self.mixed_hard_limit
        if language in {"zh", "mixed_zh"}:
            return self.chinese_hard_limit
        if language == "ja":
            return self.japanese_hard_limit
        if language == "ko":
            return self.korean_hard_limit
        return self.english_hard_limit

    def exceeds_hard_limit(
        self, text: str, language: LanguageClass | None = None
    ) -> bool:
        return self.line_length(text, language) > self.hard_limit(text, language)


def count_latin_characters(
    text: str,
    *,
    count_spaces: bool,
    count_punctuation: bool,
) -> int:
    return count_visible_characters(
        text,
        count_spaces=count_spaces,
        count_punctuation=count_punctuation,
    )


def count_visible_characters(
    text: str,
    *,
    count_spaces: bool,
    count_punctuation: bool,
) -> int:
    """Count the displayed Unicode characters used by every hard-limit path.

    A Cue is a displayed string, so every retained character follows one
    universal metric.  The legacy switches remain accepted by callers during
    this migration, but deliberately do not change the result.
    """
    value = format_text(str(text or ""))
    return len(value)


def classify_language(text: str) -> LanguageClass:
    han = len(re.findall(r"[\u3400-\u9fff]", text))
    kana = len(re.findall(r"[\u3040-\u30ff\u31f0-\u31ff]", text))
    hangul = len(re.findall(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    digits = len(re.findall(r"\d", text))
    if hangul:
        return "ko"
    if kana:
        return "ja"
    if not han and not latin:
        return "neutral" if digits or text.strip() else "neutral"
    if han and not latin:
        return "zh"
    if latin and not han:
        return "en"
    # Mixed lines keep their dominant linguistic track. A small acronym or
    # proper name must not flip an otherwise Chinese source line.
    return "mixed_zh" if han >= max(2, latin * 0.55) else "mixed_en"


def base_language(language: LanguageClass) -> str:
    if language in {"zh", "mixed_zh"}:
        return "zh-CN"
    if language == "ja":
        return "ja"
    if language == "ko":
        return "ko"
    if language in {"en", "mixed_en"}:
        return "en"
    return "neutral"


def track_lines(
    *,
    source: str,
    target: str,
    display_order: str,
    source_language: str | None = None,
) -> tuple[str, str]:
    language = source_language or base_language(classify_language(source))
    if language == "neutral":
        # Neutral items such as "14" do not establish a language direction.
        # Their paired target decides the track when possible.
        target_language = base_language(classify_language(target))
        language = "en" if target_language == "zh-CN" else "zh-CN"
    if display_order == "en_zh":
        return (target, source) if language == "zh-CN" else (source, target)
    if display_order == "zh_en":
        return (source, target) if language == "zh-CN" else (target, source)
    return source, target
