from __future__ import annotations

import unittest

from scripts.run_semantic_segmentation import finalize_display_cuts


class SemanticSegmentationSeamTests(unittest.TestCase):
    def test_execution_seams_are_preserved_as_display_cuts(self) -> None:
        cuts = finalize_display_cuts(
            {2, 8},
            [5, 11],
            final_alignment_index=14,
        )

        self.assertEqual(cuts, {2, 5, 8, 11})

    def test_final_alignment_index_is_not_emitted_as_a_cut(self) -> None:
        cuts = finalize_display_cuts(
            {2},
            [5, 9],
            final_alignment_index=9,
        )

        self.assertEqual(cuts, {2, 5})


if __name__ == "__main__":
    unittest.main()
