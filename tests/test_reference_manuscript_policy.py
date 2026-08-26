from __future__ import annotations

import unittest

from substar_core.manuscript_matching import (
    editor_reference_operations,
    materialize_reference_script,
)
from substar_core.segmentation.document_builder import (
    build_reference_script_document,
)
from substar_core.segmentation.worker import _reference_script_candidate


def _unit(index: int, text: str) -> dict[str, object]:
    start = index * 0.2
    return {
        "index": index,
        "text": text,
        "start": start,
        "end": start + 0.18,
        "speaker_id": "speaker_0",
    }


def _active_cue_text(document: object) -> str:
    tokens = {token.token_id: token for token in document.display_tokens}
    return "".join(
        tokens[token_id].text
        for cue in document.cues
        if cue.state.value == "active"
        for token_id in cue.display_token_ids
        if tokens[token_id].state.value == "active"
    )


def _active_cue_texts(document: object) -> list[str]:
    tokens = {token.token_id: token for token in document.display_tokens}
    return [
        "".join(
            tokens[token_id].text
            for token_id in cue.display_token_ids
            if tokens[token_id].state.value == "active"
        )
        for cue in document.cues
        if cue.state.value == "active"
    ]


class ReferenceManuscriptPolicyTests(unittest.TestCase):
    def test_reference_removes_asr_only_punctuation_and_marks_replacement(self) -> None:
        units = [
            _unit(0, "项"),
            _unit(1, "羽"),
            _unit(2, "杀"),
            _unit(3, "宋"),
            _unit(4, "义，"),
            _unit(5, "掌"),
        ]

        material, breaks, report = materialize_reference_script(
            "项羽杀宋义掌", units, "，。", "zh"
        )

        self.assertEqual(breaks, [])
        self.assertIn(
            {
                "source_index": 4,
                "reference_index": 4,
                "before": "义，",
                "after": "义",
                "lexical_match": True,
                "status": "applied",
            },
            report["replacements"],
        )
        document = build_reference_script_document(
            material,
            source_asset_id="asset-test",
            display_breaks=breaks,
            reference_report=report,
        )
        self.assertEqual(_active_cue_text(document), "项羽杀宋义掌")

    def test_asr_only_word_is_retained_without_its_boundary(self) -> None:
        units = [_unit(0, "甲"), _unit(1, "嗯，"), _unit(2, "乙。")] 

        material, breaks, report = materialize_reference_script(
            "甲乙。", units, "，。", "zh"
        )

        self.assertEqual(breaks, [])
        self.assertEqual(report["retained_asr_breaks"], [])
        self.assertIn(
            {
                "source_index": 1,
                "before": "嗯，",
                "reason": "reference_omitted",
            },
            report["retained_source"],
        )
        document = build_reference_script_document(
            material,
            source_asset_id="asset-test",
            display_breaks=breaks,
            reference_report=report,
        )
        self.assertEqual(_active_cue_text(document), "甲嗯，乙。")
        audit = next(
            change
            for change in document.changes
            if change.operation == "reference_manuscript_alignment"
        )
        self.assertTrue(
            any(
                item["type"] == "retained_source" and item["before"] == "嗯，"
                for item in audit.metadata["reference_changes"]
            )
        )

    def test_reference_only_word_is_active_and_marked_as_insertion(self) -> None:
        units = [_unit(0, "甲"), _unit(1, "乙。")]

        material, breaks, report = materialize_reference_script(
            "甲新乙。", units, "，。", "zh"
        )
        document = build_reference_script_document(
            material,
            source_asset_id="asset-test",
            display_breaks=breaks,
            reference_report=report,
        )

        self.assertEqual(_active_cue_text(document), "甲新乙。")
        audit = next(
            change
            for change in document.changes
            if change.operation == "reference_manuscript_alignment"
        )
        insertion = next(
            item
            for item in audit.metadata["reference_changes"]
            if item["type"] == "insert"
        )
        self.assertEqual(insertion["after"], "新")
        self.assertEqual(insertion["status"], "applied")

    def test_editor_rematch_marks_punctuation_removal(self) -> None:
        units = [
            {"index": 0, "text": "项"},
            {"index": 1, "text": "羽"},
            {"index": 2, "text": "杀"},
            {"index": 3, "text": "宋"},
            {"index": 4, "text": "义，"},
            {"index": 5, "text": "掌"},
        ]

        result = editor_reference_operations("项羽杀宋义掌", units, "zh")

        self.assertIn({"index": 4, "text": "义"}, result["edits"])
        self.assertTrue(
            any(
                item["type"] == "replace"
                and item["original"] == "义，"
                and item["text"] == "义"
                for item in result["reference_changes"]
            )
        )

    def test_reference_mode_does_not_apply_hard_limit_splitting(self) -> None:
        units = [_unit(index, text) for index, text in enumerate("甲乙丙丁戊")]
        material, breaks, report = materialize_reference_script(
            "甲乙丙丁戊", units, "，。", "zh"
        )

        document = build_reference_script_document(
            material,
            source_asset_id="asset-test",
            display_breaks=breaks,
            reference_report=report,
        )

        self.assertEqual(_active_cue_texts(document), ["甲乙丙丁戊"])
        self.assertEqual(_active_cue_text(document), "甲乙丙丁戊")
        self.assertFalse(
            any(
                change.operation == "reference_hard_limit_split"
                for change in document.changes
            )
        )

    def test_reference_candidate_ignores_hard_limit_constraints(self) -> None:
        units = [_unit(index, text) for index, text in enumerate("甲乙丙丁戊")]
        material, breaks, report = materialize_reference_script(
            "甲乙丙丁戊", units, "，。", "zh"
        )
        request = {
            "source_asset_id": "asset-test",
            "language": "zh",
            "input_fingerprint": "fingerprint",
            "mode": "reference_script",
            "transcription": {
                "task_id": "task-test",
                "input_fingerprint": "transcription-fingerprint",
                "media_sha256": "0" * 64,
            },
            "constraints": {
                "target_seconds": 90,
                "english_hard_limit": 55,
                "chinese_hard_limit": 3,
                "mixed_hard_limit": 3,
                "japanese_hard_limit": 3,
                "korean_hard_limit": 3,
                "reference_break_symbols": "，。",
            },
        }

        candidate, document, validation = _reference_script_candidate(
            request,
            material,
            {"reference_script_breaks": breaks},
            {"report": report},
        )

        self.assertEqual(len(candidate["cues"]), len(document.cues))
        self.assertEqual(validation["status"], "accepted")
        self.assertEqual(
            [item["alignment_end"] for item in candidate["cues"]],
            [4],
        )


if __name__ == "__main__":
    unittest.main()
