from __future__ import annotations

import unittest

from substar_core.editor.http_api import (
    _review_issue_cue_basis,
    _validate_review_issue,
)


class EditorReviewProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.owned = {"cue-1"}
        self.tokens = {"cue-1": {"token-1"}}

    def test_source_taxonomy_accepts_source_issue(self) -> None:
        issue = _validate_review_issue(
            {
                "issue_type": "suspected_misrecognition",
                "cue_ids": ["cue-1"],
                "token_ids": ["token-1"],
                "impact": "major",
                "confidence": "medium",
                "description": "The word conflicts with the surrounding sentence.",
                "evidence": "The next sentence names a different entity.",
                "suggested_text": None,
                "recommended_action": "inspect_audio",
            },
            track="source",
            owned_cue_ids=self.owned,
            token_ids_by_cue=self.tokens,
        )
        self.assertEqual(issue["status"], "open")

    def test_translation_type_is_rejected_from_source_track(self) -> None:
        issue = _validate_review_issue(
            {
                "issue_type": "mistranslation",
                "cue_ids": ["cue-1"],
                "token_ids": ["token-1"],
                "impact": "major",
                "confidence": "high",
                "description": "Wrong target meaning.",
                "evidence": "Source and target conflict.",
                "suggested_text": "replacement",
                "recommended_action": "replace_translation",
            },
            track="source",
            owned_cue_ids=self.owned,
            token_ids_by_cue=self.tokens,
        )
        self.assertIsNone(issue)

    def test_issue_basis_is_scoped_to_its_own_cues(self) -> None:
        issue = {"cue_ids": ["cue-1"]}
        cues = {
            "cue-1": {
                "cue_id": "cue-1",
                "tokens": [{"token_id": "token-1", "text": "Molly Tea"}],
                "target_text": "茉莉奶茶",
            },
            "cue-35": {
                "cue_id": "cue-35",
                "tokens": [{"token_id": "token-35", "text": "CHAGEE"}],
                "target_text": "霸王茶姬",
            },
        }

        basis = _review_issue_cue_basis(
            issue, track="source", cues_by_id=cues
        )

        self.assertEqual(basis, [{
            "cue_id": "cue-1",
            "source_tokens": [{"token_id": "token-1", "text": "Molly Tea"}],
        }])

    def test_translation_basis_tracks_source_and_target_text(self) -> None:
        basis = _review_issue_cue_basis(
            {"cue_ids": ["cue-1"]},
            track="translation",
            cues_by_id={
                "cue-1": {
                    "cue_id": "cue-1",
                    "tokens": [{"token_id": "token-1", "text": "Molly Tea"}],
                    "target_text": "茉莉奶茶",
                }
            },
        )

        self.assertEqual(basis[0]["target_text"], "茉莉奶茶")


if __name__ == "__main__":
    unittest.main()
