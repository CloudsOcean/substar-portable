import unittest
import tempfile
from pathlib import Path
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from substar_core.editor.application.review_exchange import export_opinions, validate_package, merge_opinions
from substar_core.editor.application.review_notes import add_note, project_note
from substar_core.editor.application.review_store import read_reviews, update_reviews
from substar_core.domain import SourceToken, DisplayToken, DisplayCue, EditorDocument, ChangeProvenance, ChangeKind, EntityState
from substar_core.editor import http_api


class ReviewExchangeTests(unittest.TestCase):
    def setUp(self):
        source = SourceToken.create(index=0, text="Hello", start=1, end=2)
        token = DisplayToken.create(position=0, text="Hello", source_token_ids=(source.token_id,), provenance=ChangeProvenance(ChangeKind.SOURCE, "test"))
        self.doc = EditorDocument("project", (source,), (token,), (DisplayCue("cue", 0, (token.token_id,), 1, 2),))
        self.note = add_note(self.doc, cue_id="cue", track="source", token_ids=[token.token_id], text="修改建议")

    def test_round_trip_dedup_and_conflict_preservation(self):
        package = export_opinions("project", "old", [self.note])
        incoming = validate_package(package, "project")
        notes = []
        self.assertEqual(merge_opinions(notes, incoming)["added"], 1)
        self.assertEqual(merge_opinions(notes, incoming)["unchanged"], 1)
        notes[0]["text"] = "本地较新意见"
        self.assertEqual(merge_opinions(notes, incoming)["conflicts"], 1)
        self.assertEqual(notes[0]["text"], "本地较新意见")
        self.assertEqual(notes[0]["import_conflicts"][0]["text"], "修改建议")
        merge_opinions(notes, incoming)
        self.assertEqual(len(notes[0]["import_conflicts"]), 1)
        self.assertEqual(validate_package(export_opinions("project", "new", notes), "project"), notes)

    def test_overlap_does_not_create_second_annotation(self):
        other = {**deepcopy(self.note), "id": "different", "text": "另一条"}
        notes = [deepcopy(self.note)]
        merge_opinions(notes, [other])
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["import_conflicts"][0]["id"], "different")

    def test_newer_document_and_missing_anchor(self):
        moved = replace(self.doc, cues=(replace(self.doc.cues[0], start=5, end=6),))
        self.assertEqual(project_note(moved, self.note)["current_start"], 5)
        regrouped = replace(moved, cues=(replace(moved.cues[0], cue_id="regrouped"),))
        self.assertEqual(project_note(regrouped, self.note)["anchor"]["cue_id"], "regrouped")
        self.assertEqual(self.note["anchor"]["cue_id"], "cue")
        changed = replace(moved, display_tokens=(replace(self.doc.display_tokens[0], text="Hi"),))
        self.assertEqual(project_note(changed, self.note)["anchor_status"], "text_changed")
        deleted = replace(changed, cues=(replace(changed.cues[0], state=EntityState.DELETED),))
        self.assertEqual(project_note(deleted, self.note)["anchor_status"], "needs_relocation")
        self.assertEqual(self.note["anchor"]["text_snapshot"], "Hello")

    def test_validation_rejects_wrong_project_and_bad_anchor(self):
        package = export_opinions("project", "old", [self.note])
        with self.assertRaises(ValueError):
            validate_package(package, "other")
        package["notes"][0]["anchor"]["token_ids"] = [{}]
        with self.assertRaises(ValueError):
            validate_package(package, "project")

    def test_endpoint_import_only_writes_sidecar_and_checks_versions(self):
        latest = SimpleNamespace(revision_id="new", document=self.doc)
        store = unittest.mock.Mock()
        store.load_latest.return_value = latest
        package = export_opinions("project", "old", [self.note])
        with tempfile.TemporaryDirectory() as directory, patch.object(http_api, "open_project_store", return_value=store), patch.object(http_api, "project_job_path", return_value=Path(directory)):
            payload = http_api.ReviewImportRequest(expected_version=0, expected_revision_id="new", package=package)
            result = http_api.import_review_opinions("project", payload)
            self.assertEqual(result["import_summary"]["added"], 1)
            self.assertEqual(result["revision_id"], "new")
            self.assertEqual({c[0] for c in store.mock_calls}, {"load_latest"})
            before = read_reviews(Path(directory) / "review" / "notes.json")
            with self.assertRaises(http_api.HTTPException):
                http_api.import_review_opinions("project", payload)
            self.assertEqual(read_reviews(Path(directory) / "review" / "notes.json"), before)
            self.assertEqual(http_api.export_review_opinions("project")["notes"], before["notes"])


if __name__ == "__main__":
    unittest.main()
