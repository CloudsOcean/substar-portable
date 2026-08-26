from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from substar_core.editor.calibration import (
    CALIBRATION_RESULT_SCHEMA,
    CalibrationActionKind,
)
from substar_core.editor.tasks import (
    EDITOR_AI_TASK_SCHEMA,
    EditorAiTaskState,
    EditorAiTaskStateError,
    editor_ai_task_holds_lock,
    require_editor_ai_task_transition,
)
from substar_core.editor.review import (
    SOURCE_REVIEW_RESULT_SCHEMA,
    TRANSLATION_REVIEW_RESULT_SCHEMA,
    SourceReviewIssueType,
    TranslationReviewIssueType,
)
from substar_core.editor.translation import TRANSLATION_RESULT_SCHEMA
from substar_core.segmentation.semantic_grouping_contract import (
    SEMANTIC_GROUPING_RESULT_SCHEMA,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"


def load_schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def validate(name: str, value: dict) -> None:
    Draft202012Validator(
        load_schema(name), format_checker=FormatChecker()
    ).validate(value)


class EditorAiContractTests(unittest.TestCase):
    def test_all_new_schemas_are_valid_draft_2020_12(self) -> None:
        for name in (
            "semantic-grouping-result.v1.schema.json",
            "calibration-result.v1.schema.json",
            "translation-result.v1.schema.json",
            "source-review-result.v1.schema.json",
            "translation-review-result.v1.schema.json",
            "editor-ai-task.v1.schema.json",
        ):
            Draft202012Validator.check_schema(load_schema(name))

    def test_semantic_grouping_contract_has_no_text_correction_channel(self) -> None:
        value = {
            "schema_version": SEMANTIC_GROUPING_RESULT_SCHEMA,
            "input_fingerprint": "a" * 64,
            "block_id": "c0001",
            "ownership": {"alignment_start": 0, "alignment_end": 9},
            "meaning_groups": [
                {
                    "alignment_start": 0,
                    "alignment_end": 9,
                    "line_breaks_after": [4, 9],
                }
            ],
            "exceptions": [],
        }
        validate("semantic-grouping-result.v1.schema.json", value)
        with self.assertRaises(ValidationError):
            validate(
                "semantic-grouping-result.v1.schema.json",
                {**value, "ai_calibrations": []},
            )

    def test_calibration_contract_routes_exact_actions(self) -> None:
        value = {
            "schema_version": CALIBRATION_RESULT_SCHEMA,
            "task_id": "task-calibration",
            "project_id": "project-1",
            "based_on_revision_id": "revision-1",
            "actions": [
                {
                    "action_id": "action-1",
                    "kind": CalibrationActionKind.REPLACE_SPAN.value,
                    "token_ids": ["token-1", "token-2"],
                    "before_text": "molly t",
                    "after_text": "Molly Tea",
                    "confidence": "high",
                    "evidence": [
                        {"kind": "glossary", "reference": "term:molly-tea"}
                    ],
                    "disposition": "apply",
                    "affects_translation": True,
                }
            ],
        }
        validate("calibration-result.v1.schema.json", value)
        invalid = json.loads(json.dumps(value))
        invalid["actions"][0]["kind"] = "rewrite_sentence"
        with self.assertRaises(ValidationError):
            validate("calibration-result.v1.schema.json", invalid)

    def test_translation_contract_binds_each_cue_to_source_hash(self) -> None:
        value = {
            "schema_version": TRANSLATION_RESULT_SCHEMA,
            "task_id": "task-translation",
            "project_id": "project-1",
            "based_on_revision_id": "revision-1",
            "source_language": "en",
            "target_language": "zh-CN",
            "cues": [
                {
                    "cue_id": "cue-1",
                    "source_hash": "b" * 64,
                    "target_text": "你好",
                }
            ],
        }
        validate("translation-result.v1.schema.json", value)
        del value["cues"][0]["source_hash"]
        with self.assertRaises(ValidationError):
            validate("translation-result.v1.schema.json", value)

    def test_source_and_translation_review_use_distinct_taxonomies(self) -> None:
        source = {
            "schema_version": SOURCE_REVIEW_RESULT_SCHEMA,
            "review_id": "review-source",
            "project_id": "project-1",
            "based_on_revision_id": "revision-1",
            "issues": [
                {
                    "issue_id": "source-1",
                    "issue_type": SourceReviewIssueType.SUSPECTED_MISRECOGNITION.value,
                    "cue_ids": ["cue-1"],
                    "token_ids": ["token-1"],
                    "impact": "major",
                    "confidence": "medium",
                    "description": "The word may have been misrecognized.",
                    "evidence": "The surrounding sentence is incoherent.",
                    "suggested_text": "weather",
                    "recommended_action": "inspect_audio",
                    "status": "open",
                }
            ],
        }
        translation = {
            "schema_version": TRANSLATION_REVIEW_RESULT_SCHEMA,
            "review_id": "review-translation",
            "project_id": "project-1",
            "based_on_revision_id": "revision-1",
            "issues": [
                {
                    "issue_id": "translation-1",
                    "issue_type": TranslationReviewIssueType.POLARITY_OR_LOGIC.value,
                    "cue_ids": ["cue-1"],
                    "source_token_ids": ["token-1"],
                    "impact": "major",
                    "confidence": "high",
                    "description": "The translation reverses the negation.",
                    "evidence": "Source contains not; target is affirmative.",
                    "suggested_text": "并未批准",
                    "recommended_action": "replace_translation",
                    "status": "open",
                }
            ],
        }
        validate("source-review-result.v1.schema.json", source)
        validate("translation-review-result.v1.schema.json", translation)

        invalid = json.loads(json.dumps(source))
        invalid["issues"][0]["issue_type"] = "other"
        with self.assertRaises(ValidationError):
            validate("source-review-result.v1.schema.json", invalid)

        cross_track = json.loads(json.dumps(source))
        cross_track["issues"][0]["issue_type"] = "mistranslation"
        with self.assertRaises(ValidationError):
            validate("source-review-result.v1.schema.json", cross_track)

    def test_editor_ai_task_is_an_exclusive_lock_contract(self) -> None:
        running = {
            "schema_version": EDITOR_AI_TASK_SCHEMA,
            "task_id": "task-1",
            "project_id": "project-1",
            "kind": "review",
            "state": "running",
            "locks_editor": True,
            "based_on_revision_id": "revision-1",
            "result_revision_id": None,
            "created_at": "2026-08-17T00:00:00Z",
            "started_at": "2026-08-17T00:00:01Z",
            "finished_at": None,
            "cancel_requested_at": None,
            "error": None,
        }
        validate("editor-ai-task.v1.schema.json", running)
        with self.assertRaises(ValidationError):
            validate(
                "editor-ai-task.v1.schema.json",
                {**running, "locks_editor": False},
            )

        succeeded = {
            **running,
            "state": "succeeded",
            "locks_editor": False,
            "finished_at": "2026-08-17T00:01:00Z",
        }
        validate("editor-ai-task.v1.schema.json", succeeded)

    def test_editor_ai_task_transition_rules_release_only_at_terminal_state(self) -> None:
        self.assertTrue(editor_ai_task_holds_lock(EditorAiTaskState.QUEUED))
        self.assertTrue(editor_ai_task_holds_lock(EditorAiTaskState.RUNNING))
        self.assertTrue(editor_ai_task_holds_lock(EditorAiTaskState.CANCELLING))
        self.assertFalse(editor_ai_task_holds_lock(EditorAiTaskState.SUCCEEDED))

        require_editor_ai_task_transition("queued", "running")
        require_editor_ai_task_transition("running", "cancelling")
        require_editor_ai_task_transition("cancelling", "cancelled")
        with self.assertRaises(EditorAiTaskStateError):
            require_editor_ai_task_transition("running", "cancelled")
        with self.assertRaises(EditorAiTaskStateError):
            require_editor_ai_task_transition("succeeded", "running")


if __name__ == "__main__":
    unittest.main()
