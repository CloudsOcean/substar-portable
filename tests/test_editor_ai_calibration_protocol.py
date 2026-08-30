import json
import unittest
from types import SimpleNamespace

from substar_core.editor.http_api import (
    _calibration_model_blocks,
    _revision_id,
    _validated_calibration_contract_actions,
)
from substar_core.editor import http_api


def action(**overrides):
    value = {
        "action_id": "a1",
        "kind": "set_case",
        "token_ids": ["t1"],
        "before_text": "russia",
        "after_text": "Russia",
        "confidence": "high",
        "evidence": [{"kind": "context", "reference": "country name"}],
        "disposition": "apply",
        "affects_translation": False,
    }
    value.update(overrides)
    return value


class EditorAiCalibrationProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token_map = {
            "t1": SimpleNamespace(text="russia"),
            "t2": SimpleNamespace(text="people"),
            "t3": SimpleNamespace(text="said"),
        }
        self.owned = list(self.token_map)
        self.token_to_cue = {token_id: "cue-1" for token_id in self.owned}

    def test_empty_actions_are_a_valid_noop(self) -> None:
        actions, rejected = _validated_calibration_contract_actions(
            {"actions": []}, self.owned, self.token_map, self.token_to_cue
        )
        self.assertEqual(actions, [])
        self.assertEqual(rejected, [])

    def test_high_confidence_evidenced_case_action_is_valid(self) -> None:
        actions, rejected = _validated_calibration_contract_actions(
            {"actions": [action()]}, self.owned, self.token_map, self.token_to_cue
        )
        self.assertEqual(actions[0]["after_text"], "Russia")
        self.assertEqual(rejected, [])

    def test_model_apply_disposition_is_authoritative_at_medium_confidence(self) -> None:
        actions, rejected = _validated_calibration_contract_actions(
            {"actions": [action(confidence="medium")]}, self.owned, self.token_map, self.token_to_cue
        )
        self.assertEqual(actions[0]["after_text"], "Russia")
        self.assertEqual(rejected, [])

    def test_span_must_preserve_tokens_and_ownership(self) -> None:
        actions, rejected = _validated_calibration_contract_actions(
            {"actions": [action(
                kind="replace_span",
                token_ids=["t1", "t2"],
                before_text="russia people",
                after_text="Russian people",
                affects_translation=True,
            )]},
            self.owned,
            self.token_map,
            self.token_to_cue,
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(rejected, [])

    def test_model_input_preserves_the_exact_punctuated_precondition(self) -> None:
        blocks = {
            "editor_0000": [{
                "cue_id": "cue-11",
                "editable": True,
                "tokens": [{"token_id": "t1", "text": "T,"}],
            }]
        }

        request = _calibration_model_blocks(blocks)

        self.assertEqual(request["editor_0000"][0]["tokens"][0]["text"], "T,")

    def test_lexical_replacement_retains_attached_punctuation(self) -> None:
        token_map = {"t1": SimpleNamespace(text="T,")}
        actions, rejected = _validated_calibration_contract_actions(
            {"actions": [action(
                kind="replace_token",
                before_text="T,",
                after_text="Tea,",
                affects_translation=True,
            )]},
            ["t1"],
            token_map,
            {"t1": "cue-1"},
        )

        self.assertEqual(actions[0]["after_text"], "Tea,")
        self.assertEqual(rejected, [])

    def test_chinese_light_punctuation_is_valid(self) -> None:
        token_map = {"t1": SimpleNamespace(text="结束")}
        actions, rejected = _validated_calibration_contract_actions(
            {"actions": [action(
                kind="set_punctuation",
                before_text="结束",
                after_text="结束。",
                affects_translation=False,
            )]},
            ["t1"],
            token_map,
            {"t1": "cue-1"},
        )

        self.assertEqual(actions[0]["after_text"], "结束。")
        self.assertEqual(rejected, [])

    def test_case_action_cannot_also_change_punctuation(self) -> None:
        actions, rejected = _validated_calibration_contract_actions(
            {"actions": [action(after_text="Russia,")]},
            self.owned,
            self.token_map,
            self.token_to_cue,
        )

        self.assertEqual(actions, [])
        self.assertIn("only case", rejected[0]["detail"])

    def test_punctuation_action_cannot_also_change_case(self) -> None:
        actions, rejected = _validated_calibration_contract_actions(
            {"actions": [action(
                kind="set_punctuation",
                before_text="russia",
                after_text="Russia.",
            )]},
            self.owned,
            self.token_map,
            self.token_to_cue,
        )

        self.assertEqual(actions, [])
        self.assertIn("only light punctuation", rejected[0]["detail"])

    def test_saved_revision_payload_id_is_read_as_a_mapping(self) -> None:
        self.assertEqual(_revision_id({"revision_id": "rev_after_save"}), "rev_after_save")


if __name__ == "__main__":
    unittest.main()


def test_calibration_task_projection_uses_runtime_progress() -> None:
    task = {
        "task_id": "tsk_" + "a" * 32,
        "task_type": "calibration",
        "state": "running",
        "progress": 0.42,
        "progress_message": "正在校准第 2 / 5 个语义块",
        "phase": "primary",
        "completed_units": 2,
        "total_units": 5,
        "error": None,
    }

    projected = http_api._runtime_ai_task_projection(task)

    assert projected["progress"] == 0.42
    assert projected["message"] == "正在校准第 2 / 5 个语义块"
    assert projected["ai_progress"]["units"]["completed"] == 2
    assert projected["kind"] == "calibration"


def test_translation_projection_uses_its_frozen_input() -> None:
    task = {
        "task_id": "tsk_" + "b" * 32,
        "task_type": "translation",
        "state": "running",
        "phase": "primary",
        "error": None,
    }

    projected = http_api._runtime_ai_task_projection(task, {
        "target_language": "zh-CN", "mapping_mode": "one_to_one"
    })
    assert projected["target_language"] == "zh-CN"
    assert projected["mapping_mode"] == "one_to_one"


def test_runtime_projection_does_not_synthesize_another_task_identity() -> None:
    task = {
        "task_id": "tsk_" + "c" * 32,
        "task_type": "calibration",
        "state": "queued",
        "phase": "primary",
    }
    assert http_api._runtime_ai_task_projection(task)["task_id"] == task["task_id"]
