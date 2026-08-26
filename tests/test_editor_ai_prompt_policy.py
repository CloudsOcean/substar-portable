from __future__ import annotations

from substar_core.prompt_registry import render_prompt


def test_english_calibration_prompt_requires_complete_exact_bound_scan() -> None:
    prompt = render_prompt("calibration", variant="en")

    assert prompt.version == "2026-08-19.1"
    assert "do not stop after finding the first issue" in prompt.text
    assert "including case and attached punctuation" in prompt.text
    assert 'before_text="T,"' in prompt.text
    assert "Do not assume the most frequent spelling is correct" in prompt.text
    assert '`"high"`, `"medium"`, or `"low"`' in prompt.text
    assert "Never return a numeric score" in prompt.text


def test_chinese_calibration_prompt_matches_the_same_safety_policy() -> None:
    prompt = render_prompt("calibration", variant="zh")

    assert prompt.version == "2026-08-19.1"
    assert "发现一个问题后也要继续检查" in prompt.text
    assert "包括大小写和附着标点" in prompt.text
    assert 'before_text="T,"' in prompt.text
    assert "不得因为某种写法出现次数最多就认定它正确" in prompt.text
    assert '`"high"`、`"medium"` 或 `"low"`' in prompt.text
    assert "不得返回数字分数" in prompt.text


def test_review_prompt_requires_exhaustive_but_precision_first_tracks() -> None:
    prompt = render_prompt("editor_review")

    assert prompt.version == "2026-08-19.1"
    assert "It is normal and valid for either issue array" in prompt.text
    assert "Precision is more important than producing a long issue list" in prompt.text
    assert "If any one is missing, do not report it" in prompt.text
    assert "your own analysis finds grammatically valid" in prompt.text
    assert "Required source review pass" in prompt.text
    assert "Required translation review pass" in prompt.text
    assert "did not reverse a consistency correction" in prompt.text
    assert "If no translation exists, return no translation issue" in prompt.text
    assert "description` and `evidence` in concise Simplified Chinese" in prompt.text
    assert "keep it in the language of the source or translation" in prompt.text
    assert "Keep entity boundaries exact" in prompt.text
    assert "must differ from its current text" in prompt.text
