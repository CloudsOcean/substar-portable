from __future__ import annotations

from enum import Enum


TRANSLATION_RESULT_SCHEMA = "substar.translation-result.v2"


class TranslationTargetLanguage(str, Enum):
    CHINESE_SIMPLIFIED = "zh-CN"
    ENGLISH = "en"
    JAPANESE = "ja"
    KOREAN = "ko"

