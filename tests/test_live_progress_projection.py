from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from substar_core.creation.projection import subtitle_creation_projection
from substar_core.stage_progress import StageProgress


class LiveProgressProjectionTests(unittest.TestCase):
    def test_success_with_review_reaches_editor_instead_of_stalling_at_99(self) -> None:
        result = subtitle_creation_projection(
            transcription={"state": "succeeded", "progress": 1.0},
            segmentation={
                "state": "succeeded_with_issues",
                "progress": 1.0,
                "needs_attention": True,
                "result": {"summary": {"review_required_count": 1}},
            },
            editor_ready=True,
            cancel_requested=False,
        )

        self.assertEqual(result["status"], "awaiting_edit")
        self.assertEqual(result["progress"], 1.0)
        self.assertIn("1 处需要复核", result["message"])

    def test_cancel_settles_when_review_result_already_succeeded(self) -> None:
        result = subtitle_creation_projection(
            transcription={"state": "succeeded", "progress": 1.0},
            segmentation={"state": "succeeded_with_issues", "progress": 1.0},
            editor_ready=True,
            cancel_requested=True,
        )

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["progress"], 0.99)

    def test_success_with_review_reports_broken_editor_materialization(self) -> None:
        result = subtitle_creation_projection(
            transcription={"state": "succeeded", "progress": 1.0},
            segmentation={"state": "succeeded_with_issues", "progress": 1.0},
            editor_ready=False,
            cancel_requested=False,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["progress"], 0.99)
        self.assertIn("不可读", result["error"])

    def test_stage_ledger_notifies_after_plan_and_block_completion(self) -> None:
        snapshots: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = StageProgress(
                Path(directory) / "progress.json",
                on_update=snapshots.append,
            )
            ledger.plan("semantic_grouping", 2, block_ids=["c0001", "c0002"])
            ledger.event("semantic_grouping", "response", block_id="c0001")
            ledger.event("semantic_grouping", "accepted", block_id="c0001")

        self.assertEqual(len(snapshots), 3)
        row = snapshots[-1]["stages"]["semantic_grouping"]
        self.assertEqual(row["planned"], 2)
        self.assertEqual(row["responses"], 1)
        self.assertEqual(row["accepted"], 1)

    def test_semantic_grouping_maps_from_half_to_ninety_five_percent(self) -> None:
        transcription = {"state": "succeeded", "progress": 1.0}
        base = subtitle_creation_projection(
            transcription=transcription,
            segmentation={"state": "running", "progress": 0.230769},
            editor_ready=False,
            cancel_requested=False,
        )
        complete = subtitle_creation_projection(
            transcription=transcription,
            segmentation={"state": "running", "progress": 0.923077},
            editor_ready=False,
            cancel_requested=False,
        )
        self.assertAlmostEqual(base["progress"], 0.5, places=5)
        self.assertAlmostEqual(complete["progress"], 0.95, places=5)


if __name__ == "__main__":
    unittest.main()
