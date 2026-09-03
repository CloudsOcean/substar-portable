from __future__ import annotations

import unittest
from types import SimpleNamespace

from substar_core.prompt_registry import render_prompt
from scripts.run_semantic_segmentation import (
    SegmentationError,
    semantic_grouping_binding,
    semantic_grouping_overflow_issues,
    validate_overflow_repair_scope,
    validate_semantic_grouping_result,
)


def source_units() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            index=index,
            start=float(index),
            end=float(index) + 0.5,
            text=text,
            sentence_start=index == 0,
            sentence_end=index == 3,
            speaker_id="speaker-1",
        )
        for index, text in enumerate(("This", "is", "the", "source."))
    ]


class SemanticGroupingContractTests(unittest.TestCase):
    def valid_result(self) -> tuple[list[SimpleNamespace], dict, dict]:
        units = source_units()
        _, binding = semantic_grouping_binding(
            units, 0, 3, 1, sentence_boundary_policy="reference"
        )
        result = {
            "schema_version": "substar.semantic-grouping-result.v1",
            **binding,
            "meaning_groups": [
                {
                    "alignment_start": 0,
                    "alignment_end": 3,
                    "line_breaks_after": [3],
                }
            ],
            "exceptions": [],
        }
        return units, binding, result

    def test_accepts_exact_bound_result_without_calibration_output(self) -> None:
        units, binding, result = self.valid_result()
        protections, groups, corrections, cuts, exceptions = (
            validate_semantic_grouping_result(
                result, units, (0, 3), 1, binding, hard_limit=55
            )
        )
        self.assertEqual(protections, [])
        self.assertEqual(corrections, [])
        self.assertEqual(cuts, set())
        self.assertEqual(exceptions, [])
        self.assertEqual(groups[0]["alignment_end"], 3)

    def test_rejects_replay_from_another_input_block(self) -> None:
        units, binding, result = self.valid_result()
        result["input_fingerprint"] = "0" * 64
        with self.assertRaisesRegex(SegmentationError, "input_fingerprint"):
            validate_semantic_grouping_result(
                result, units, (0, 3), 1, binding, hard_limit=55
            )

    def test_rejects_calibration_field(self) -> None:
        units, binding, result = self.valid_result()
        result["canonicalizations"] = []
        with self.assertRaisesRegex(SegmentationError, "字段"):
            validate_semantic_grouping_result(
                result, units, (0, 3), 1, binding, hard_limit=55
            )

    def test_prompt_treats_hard_limit_as_ceiling_not_length_target(self) -> None:
        prompt = render_prompt("semantic_grouping", variant="en")
        self.assertEqual("2026-08-19.1", prompt.version)
        self.assertIn("only a rejection ceiling", prompt.text)
        self.assertIn("not a preferred length", prompt.text)
        self.assertIn("A Cue may be substantially shorter", prompt.text)
        self.assertIn("never move the boundary later merely to make the Cue longer", prompt.text)
        self.assertNotIn("Choose the latest legal boundary", prompt.text)

    def test_english_prompt_covers_clause_markers_and_preposition_roles(self) -> None:
        prompt = render_prompt("semantic_grouping", variant="en").text
        self.assertIn("complementizer `that`", prompt)
        self.assertIn("relative `that/which`", prompt)
        self.assertIn("a preposition stays with its minimal object", prompt)
        self.assertIn("a phrasal-verb particle stays with its verb", prompt)
        self.assertIn("If the appeal is unsuccessful / Molly Tea", prompt)
        self.assertIn("They are jumping in / to provide new options", prompt)

    def test_collects_all_overflows_and_accepts_only_candidate_break_additions(self) -> None:
        units = [
            SimpleNamespace(
                index=index,
                start=float(index),
                end=float(index) + 0.5,
                text=text,
                sentence_start=index == 0,
                sentence_end=index == 4,
                speaker_id="speaker-1",
            )
            for index, text in enumerate(("A" * 30, "B" * 29, "C" * 30, "D" * 29, "ok"))
        ]
        _, binding = semantic_grouping_binding(
            units, 0, 4, 1, sentence_boundary_policy="reference"
        )
        initial = {
            "schema_version": "substar.semantic-grouping-result.v1",
            **binding,
            "meaning_groups": [
                {"alignment_start": 0, "alignment_end": 1, "line_breaks_after": [1]},
                {"alignment_start": 2, "alignment_end": 3, "line_breaks_after": [3]},
                {"alignment_start": 4, "alignment_end": 4, "line_breaks_after": [4]},
            ],
            "exceptions": [],
        }

        issues = semantic_grouping_overflow_issues(
            initial, units, (0, 4), 1, binding, hard_limit=55
        )
        self.assertEqual([(item["cue_start"], item["cue_end"]) for item in issues], [(0, 1), (2, 3)])
        self.assertEqual(
            [[item["line_break_after"] for item in issue["legal_candidates"]] for issue in issues],
            [[0], [2]],
        )

        repaired = {
            **initial,
            "meaning_groups": [
                {"alignment_start": 0, "alignment_end": 1, "line_breaks_after": [0, 1]},
                {"alignment_start": 2, "alignment_end": 3, "line_breaks_after": [2, 3]},
                initial["meaning_groups"][2],
            ],
        }
        audit = validate_overflow_repair_scope(initial, repaired, issues)
        self.assertEqual(audit["reported_issue_count"], 2)
        self.assertEqual(audit["accepted_group_preserved_count"], 1)
        validate_semantic_grouping_result(
            repaired, units, (0, 4), 1, binding, hard_limit=55
        )

    def test_overflow_repair_rejects_changes_to_an_unaffected_group(self) -> None:
        units = [
            SimpleNamespace(
                index=index,
                start=float(index),
                end=float(index) + 0.5,
                text=text,
                sentence_start=index == 0,
                sentence_end=index == 3,
                speaker_id="speaker-1",
            )
            for index, text in enumerate(("A" * 30, "B" * 29, "healthy", "group"))
        ]
        _, binding = semantic_grouping_binding(
            units, 0, 3, 1, sentence_boundary_policy="reference"
        )
        initial = {
            "schema_version": "substar.semantic-grouping-result.v1",
            **binding,
            "meaning_groups": [
                {"alignment_start": 0, "alignment_end": 1, "line_breaks_after": [1]},
                {"alignment_start": 2, "alignment_end": 3, "line_breaks_after": [3]},
            ],
            "exceptions": [],
        }
        issues = semantic_grouping_overflow_issues(
            initial, units, (0, 3), 1, binding, hard_limit=55
        )
        repaired = {
            **initial,
            "meaning_groups": [
                {"alignment_start": 0, "alignment_end": 1, "line_breaks_after": [0, 1]},
                {"alignment_start": 2, "alignment_end": 3, "line_breaks_after": [2, 3]},
            ],
        }
        with self.assertRaisesRegex(SegmentationError, "accepted group"):
            validate_overflow_repair_scope(initial, repaired, issues)


if __name__ == "__main__":
    unittest.main()
