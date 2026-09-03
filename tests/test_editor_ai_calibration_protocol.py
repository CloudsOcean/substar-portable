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

    def test_case_only_replace_token_is_normalized_to_set_case(self) -> None:
        actions, rejected = _validated_calibration_contract_actions(
            {"actions": [action(
                kind="replace_token",
                before_text="russia",
                after_text="Russia",
                affects_translation=True,
            )]},
            self.owned,
            self.token_map,
            self.token_to_cue,
        )

        self.assertEqual(actions[0]["kind"], "set_case")
        self.assertFalse(actions[0]["affects_translation"])
        self.assertEqual(rejected, [])

    def test_glued_token_split_is_preserved_for_review_not_applied(self) -> None:
        token_map = {"t1": SimpleNamespace(text="america,well")}
        actions, rejected = _validated_calibration_contract_actions(
            {"actions": [action(
                kind="replace_token",
                before_text="america,well",
                after_text="America. Well",
                affects_translation=True,
            )]},
            ["t1"],
            token_map,
            {"t1": "cue-1"},
        )

        self.assertEqual(actions[0]["after_text"], "America. Well")
        self.assertEqual(actions[0]["disposition"], "review")
        self.assertEqual(rejected, [])

    def test_merge_span_derives_translation_invalidation(self) -> None:
        token_map = {
            "t1": SimpleNamespace(text="U"),
            "t2": SimpleNamespace(text="s"),
        }
        actions, rejected = _validated_calibration_contract_actions(
            {"actions": [action(
                kind="merge_span",
                token_ids=["t1", "t2"],
                before_text="U s",
                after_text="U.S.",
                affects_translation=False,
            )]},
            ["t1", "t2"],
            token_map,
            {"t1": "cue-1", "t2": "cue-1"},
        )

        self.assertTrue(actions[0]["affects_translation"])
        self.assertEqual(rejected, [])

    def test_non_conserving_applied_merge_is_downgraded_to_review(self) -> None:
        token_map = {
            "t1": SimpleNamespace(text="us"),
            "t2": SimpleNamespace(text="state"),
        }
        actions, rejected = _validated_calibration_contract_actions(
            {"actions": [action(
                kind="merge_span",
                token_ids=["t1", "t2"],
                before_text="us state",
                after_text="U.S.",
                disposition="apply",
                affects_translation=True,
            )]},
            ["t1", "t2"],
            token_map,
            {"t1": "cue-1", "t2": "cue-1"},
        )

        self.assertEqual(actions[0]["kind"], "merge_span")
        self.assertEqual(actions[0]["disposition"], "review")
        self.assertEqual(rejected, [])

    def test_same_cue_many_to_one_replace_span_is_normalized_to_merge(self) -> None:
        token_map = {
            "t1": SimpleNamespace(text="Al"),
            "t2": SimpleNamespace(text="jalani"),
        }
        actions, rejected = _validated_calibration_contract_actions(
            {"actions": [action(
                kind="replace_span",
                token_ids=["t1", "t2"],
                before_text="Al jalani",
                after_text="al-Julani",
                disposition="review",
                affects_translation=True,
            )]},
            ["t1", "t2"],
            token_map,
            {"t1": "cue-1", "t2": "cue-1"},
        )

        self.assertEqual(actions[0]["kind"], "merge_span")
        self.assertEqual(actions[0]["disposition"], "review")
        self.assertTrue(actions[0]["affects_translation"])
        self.assertEqual(rejected, [])

    def test_non_materialized_review_span_can_change_token_count(self) -> None:
        actions, rejected = _validated_calibration_contract_actions(
            {"actions": [action(
                kind="replace_span",
                token_ids=["t1", "t2"],
                before_text="russia people",
                after_text="the Russian people",
                disposition="review",
                affects_translation=True,
            )]},
            self.owned,
            self.token_map,
            self.token_to_cue,
        )

        self.assertEqual(actions[0]["disposition"], "review")
        self.assertEqual(actions[0]["kind"], "replace_span")
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

    def test_same_token_can_receive_ordered_case_then_punctuation_actions(self) -> None:
        actions, rejected = _validated_calibration_contract_actions(
            {"actions": [
                action(action_id="case", after_text="Russia"),
                action(
                    action_id="punctuation",
                    kind="set_punctuation",
                    before_text="Russia",
                    after_text="Russia.",
                ),
            ]},
            self.owned,
            self.token_map,
            self.token_to_cue,
        )

        self.assertEqual([row["action_id"] for row in actions], ["case", "punctuation"])
        self.assertEqual(rejected, [])

    def test_later_action_must_bind_to_the_prior_action_result(self) -> None:
        actions, rejected = _validated_calibration_contract_actions(
            {"actions": [
                action(action_id="case", after_text="Russia"),
                action(
                    action_id="punctuation",
                    kind="set_punctuation",
                    before_text="russia",
                    after_text="russia.",
                ),
            ]},
            self.owned,
            self.token_map,
            self.token_to_cue,
        )

        self.assertEqual([row["action_id"] for row in actions], ["case"])
        self.assertIn("bound source tokens", rejected[0]["detail"])

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


def test_terminal_projection_prefers_final_result_progress() -> None:
    task = {
        "task_id": "tsk_" + "d" * 32,
        "task_type": "translation",
        "state": "succeeded_with_issues",
        "phase": "delivery",
        "progress_payload": {"phase": "completed", "problem_count": 0},
        "result": {
            "problem_cue_ids": ["cue-1"],
            "ai_progress": {"phase": "completed", "problem_count": 1},
        },
    }

    projected = http_api._runtime_ai_task_projection(task)

    assert projected["ai_progress"]["problem_count"] == 1
