from __future__ import annotations

from substar_core.cue_script import output_contract
from substar_core.prompt_registry import (
    calibration_variant,
    render_prompt,
    translation_variant,
)


def test_english_calibration_prompt_requires_complete_exact_bound_scan() -> None:
    prompt = render_prompt("calibration", variant="en")

    assert prompt.version == "2026-09-06.17"
    assert "Inspect every OWN row" in prompt.text
    assert "Return the complete corrected source-language text" in prompt.text
    assert "finalizer" in prompt.text
    assert "two-column calibration rows" in output_contract("CALIBRATE")


def test_chinese_calibration_prompt_matches_the_same_safety_policy() -> None:
    prompt = render_prompt("calibration", variant="zh")

    assert prompt.version == "2026-09-06.17"
    assert "先跨行重建完整的句子和话语结构" in prompt.text
    assert "完整源语言文本" in prompt.text
    assert "词元对齐分隔符" in prompt.text
    assert "finalizer" in prompt.text
    assert "two-column calibration rows" in output_contract("CALIBRATE")


def test_every_supported_source_language_has_a_dedicated_calibration_route() -> None:
    expected = {
        "zh-CN": ("zh", "先跨行重建完整的句子和话语结构"),
        "en": ("en", "Inspect every OWN row"),
        "ja": ("ja", "文と談話の構造を復元"),
        "ko": ("ko", "문장과 담화 구조를 복원"),
        "mixed": ("mixed", "including code switches"),
    }
    for language, (variant, marker) in expected.items():
        assert calibration_variant(language) == variant
        primary = render_prompt("calibration", variant=variant)
        repair = render_prompt("calibration_repair", variant=variant)
        assert primary.version == "2026-09-06.17"
        assert repair.version == "2026-09-06.17"
        assert marker in primary.text
        assert marker in repair.text
        assert "production/calibration/repair.md" in repair.files


def test_all_calibration_languages_share_projection_and_alignment_safety() -> None:
    expected = {
        "en": ("reconstruct the sentence and discourse structure", "token-alignment separators"),
        "zh": ("先跨行重建完整的句子和话语结构", "词元对齐分隔符"),
        "ja": ("文と談話の構造を復元", "トークン整列用の区切り"),
        "ko": ("문장과 담화 구조를 복원", "토큰 정렬 구분자"),
        "mixed": ("including code switches", "token-alignment separators"),
    }
    for variant, markers in expected.items():
        prompt = render_prompt("calibration", variant=variant)
        assert prompt.version == "2026-09-06.17"
        assert all(marker in prompt.text for marker in markers)
        assert "1|" in prompt.text
        assert "finalizer" in prompt.text


def test_mixed_source_translation_uses_explicit_target_routes() -> None:
    for target in ("zh-CN", "en", "ja", "ko"):
        route = translation_variant("mixed", target)
        assert route == f"mixed_to_{'zh' if target == 'zh-CN' else target}"
        for mode in ("one_to_one", "many_to_many"):
            prompt = render_prompt("contextual_translation", variant=route, mode=mode)
            repair = render_prompt(
                "contextual_translation_repair", variant=route, mode=mode
            )
            assert prompt.version == "2026-09-15.2"
            assert repair.version == "2026-09-15.2"
            assert "production/translation/mixed/rules.md" in prompt.files
            assert "production/translation/mixed/rules.md" in repair.files
            assert "编号|译文" in prompt.text
            assert "全部结构错误" in repair.text
            if mode == "many_to_many":
                assert "每个输入 cue 恰好覆盖一次" in prompt.text
                assert "2-4" in prompt.text


def test_mixed_segmentation_has_rules_cases_and_repair_route() -> None:
    primary = render_prompt("semantic_grouping", variant="mixed")
    repair = render_prompt("semantic_grouping_repair", variant="mixed")
    assert primary.version == "2026-09-06.17"
    assert primary.files == (
        "production/segmentation/common/semantic_grouping.md",
        "production/segmentation/mixed/rules.md",
        "production/cases/semantic_grouping_mixed.constructed.md",
    )
    assert repair.files == (
        "production/segmentation/common/semantic_grouping_repair.md",
        "production/segmentation/mixed/rules.md",
    )
