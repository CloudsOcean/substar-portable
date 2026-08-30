from __future__ import annotations

import unittest
from types import SimpleNamespace

from substar_core.editor.translation.contextual import complete_results, warning_report
from substar_core.editor.translation.result_policy import translation_problem_cue_ids
from substar_core.semantic_execution import validate_presentation_plan


def _group(*cue_ids: str, hard_limit: int = 5) -> dict[str, object]:
    return {
        "group_id": "group_1",
        "cues": [
            {
                "cue_id": cue_id,
                "source_text": f"source {cue_id}",
                "hard_limit": hard_limit,
                "count_rule": "characters_including_spaces",
            }
            for cue_id in cue_ids
        ],
    }


def _row(*cue_ids: str, target_text: str) -> dict[str, object]:
    return {
        "group_id": "group_1",
        "meaning_units": [
            {
                "meaning_unit_id": "unit_1",
                "target_text": target_text,
                "source_evidence_cue_ids": list(cue_ids),
            }
        ],
        "cue_assignments": [
            {"cue_id": cue_id, "meaning_unit_id": "unit_1"}
            for cue_id in cue_ids
        ],
    }


class SemanticExecutionTests(unittest.TestCase):
    def test_over_limit_translation_is_structurally_valid(self) -> None:
        group = _group("cue_1", hard_limit=5)
        plan = validate_presentation_plan(
            group, _row("cue_1", target_text="This is longer than five characters")
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan["meaning_units"][0]["target_text"], "This is longer than five characters")

    def test_over_limit_shared_unit_is_kept_for_every_declared_cue(self) -> None:
        group = _group("cue_1", "cue_2", hard_limit=5)
        plan = validate_presentation_plan(
            group, _row("cue_1", "cue_2", target_text="One long shared translation")
        )
        self.assertIsNotNone(plan)
        self.assertEqual(
            [item["cue_id"] for item in plan["cue_assignments"]],
            ["cue_1", "cue_2"],
        )

    def test_over_limit_result_skips_repair_and_is_not_unresolved(self) -> None:
        group = _group("cue_1", hard_limit=5)
        plans, repair = complete_results(
            settings={"translation_repair_attempts": 0},
            repair_prompt="unused",
            groups=[group],
            response={"group_results": [_row("cue_1", target_text="Long but useful")]},
        )
        self.assertEqual(len(plans), 1)
        self.assertEqual(repair["invalid_group_ids"], [])

    def test_over_limit_result_is_reported_as_warning(self) -> None:
        warnings = warning_report(
            {"cue_1": "This line is too long"},
            {"english_hard_limit": 5},
        )
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["code"], "target_character_limit_warning")
        self.assertEqual(warnings[0]["cue_id"], "cue_1")

    def test_structurally_incomplete_result_still_fails(self) -> None:
        group = _group("cue_1", "cue_2")
        row = _row("cue_1", target_text="Valid text")
        self.assertIsNone(validate_presentation_plan(group, row))

    def test_empty_translation_still_fails(self) -> None:
        group = _group("cue_1")
        self.assertIsNone(validate_presentation_plan(group, _row("cue_1", target_text="  ")))

    def test_translation_problem_queue_is_read_from_revision_metadata(self) -> None:
        document = SimpleNamespace(changes=(
            SimpleNamespace(operation="split_cue", metadata={}),
            SimpleNamespace(
                operation="contextual_translation",
                metadata={"translation_problem_cue_ids": ["cue_2", "cue_2", "cue_5"]},
            ),
        ))
        self.assertEqual(translation_problem_cue_ids(document), ["cue_2", "cue_5"])


if __name__ == "__main__":
    unittest.main()
