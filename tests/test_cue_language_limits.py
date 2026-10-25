import pytest

from substar_core.cue_limits import CueLimits, cue_language
from substar_core.policy import SubtitlePolicy
from substar_core.editor.translation.contextual import warning_report
from scripts.run_semantic_segmentation import semantic_grouping_overflow_issues, semantic_grouping_binding
from substar_core.segmentation.material import AlignmentUnit


@pytest.mark.parametrize("text,hint,expected", [
    ("今天使用 ChatGPT", "zh", "mixed"),
    ("这是一段很长很长很长的中文A", "zh", "mixed"),
    ("English only 123!", "mixed", "en"),
    ("今天是2026年。", "mixed", "zh"),
    ("日本語の字幕", "", "ja"),
    ("漢字", "ja", "ja"),
    ("한국어 字幕", "ko", "ko"),
    ("日本語 ChatGPT", "ja", "mixed"),
    ("한국어 OpenAI", "ko", "mixed"),
    ("ภาษาไทย", "Auto", "other"),
    ("مرحبا بالعالم", "Auto", "other"),
    ("Bonjour le monde", "fr", "other"),
    ("Привет мир", "Auto", "other"),
    ("ภาษาไทย hello", "th", "mixed"),
])
def test_one_track_language(text, hint, expected):
    assert cue_language(text, hint) == expected


def test_selected_mixed_project_keeps_individual_language_caps():
    limits = CueLimits(english_hard_limit=55, chinese_hard_limit=24, mixed_hard_limit=25, other_hard_limit=60, language_hint="mixed")
    assert limits.for_text("An entirely English subtitle") == 55
    assert limits.for_text("单独的中文字幕") == 24
    assert limits.for_text("今天使用 ChatGPT") == 25
    assert limits.for_text("ภาษาไทย") == 60
    assert SubtitlePolicy(other_hard_limit=61).hard_limit("Bonjour", "fr") == 61


def test_translation_checks_each_track_not_bilingual_pair():
    settings = {"english_hard_limit": 55, "chinese_hard_limit": 24, "mixed_hard_limit": 10, "other_hard_limit": 55}
    assert warning_report({"en": "An ordinary English subtitle.", "zh": "独立的中文翻译"}, settings, {"en": "en", "zh": "zh-CN"}) == []
    report = warning_report({"zh": "今天我们开始使用 ChatGPT"}, settings, {"zh": "zh-CN"})
    assert report[0]["limit"] == 10


def test_semantic_validation_uses_each_cue_language():
    units = [AlignmentUnit(index=i, start=i, end=i + .8, text=text) for i, text in enumerate([
        "This English line is longer than twenty five.", "独立的中文字幕", "今天我们开始使用 ChatGPT"])]
    _, binding = semantic_grouping_binding(units, 0, 2, 1, sentence_boundary_policy="unpunctuated")
    value = {"schema_version": "substar.semantic-grouping-result.v1", **binding,
             "meaning_groups": [{"alignment_start": 0, "alignment_end": 2, "line_breaks_after": [0, 1, 2]}], "exceptions": []}
    issues = semantic_grouping_overflow_issues(value, units, (0, 2), 1, binding,
        CueLimits(english_hard_limit=55, chinese_hard_limit=24, mixed_hard_limit=10, language_hint="mixed"))
    assert len(issues) == 1
    assert issues[0]["cue_start"] == 2
    assert issues[0]["hard_limit"] == 10


def test_wire_max_end_does_not_apply_mixed_cap_to_english():
    from substar_core.cue_script import render_segmentation_request
    limits = CueLimits(english_hard_limit=55, mixed_hard_limit=10)
    _, ledger = render_segmentation_request({"active_output_profile": {"hard_limit": 10, "cue_limits": vars(limits)},
        "rows": [{"index": 0, "text": "This is English"}, {"index": 1, "text": "with more words"}]})
    assert ledger.max_end_by_alias["W0001"] == "W0002"
