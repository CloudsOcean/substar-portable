"""Per-Cue language routing. Original text and word clocks remain unchanged."""
from __future__ import annotations

import re
from typing import Any, Mapping

LANGUAGE_NAMES = {'zh-CN': '汉语', 'en': '英语', 'ja': '日语', 'ko': '韩语', 'th': '泰语', 'ar': '阿语', 'fr': '法语', 'de': '德语', 'es': '西语', 'pt': '葡语', 'ru': '俄语', 'it': '意语', 'vi': '越语', 'id': '印尼', 'ms': '马语', 'tl': '菲语', 'hi': '印语', 'nl': '荷语', 'sv': '瑞语', 'da': '丹语', 'fi': '芬语', 'no': '挪语', 'el': '希语', 'pl': '波语', 'cs': '捷语', 'hu': '匈语', 'ro': '罗语', 'bg': '保语', 'hr': '克语', 'sk': '斯语'}
LANGUAGES = set(LANGUAGE_NAMES)


def normalize_layout(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("语言行配置必须是对象")
    if not value.get("enabled"):
        return {}
    upper = list(dict.fromkeys(value.get("upper_languages", ["en", "ja", "ko"])))
    lower = list(dict.fromkeys(value.get("lower_languages", ["zh-CN"])))
    upper_target = str(value.get("upper_target", "en"))
    lower_target = str(value.get("lower_target", "zh-CN"))
    if not upper or not lower or not set(upper + lower) <= LANGUAGES:
        raise ValueError("上下行均须选择有效语言")
    if set(upper) & set(lower):
        raise ValueError("同一种语言不能同时归入上下行")
    if upper_target not in upper or lower_target not in lower:
        raise ValueError("每行的目标语言必须属于该行的语言列表")
    if "bidirectional" in value and (not isinstance(value["bidirectional"], bool) or len(lower) != 1):
        raise ValueError("目标语言只能选一种，双向翻译必须为布尔值")
    extra = {}
    if "source_languages" in value:
        sources = value["source_languages"]
        if not isinstance(sources, list) or any(code not in LANGUAGES for code in sources):
            raise ValueError("原始语言必须是有效语言列表")
        if len(set(sources)) > 4:
            raise ValueError("最多选择四种原始语言")
        auto = value.get("auto_detect", False)
        if not isinstance(auto, bool) or (not auto and not sources):
            raise ValueError("请选择自动识别或至少一种原始语言")
        extra = {"source_languages": list(dict.fromkeys(sources)), "auto_detect": auto}
    if "keep_dialect" in value:
        if not isinstance(value["keep_dialect"], bool):
            raise ValueError("保留方言表达必须为布尔值")
        extra["keep_dialect"] = value["keep_dialect"]
    return {**extra, "enabled": True, "upper_languages": upper, "lower_languages": lower,
            "upper_target": upper_target, "lower_target": lower_target,
            **({"bidirectional": value["bidirectional"]} if "bidirectional" in value else {})}


def cue_language(text: str) -> str:
    kana = bool(re.search(r"[\u3040-\u30ff\u31f0-\u31ff]", text))
    hangul = bool(re.search(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]", text))
    han = bool(re.search(r"[\u3400-\u9fff]", text))
    latin = bool(re.search(r"[A-Za-z]", text))
    scripts = [("ar", r"[؀-ۿݐ-ݿࢠ-ࣿ]"), ("th", r"[฀-๿]"),
               ("ru", r"[Ѐ-ԯ]"), ("el", r"[Ͱ-Ͽ]"), ("hi", r"[ऀ-ॿ]")]
    other = [code for code, pattern in scripts if re.search(pattern, text)]
    # Kana and Han are both native Japanese writing, not two languages.
    families = int(kana or han) + int(hangul) + int(latin) + len(other)
    if families > 1:
        return "mixed"
    return other[0] if other else "ja" if kana else "ko" if hangul else "zh-CN" if han else "en"


def route_text(text: str, layout: Mapping[str, Any]) -> dict[str, Any]:
    policy = normalize_layout(layout)
    language = cue_language(text)
    source_upper = language == "mixed" or language not in policy["lower_languages"]
    return {"source_language": language, "source_upper": source_upper,
            "target_language": policy["lower_target"] if source_upper or not policy.get("bidirectional", True) else policy["upper_target"]}


def source_is_upper(document, source: str) -> bool:
    policy = document.presentation.translation_layout
    if policy:
        return route_text(source, policy)["source_upper"]
    return document.presentation.display_order.value == "source_above_target"


def routing_prompt(layout: Mapping[str, Any]) -> str:
    policy = normalize_layout(layout)
    return ("# PER-CUE LANGUAGE ROUTING\n"
            f"Source languages: {', '.join(policy.get('source_languages', policy['upper_languages'])) or 'automatic detection'}. Single target language: {policy['lower_target']}. "
            f"Bidirectional translation: {policy.get('bidirectional', True)}. Reverse target: {policy['upper_target']}. "
            "When bidirectional translation is off and a Cue is already in the target language, return its original text unchanged; the application keeps it on the lower row without a duplicate translation. "
            "Each Cue has a TARGET_LANGUAGE directive. Translate its complete meaning into that language. "
            "Preserve original source text, names and brands. Never translate individual words into alternating languages. "
            "Do not merge Cues with different target languages. Mixed-language originals stay on the upper line; "
            f"their translation is always {policy['lower_target']}. "
            "The per-Cue directive overrides any global target-language instruction.\n")
