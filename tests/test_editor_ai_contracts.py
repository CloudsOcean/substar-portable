from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from substar_core.editor.calibration import (
    CALIBRATION_RESULT_SCHEMA,
    CalibrationActionKind,
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
            "calibration-result.v2.schema.json",
            "translation-result.v2.schema.json",
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
        validate("calibration-result.v2.schema.json", value)
        invalid = json.loads(json.dumps(value))
        invalid["actions"][0]["kind"] = "rewrite_sentence"
        with self.assertRaises(ValidationError):
            validate("calibration-result.v2.schema.json", invalid)

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
                    "translation_status": "translated",
                    "issue_code": None,
                    "editable": True,
                }
            ],
        }
        validate("translation-result.v2.schema.json", value)
        del value["cues"][0]["source_hash"]
        with self.assertRaises(ValidationError):
            validate("translation-result.v2.schema.json", value)

if __name__ == "__main__":
    unittest.main()
