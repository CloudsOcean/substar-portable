"""Character caps are selected from one displayed track, never a whole project."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Mapping, Any


def cue_language(text: str, hint: str = "") -> str:
    hint = str(hint or "").lower().replace("_", "-")
    scripts: set[str] = set()
    for char in text:
        if not unicodedata.category(char).startswith("L"):
            continue
        name = unicodedata.name(char, "")
        if "CJK" in name or "IDEOGRAPH" in name:
            scripts.add("zh")
        elif "HIRAGANA" in name or "KATAKANA" in name:
            scripts.add("ja")
        elif "HANGUL" in name:
            scripts.add("ko")
        elif "LATIN" in name:
            scripts.add("latin")
        else:
            # Keep distinct scripts distinct (e.g. Thai + Arabic is mixed).
            scripts.add("other:" + name.split(" ")[0])
    if "ja" in scripts or (hint.startswith("ja") and scripts == {"zh"}):
        scripts.discard("zh")
        scripts.add("ja")
    elif "ko" in scripts and "zh" in scripts:
        scripts.discard("zh")  # Hanja in Korean belongs to the same language.
    if len(scripts) > 1:
        return "mixed"
    if not scripts:
        return {"zh-cn": "zh"}.get(hint, hint) if hint in {"zh", "zh-cn", "en", "ja", "ko"} else "other"
    script = next(iter(scripts))
    if script == "latin":
        # Latin script cannot distinguish English from French, etc.; use the
        # explicit track language when available, without inventing certainty.
        return "en" if hint in {"", "auto", "automatic", "mixed", "mixed_en", "mixed_zh", "en", "zh", "zh-cn", "ja", "ko"} else "other"
    return "other" if script.startswith("other:") else script


@dataclass(frozen=True)
class CueLimits:
    chinese_hard_limit: int = 20
    japanese_hard_limit: int = 24
    korean_hard_limit: int = 28
    english_hard_limit: int = 55
    other_hard_limit: int = 55
    mixed_hard_limit: int = 25
    language_hint: str = ""

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any], hint: str = "") -> "CueLimits":
        return cls(**{key: int(settings.get(key, field.default)) for key, field in cls.__dataclass_fields__.items() if key != "language_hint"}, language_hint=hint)

    def for_text(self, text: str) -> int:
        key = {"zh": "chinese", "ja": "japanese", "ko": "korean", "en": "english", "other": "other", "mixed": "mixed"}[cue_language(text, self.language_hint)]
        return getattr(self, key + "_hard_limit")

    def __str__(self) -> str:
        return (f"中文={self.chinese_hard_limit}, 日文={self.japanese_hard_limit}, 韩文={self.korean_hard_limit}, "
                f"英文={self.english_hard_limit}, 其他={self.other_hard_limit}, 同一条字幕内混合语言={self.mixed_hard_limit}。"
                "逐条按实际文本语言选择；多选原文语言不代表每条字幕混合；数字和标点不构成另一种语言。")


def limit_for_text(limits: int | CueLimits, text: str) -> int:
    return limits.for_text(text) if isinstance(limits, CueLimits) else int(limits)
