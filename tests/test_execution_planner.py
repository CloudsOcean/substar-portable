from __future__ import annotations

import unittest

from substar_core.segmentation.execution_planner import (
    EXECUTION_BLOCK_PLAN_SCHEMA,
    execution_block_plan,
    plan_execution_seams,
)


def units(count: int = 240) -> list[dict]:
    return [
        {
            "index": index,
            "start": float(index),
            "end": float(index) + 0.8,
            "text": f"word{index}",
            "sentence_end": False,
            "speaker_id": "speaker-1",
            "speaker_confidence": 1.0,
        }
        for index in range(count)
    ]


class ExecutionPlannerTests(unittest.TestCase):
    def test_ignores_asr_sentence_end_and_punctuation(self) -> None:
        source = units(400)
        source[194]["sentence_end"] = True
        source[194]["text"] = "complete."
        seams, evidence = plan_execution_seams(source, target_seconds=180)
        self.assertEqual(seams[0], 179)
        self.assertNotIn("sentence_end", evidence[0].__dict__)

    def test_prefers_observed_low_volume_boundary(self) -> None:
        source = units(240)
        source[176]["boundary_rms_db"] = -50.0
        seams, evidence = plan_execution_seams(source, target_seconds=180)
        self.assertEqual(seams, [176])
        self.assertTrue(evidence[0].low_volume)

    def test_forbidden_boundary_is_never_selected(self) -> None:
        source = units(240)
        source[116]["sentence_end"] = True
        seams, _ = plan_execution_seams(
            source, target_seconds=180, forbidden_after={116}
        )
        self.assertNotEqual(seams, [116])

    def test_plan_is_complete_and_uses_canonical_names(self) -> None:
        plan = execution_block_plan(units(400), target_seconds=180)
        self.assertEqual(plan["schema_version"], EXECUTION_BLOCK_PLAN_SCHEMA)
        self.assertEqual(plan["blocks"][0]["block_id"], "block_0001")
        self.assertEqual(plan["blocks"][0]["alignment_start"], 0)
        self.assertEqual(plan["blocks"][-1]["alignment_end"], 399)

    def test_default_profile_is_bounded_to_75_100_seconds(self) -> None:
        plan = execution_block_plan(units(487))
        self.assertEqual(plan["target_seconds"], 90.0)
        self.assertEqual(plan["minimum_seconds"], 75.0)
        self.assertEqual(plan["maximum_seconds"], 100.0)
        self.assertFalse(plan["exceptions"])
        self.assertTrue(all(
            75 <= block["end"] - block["start"] <= 100
            for block in plan["blocks"]
        ))

    def test_downstream_plan_cuts_only_at_accepted_meaning_group_ends(self) -> None:
        allowed = {79, 159, 239, 319, 409}
        plan = execution_block_plan(
            units(487), allowed_after=allowed,
            basis="accepted_ai_semantic_groups",
        )
        self.assertEqual(plan["basis"], "accepted_ai_semantic_groups")
        self.assertTrue(set(plan["boundaries_after"]) <= allowed)
        self.assertFalse(plan["exceptions"])


if __name__ == "__main__":
    unittest.main()
