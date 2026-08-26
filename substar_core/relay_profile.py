from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import APP_DATA_DIR


RELAY_PROFILE_FILE = APP_DATA_DIR / "relay_profile.json"

DEFAULT_RELAY_PROFILE: dict[str, Any] = {
    "schema_version": "substar.relay-profile.v1",
    "source_language": "auto",
    "target_language": "zh-CN",
    "top_line_role": "source",
    "bottom_line_role": "target",
    "top_raised_punctuation": "preserve",
    "top_baseline_punctuation": "preserve",
    "bottom_raised_punctuation": "preserve",
    "bottom_baseline_punctuation": "preserve",
    "english_hard_limit": 55,
    "english_count_spaces": True,
    "english_count_punctuation": True,
    "chinese_hard_limit": 24,
    "mixed_hard_limit": 25,
    "japanese_hard_limit": 25,
    "korean_hard_limit": 32,
    "minimum_cue_duration_ms": 400,
    "maximum_cue_duration_ms": 7000,
    "maximum_cps_latin": 20.0,
    "maximum_cps_cjk": 12.0,
    "audio_denoise_mode": "off",
    "text_cleanup_mode": "mark_conservative",
    "translation_style": "corporate_broadcast",
    "nm_alignment_enabled": True,
    "glossary_enabled": True,
    "translation_polish_enabled": True,
    "risk_review_enabled": True,
}


class RelayProfileError(ValueError):
    pass


def validate_relay_profile(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(payload or {})
    profile = {
        key: raw.get(key, value)
        for key, value in DEFAULT_RELAY_PROFILE.items()
    }
    profile["schema_version"] = "substar.relay-profile.v1"
    if profile["source_language"] not in {"auto", "en", "zh-CN", "ja", "ko", "mixed"}:
        raise RelayProfileError("源语种必须是自动、English 或简体中文")
    if profile["target_language"] not in {"en", "zh-CN", "ja", "ko"}:
        raise RelayProfileError("目标语种必须是 English 或简体中文")
    if (
        profile["source_language"] != "auto"
        and profile["source_language"] == profile["target_language"]
    ):
        raise RelayProfileError("源语种与目标语种不能相同")
    roles = {profile["top_line_role"], profile["bottom_line_role"]}
    if roles != {"source", "target"}:
        raise RelayProfileError("上下行必须各显示一次源语和目标语")
    for key in ("top_raised_punctuation", "bottom_raised_punctuation"):
        if profile[key] not in {"preserve", "remove"}:
            raise RelayProfileError(f"{key} 必须是 preserve 或 remove")
    for key in ("top_baseline_punctuation", "bottom_baseline_punctuation"):
        if profile[key] not in {"preserve", "normalize"}:
            raise RelayProfileError(f"{key} 必须是 preserve 或 normalize")
    for prefix, cap in (
        ("english", 200),
        ("chinese", 100),
        ("mixed", 200),
        ("japanese", 100),
        ("korean", 120),
    ):
        hard = int(profile[f"{prefix}_hard_limit"])
        if not 1 <= hard <= cap:
            raise RelayProfileError(f"{prefix} 字符硬上限必须在 1–{cap} 之间")
        profile[f"{prefix}_hard_limit"] = hard
    profile["minimum_cue_duration_ms"] = int(profile["minimum_cue_duration_ms"])
    profile["maximum_cue_duration_ms"] = int(profile["maximum_cue_duration_ms"])
    if not (
        0
        <= profile["minimum_cue_duration_ms"]
        < profile["maximum_cue_duration_ms"]
        <= 60000
    ):
        raise RelayProfileError("字幕时长设置无效")
    profile["maximum_cps_latin"] = float(profile["maximum_cps_latin"])
    profile["maximum_cps_cjk"] = float(profile["maximum_cps_cjk"])
    if not (
        1 <= profile["maximum_cps_latin"] <= 100
        and 1 <= profile["maximum_cps_cjk"] <= 100
    ):
        raise RelayProfileError("CPS 必须在 1–100 之间")
    if profile["audio_denoise_mode"] not in {"off", "light"}:
        raise RelayProfileError("降噪模式无效")
    if profile["text_cleanup_mode"] not in {
        "preserve",
        "mark_conservative",
        "remove_conservative",
    }:
        raise RelayProfileError("文本清理模式无效")
    if profile["translation_style"] not in {
        "corporate_broadcast",
        "literal_review",
        "concise_web",
    }:
        raise RelayProfileError("翻译风格无效")
    for key in (
        "english_count_spaces",
        "english_count_punctuation",
        "nm_alignment_enabled",
        "glossary_enabled",
        "translation_polish_enabled",
        "risk_review_enabled",
    ):
        profile[key] = bool(profile[key])
    return profile


def profile_sha256(profile: dict[str, Any]) -> str:
    payload = json.dumps(
        validate_relay_profile(profile),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_relay_profile() -> dict[str, Any]:
    if not RELAY_PROFILE_FILE.is_file():
        return dict(DEFAULT_RELAY_PROFILE)
    try:
        value = json.loads(RELAY_PROFILE_FILE.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RelayProfileError("接力配置顶层必须是对象")
        return validate_relay_profile(value)
    except (OSError, json.JSONDecodeError, RelayProfileError):
        return dict(DEFAULT_RELAY_PROFILE)


def save_relay_profile(payload: dict[str, Any]) -> dict[str, Any]:
    profile = validate_relay_profile(payload)
    RELAY_PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    RELAY_PROFILE_FILE.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return profile


def write_frozen_profile(path: Path, profile: dict[str, Any]) -> str:
    normalized = validate_relay_profile(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return profile_sha256(normalized)
