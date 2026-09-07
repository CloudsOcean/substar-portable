from __future__ import annotations

import unittest
from unittest.mock import patch
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

    def test_failed_limit_repair_preserves_original_as_manual_candidate(self) -> None:
        group = _group("cue_1", hard_limit=5)
        with patch(
            "substar_core.editor.translation.contextual.api_call",
            side_effect=RuntimeError("repair unavailable"),
        ):
            plans, repair = complete_results(
                settings={"translation_workers": 1},
                repair_prompt="repair",
                groups=[group],
                response={"group_results": [_row("cue_1", target_text="Long but useful")]},
            )
        self.assertEqual(plans[0]["meaning_units"][0]["target_text"], "Long but useful")
        self.assertEqual(repair["invalid_group_ids"], [])
        self.assertEqual(repair["model_repair"]["groups"], [])
        self.assertEqual(repair["quality_issues"][0]["code"], "target_over_limit")

    def test_limit_repair_replaces_only_the_rejected_local_scope(self) -> None:
        group = _group("cue_1", hard_limit=5)
        def repaired(**kwargs: object) -> tuple[dict[str, object], dict[str, str]]:
            repair_group = kwargs["groups"][0]  # type: ignore[index]
            row = _row("cue_1", target_text="Short")
            row["group_id"] = repair_group["group_id"]
            return {"group_results": [row]}, {"model": "repair"}
        with patch(
            "substar_core.editor.translation.contextual.api_call",
            side_effect=repaired,
        ) as api:
            plans, repair = complete_results(
                settings={"translation_workers": 1},
                repair_prompt="repair",
                groups=[group],
                response={"group_results": [_row("cue_1", target_text="Long but useful")]},
            )
        self.assertEqual(plans[0]["meaning_units"][0]["target_text"], "Long but useful")
        api.assert_not_called()
        self.assertEqual(repair["model_repair"]["groups"], [])

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

    def test_translation_problem_queue_uses_current_track_state(self) -> None:
        active = SimpleNamespace(value="active")
        deleted = SimpleNamespace(value="deleted")
        def cue(name, state, status):
            return SimpleNamespace(cue_id=name, state=state, target=SimpleNamespace(translation_status=status))
        document = SimpleNamespace(cues=[cue("cue_2", active, "manual_required"),
                                        cue("cue_5", active, "needs_review"),
                                        cue("old", deleted, "manual_required"),
                                        cue("fixed", active, "translated")])
        self.assertEqual(translation_problem_cue_ids(document), ["cue_2", "cue_5"])


if __name__ == "__main__":
    unittest.main()
