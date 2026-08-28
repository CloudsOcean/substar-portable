from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from substar_core.creation.projection import subtitle_creation_projection
from substar_core.stage_progress import StageProgress


class LiveProgressProjectionTests(unittest.TestCase):
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
