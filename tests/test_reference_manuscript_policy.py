from __future__ import annotations

import unittest

from substar_core.manuscript_matching import (
    editor_reference_operations,
    materialize_reference_alignment,
    materialize_reference_script,
    reference_tokens,
)
from substar_core.segmentation.input_contract import (
    build_segmentation_material,
    build_segmentation_material_with_display_projection,
    build_segmentation_material_with_reference_projection,
)
from substar_core.segmentation.document_builder import (
    apply_semantic_display_projection,
    attach_semantic_reference_audit,
    build_reference_script_document,
)
from substar_core.contracts.editor_document import (
    build_editor_document,
    source_tokens_from_asr,
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
    def test_ai_material_hides_semantic_punctuation_but_projects_it_onto_tokens(self) -> None:
        evidence = {
            "units": [
                _unit(0, "Hello,"),
                _unit(1, "world."),
                _unit(2, "TEUs ——"),
                _unit(3, "2,000"),
                _unit(4, "1.425"),
            ]
        }

        material, projection = build_segmentation_material_with_display_projection(
            "ignored", evidence
        )

        self.assertEqual(
            [unit["text"] for unit in material["units"]],
            ["Hello", "world", "TEUs", "2,000", "1.425"],
        )
        self.assertEqual(
            [unit["text"] for unit in projection],
            ["Hello,", "world.", "TEUs ——", "2,000", "1.425"],
        )

    def test_semantic_display_projection_restores_punctuation_without_moving_cues(self) -> None:
        evidence = {
            "units": [
                _unit(0, "Hello,"),
                _unit(1, "world."),
                _unit(2, "TEUs ——"),
                _unit(3, "2,000"),
            ]
        }
        material, projection = build_segmentation_material_with_display_projection(
            "ignored", evidence
        )
        source_tokens = source_tokens_from_asr(
            material["units"], source_asset_id="asset-test"
        )
        document = build_editor_document(
            source_tokens=source_tokens,
            source_kind="asr",
            source_asset_id="asset-test",
            execution_plan={"blocks": [], "boundaries_after": [], "skipped_reason": None},
            semantic_grouping={
                "protections": [],
                "meaning_groups": [],
                "review_regions": [],
            },
            cue_layout={"display_breaks": [1]},
        )
        cue_ids = [cue.cue_id for cue in document.cues]
        cue_times = [(cue.start, cue.end) for cue in document.cues]

        restored = apply_semantic_display_projection(document, projection)

        self.assertEqual(
            [token.text for token in restored.display_tokens],
            ["Hello,", "world.", "TEUs ——", "2,000"],
        )
        self.assertEqual([cue.cue_id for cue in restored.cues], cue_ids)
        self.assertEqual([(cue.start, cue.end) for cue in restored.cues], cue_times)
        audit = next(
            change
            for change in restored.changes
            if change.operation == "reference_manuscript_display_projection"
        )
        self.assertFalse(audit.metadata["cue_boundaries_changed"])

    def test_tokenizer_preserves_numeric_and_opaque_written_tokens(self) -> None:
        tokens = reference_tokens(
            "2,000 1.425 v1.2.3 12:30 U.S. test@example.com https://example.com/a",
            "en",
        )

        self.assertEqual(
            [token.lexical for token in tokens],
            [
                "2,000",
                "1.425",
                "v1.2.3",
                "12:30",
                "U.S.",
                "test@example.com",
                "https://example.com/a",
            ],
        )

    def test_numeric_commas_do_not_create_reference_or_asr_breaks(self) -> None:
        units = [
            _unit(0, "2,000"),
            _unit(1, "people"),
            _unit(2, "arrived."),
            _unit(3, "Today"),
        ]

        _material, breaks, report = materialize_reference_script(
            "2,000 people arrived. Today", units, ",.", "en"
        )

        self.assertEqual(breaks, [2])
        self.assertNotIn(0, report["asr_breaks"])

    def test_reference_alignment_keeps_numeric_token_for_ai_material(self) -> None:
        alignment = {
            "units": [
                _unit(0, "2000"),
                _unit(1, "kilometres"),
            ]
        }

        master, projected, _report = materialize_reference_alignment(
            "2,000 kilometres", alignment, "en"
        )
        material, _projection, suggestions = (
            build_segmentation_material_with_reference_projection(master, projected)
        )

        self.assertEqual(
            [unit["text"] for unit in material["units"]],
            ["2,000", "kilometres"],
        )

    def test_semantic_reference_differences_survive_into_editor_markers(self) -> None:
        alignment = {
            "units": [
                _unit(0, "kilometers"),
                _unit(1, "east"),
            ]
        }
        master, projected, report = materialize_reference_alignment(
            "kilometres — further east", alignment, "en"
        )
        material, _projection, suggestions = (
            build_segmentation_material_with_reference_projection(master, projected)
        )
        source_tokens = source_tokens_from_asr(
            material["units"], source_asset_id="asset-test"
        )
        document = build_editor_document(
            source_tokens=source_tokens,
            source_kind="asr",
            source_asset_id="asset-test",
            execution_plan={"blocks": [], "boundaries_after": [], "skipped_reason": None},
            semantic_grouping={
                "protections": [],
                "meaning_groups": [],
                "review_regions": [],
            },
            cue_layout={"display_breaks": []},
        )

        marked = attach_semantic_reference_audit(document, report, suggestions)
        audit = next(
            change
            for change in marked.changes
            if change.operation == "reference_manuscript_alignment"
        )
        changes = audit.metadata["reference_changes"]

        self.assertEqual(len(changes), 2)
        self.assertEqual({item["type"] for item in changes}, {"replace", "insert"})
        self.assertEqual(
            {item["after"] for item in changes}, {"kilometres", "further"}
        )
        active_ids = {
            token.token_id
            for token in marked.display_tokens
            if token.state.value == "active"
        }
        replacement = next(item for item in changes if item["type"] == "replace")
        insertion = next(item for item in changes if item["type"] == "insert")
        self.assertIn(replacement["token_ids"][0], active_ids)
        self.assertNotIn(insertion["token_ids"][0], active_ids)
        self.assertEqual(insertion["status"], "deleted")
        display_by_id = {token.token_id: token.text for token in marked.display_tokens}
        self.assertTrue(
            all(
                display_by_id[item["token_ids"][0]] == item["after"]
                for item in changes
            )
        )
        self.assertEqual(len(marked.cues), len(document.cues))

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

    def test_reference_only_word_is_deleted_and_marked_as_insertion(self) -> None:
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

        self.assertEqual(_active_cue_text(document), "甲乙。")
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
        self.assertEqual(insertion["status"], "deleted")
        token = next(
            token for token in document.display_tokens
            if token.token_id == insertion["token_ids"][0]
        )
        self.assertEqual(token.state.value, "deleted")

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
