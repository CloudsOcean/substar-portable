from __future__ import annotations

from substar_core.cue_script import output_contract
from substar_core.prompt_registry import (
    calibration_variant,
    render_prompt,
    translation_variant,
)


def test_english_calibration_prompt_requires_complete_exact_bound_scan() -> None:
    prompt = render_prompt("calibration", variant="en")

    assert prompt.version == "2026-09-04.3"
    assert "Inspect every OWN Cue" in prompt.text
    assert "Return the complete corrected source-language text" in prompt.text
    assert "finalizer" in prompt.text
    assert "JSON" in output_contract("CALIBRATE")


def test_chinese_calibration_prompt_matches_the_same_safety_policy() -> None:
    prompt = render_prompt("calibration", variant="zh")

    assert prompt.version == "2026-09-04.3"
    assert "检查按时间排列的每一个 OWN Cue" in prompt.text
    assert "完整源语言文本" in prompt.text
    assert "finalizer" in prompt.text
    assert "JSON" in output_contract("CALIBRATE")


def test_every_supported_source_language_has_a_dedicated_calibration_route() -> None:
    expected = {
        "zh-CN": ("zh", "检查按时间排列的每一个 OWN Cue"),
        "en": ("en", "Inspect every OWN Cue"),
        "ja": ("ja", "各 OWN Cue を確認する"),
        "ko": ("ko", "모든 OWN Cue를 검사한다"),
        "mixed": ("mixed", "mixed-language source block"),
    }
    for language, (variant, marker) in expected.items():
        assert calibration_variant(language) == variant
        primary = render_prompt("calibration", variant=variant)
        repair = render_prompt("calibration_repair", variant=variant)
        assert primary.version == "2026-09-04.3"
        assert repair.version == "2026-09-04.3"
        assert marker in primary.text
        assert marker in repair.text
        assert "production/calibration/repair.md" in repair.files


def test_mixed_source_translation_uses_explicit_target_routes() -> None:
    for target in ("zh-CN", "en", "ja", "ko"):
        route = translation_variant("mixed", target)
        assert route == f"mixed_to_{'zh' if target == 'zh-CN' else target}"
        for mode in ("one_to_one", "many_to_many"):
            prompt = render_prompt("contextual_translation", variant=route, mode=mode)
            repair = render_prompt(
                "contextual_translation_repair", variant=route, mode=mode
            )
            assert prompt.version == "2026-09-04.4"
            assert repair.version == "2026-09-04.4"
            assert "production/translation/mixed/rules.md" in prompt.files
            assert "production/translation/mixed/rules.md" in repair.files


def test_mixed_segmentation_has_rules_cases_and_repair_route() -> None:
    primary = render_prompt("semantic_grouping", variant="mixed")
    repair = render_prompt("semantic_grouping_repair", variant="mixed")
    assert primary.version == "2026-09-04.3"
    assert primary.files == (
        "production/segmentation/common/semantic_grouping.md",
        "production/segmentation/mixed/rules.md",
        "production/cases/semantic_grouping_mixed.constructed.md",
    )
    assert repair.files == (
        "production/segmentation/common/semantic_grouping_repair.md",
        "production/segmentation/mixed/rules.md",
    )
