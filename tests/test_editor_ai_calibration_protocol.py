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


def test_calibration_task_projection_uses_detailed_progress(tmp_path, monkeypatch) -> None:
    detail_path = tmp_path / "calibration.json"
    detail_path.write_text(json.dumps({
        "task_id": "editor_ai_test",
        "progress": 0.42,
        "message": "正在校准第 2 / 5 个语义块",
        "error": "",
    }), encoding="utf-8")
    monkeypatch.setattr(http_api, "_editor_task_path", lambda *_args: detail_path)
    task = {
        "task_id": "editor_ai_test",
        "kind": "calibration",
        "state": "running",
        "started_at": "2000-01-01T00:00:00+00:00",
        "error": None,
    }

    projected = http_api._project_editor_ai_task_payload("project", task)

    assert projected is not None
    assert projected["progress"] == 0.42
    assert projected["message"] == "正在校准第 2 / 5 个语义块"
    assert projected["elapsed_seconds"] > 0


def test_translation_lock_does_not_claim_calibration_progress_file(tmp_path, monkeypatch) -> None:
    detail_path = tmp_path / "calibration.json"
    detail_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(http_api, "_editor_task_path", lambda *_args: detail_path)
    task = {
        "task_id": "editor_ai_translation",
        "kind": "translation",
        "state": "running",
        "error": None,
    }

    assert http_api._project_editor_ai_task_payload("project", task) == task


def test_new_calibration_task_does_not_inherit_previous_task_timestamp(
    tmp_path, monkeypatch
) -> None:
    detail_path = tmp_path / "calibration.json"
    detail_path.write_text(json.dumps({
        "task_id": "old-task",
        "created_at": "2000-01-01T00:00:00+00:00",
    }), encoding="utf-8")
    monkeypatch.setattr(http_api, "_editor_task_path", lambda *_args: detail_path)

    value = http_api._write_editor_task(
        "project", "calibration", status="running", progress=0.02,
        message="准备中", task_id="new-task",
    )

    assert value["task_id"] == "new-task"
    assert value["created_at"] != "2000-01-01T00:00:00+00:00"
