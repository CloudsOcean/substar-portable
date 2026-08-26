from __future__ import annotations

import unittest

from substar_core.policy import (
    SubtitlePolicy,
    classify_language,
    count_visible_characters,
)
from substar_core.segmentation.material import validate_draft


class VisibleCharacterLimitTests(unittest.TestCase):
    def test_mixed_text_counts_each_character(self) -> None:
        self.assertEqual(count_visible_characters("data", count_spaces=True, count_punctuation=True), 4)
        self.assertEqual(count_visible_characters("data,", count_spaces=True, count_punctuation=True), 5)
        self.assertEqual(count_visible_characters("data test", count_spaces=True, count_punctuation=True), 9)
        self.assertEqual(
            count_visible_characters("\u4e2d\u6587data\u3002", count_spaces=True, count_punctuation=True),
            7,
        )

    def test_legacy_switches_do_not_change_universal_metric(self) -> None:
        self.assertEqual(
            count_visible_characters("data,", count_spaces=False, count_punctuation=False),
            5,
        )
        policy = SubtitlePolicy.from_settings(
            {
                "english_count_spaces": True,
                "english_count_punctuation": True,
            }
        )
        self.assertEqual(policy.line_length("\u4e2d\u6587data\u3002", "mixed_zh"), 7)

    def test_stage1_rejects_mixed_cue_using_visible_count(self) -> None:
        text = "\u4e2d\u6587data\u3002"
        report = validate_draft(
            text,
            text,
            source_language="zh-CN",
            chinese_hard_limit=6,
            english_count_spaces=True,
            english_count_punctuation=True,
        )
        self.assertFalse(report.to_dict()["valid"])
        self.assertTrue(
            any(error["code"] == "chinese_over_6" for error in report.errors)
        )

    def test_language_specific_hard_limits(self) -> None:
        policy = SubtitlePolicy(
            mixed_hard_limit=6,
            japanese_hard_limit=4,
            korean_hard_limit=5,
        )
        self.assertEqual(classify_language("\u4e2d\u6587data"), "mixed_en")
        self.assertEqual(classify_language("\u30c6\u30b9\u30c8"), "ja")
        self.assertEqual(classify_language("\ud14c\uc2a4\ud2b8"), "ko")
        self.assertEqual(policy.hard_limit("\u4e2d\u6587data"), 6)
        self.assertEqual(policy.hard_limit("\u30c6\u30b9\u30c8"), 4)
        self.assertEqual(policy.hard_limit("\ud14c\uc2a4\ud2b8"), 5)


if __name__ == "__main__":
    unittest.main()
