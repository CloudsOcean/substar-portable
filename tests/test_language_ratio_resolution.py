from __future__ import annotations

from substar_core.prompt_registry import source_language_analysis, source_language_for_text
from substar_core.segmentation.contracts import resolve_segmentation_language


def test_other_languages_at_threshold_keep_the_primary_language() -> None:
    text = "a" * 80 + "中" * 20
    analysis = source_language_analysis(text, 20)

    assert analysis["primary_language"] == "en"
    assert analysis["other_language_ratio_percent"] == 20.0
    assert analysis["resolved_language"] == "en"


def test_other_languages_above_threshold_resolve_to_mixed() -> None:
    assert source_language_for_text("a" * 79 + "中" * 21, 20) == "mixed"


def test_every_non_primary_language_contributes_to_the_ratio() -> None:
    analysis = source_language_analysis("a" * 75 + "中" * 15 + "한" * 10, 20)

    assert analysis["language_character_counts"] == {
        "zh-CN": 15,
        "en": 75,
        "ja": 0,
        "ko": 10,
    }
    assert analysis["other_language_ratio_percent"] == 25.0
    assert analysis["resolved_language"] == "mixed"


def test_manual_language_bypasses_ratio_but_keeps_detection_audit() -> None:
    request = {
        "language": "zh",
        "mode": "semantic",
        "constraints": {
            "language_ratio_threshold_percent": 20,
            "english_hard_limit": 55,
            "chinese_hard_limit": 24,
            "japanese_hard_limit": 25,
            "korean_hard_limit": 32,
            "mixed_hard_limit": 25,
        },
    }

    resolution = resolve_segmentation_language(request, "English only")

    assert resolution["automatic"] is False
    assert resolution["detected_language"] == "en"
    assert resolution["resolved_language"] == "zh-CN"
    assert resolution["resolved_hard_limit"] == 24


def test_auto_reference_mode_uses_symbols_for_the_resolved_language() -> None:
    request = {
        "language": "Auto",
        "mode": "reference_script",
        "constraints": {
            "language_ratio_threshold_percent": 20,
            "english_hard_limit": 55,
            "chinese_hard_limit": 24,
            "japanese_hard_limit": 25,
            "korean_hard_limit": 32,
            "mixed_hard_limit": 25,
            "reference_break_symbols": "，。？！.?!",
        },
    }

    english = resolve_segmentation_language(request, "This is entirely English.")
    mixed = resolve_segmentation_language(request, "a" * 70 + "中" * 30)

    assert english["resolved_language"] == "en"
    assert english["resolved_reference_break_symbols"] == ".?!"
    assert mixed["resolved_language"] == "mixed"
    assert mixed["resolved_reference_break_symbols"] == "，。？！.?!"


def test_custom_reference_symbols_survive_auto_language_resolution() -> None:
    request = {
        "language": "Auto",
        "mode": "reference_script",
        "constraints": {
            "language_ratio_threshold_percent": 20,
            "english_hard_limit": 55,
            "chinese_hard_limit": 24,
            "japanese_hard_limit": 25,
            "korean_hard_limit": 32,
            "mixed_hard_limit": 25,
            "reference_break_symbols": ";!",
        },
    }

    resolution = resolve_segmentation_language(request, "English only")

    assert resolution["resolved_language"] == "en"
    assert resolution["resolved_reference_break_symbols"] == ";!"
