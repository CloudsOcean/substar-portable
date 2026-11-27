from __future__ import annotations

from substar_core.cue_script import output_contract
from substar_core.prompt_registry import (
    calibration_variant,
    render_prompt,
    translation_variant,
)


def test_calibration_shares_one_template_across_languages() -> None:
    for language in ("zh-CN", "en", "ja", "ko", "mixed", "th"):
        assert calibration_variant(language) == "generic"
    prompt = render_prompt("calibration", variant="generic")
    repair = render_prompt("calibration_repair", variant="generic")
    assert prompt.files == ("production/calibration/common.md",)
    assert repair.files == (*prompt.files, "production/calibration/repair.md")
    assert "原文轨" in prompt.text and "译文轨" in prompt.text
    assert "即使没有改动，也要返回该行" in prompt.text
    assert "two-column calibration rows" in output_contract("CALIBRATE")


def test_mixed_source_translation_uses_explicit_target_routes() -> None:
    for target in ("zh-CN", "en", "ja", "ko"):
        route = translation_variant("mixed", target)
        assert route == f"target_{'zh' if target == 'zh-CN' else target}"
        for mode in ("one_to_one", "many_to_many"):
            prompt = render_prompt("contextual_translation", variant=route, mode=mode)
            repair = render_prompt(
                "contextual_translation_repair", variant=route, mode=mode
            )
            assert prompt.version == "2026-11-02.target-language.1"
            assert repair.version == "2026-11-02.target-language.1"
            assert f"production/translation/target/{'zh' if target == 'zh-CN' else target}.md" in prompt.files
            assert repair.files[-1] == prompt.files[-1]
            assert "编号|译文" in prompt.text
            assert "全部结构错误" in repair.text
            if mode == "many_to_many":
                assert "每个输入 cue 恰好覆盖一次" in prompt.text
                assert "2-4" in prompt.text


def test_mixed_segmentation_uses_each_language_rule_once() -> None:
    for key in ("semantic_grouping", "semantic_grouping_repair"):
        prompt = render_prompt(key, variant="zh+en+zh")
        assert prompt.files.count("production/segmentation/zh/rules.md") == 1
        assert prompt.files.count("production/segmentation/en/rules.md") == 1
        assert "production/segmentation/mixed/rules.md" not in prompt.files
