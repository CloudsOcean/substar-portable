import tempfile
import unittest
from pathlib import Path

from substar_core.editor.application.review_notes import change_note, upsert_note
from substar_core.editor.application.review_store import read_reviews, update_reviews
from substar_core.editor.http_api import ReviewNoteRequest


class ReviewDecisionTests(unittest.TestCase):
    def test_group_members_share_one_annotation_and_delete_together(self):
        from copy import deepcopy
        group = {"id": "group", "status": "open", "text": "World War II", "replies": [],
                 "anchor": {"cue_id": "cue", "track": "source", "token_ids": ["world", "war", "ii"]}}
        notes = [deepcopy(group)]
        member = {**deepcopy(group), "id": "member", "text": "updated group"}
        member["anchor"]["token_ids"] = ["war"]
        upsert_note(notes, member)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["anchor"]["token_ids"], ["world", "war", "ii"])
        self.assertEqual(notes[0]["text"], "updated group")
        overlap = deepcopy(member)
        overlap["anchor"]["token_ids"] = ["war", "another"]
        with self.assertRaises(ValueError):
            upsert_note(notes, overlap)
        notes[0] = change_note(notes[0], action="delete")
        self.assertEqual([note for note in notes if note["status"] != "deleted"], [])
        self.assertEqual(notes[0]["anchor"]["token_ids"], ["world", "war", "ii"])

    def test_one_annotation_per_token_and_delete_then_add(self):
        from copy import deepcopy
        note = {"id": "original", "status": "accepted", "text": "first", "replies": [],
                "anchor": {"cue_id": "cue", "track": "source", "token_ids": ["word"]}}
        notes = [deepcopy(note)]
        replacement = {**deepcopy(note), "id": "new", "text": "updated", "status": "open"}
        upsert_note(notes, replacement)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["id"], "original")
        self.assertEqual(notes[0]["text"], "updated")
        self.assertEqual(notes[0]["status"], "open")
        notes[0] = change_note(notes[0], action="reject")
        upsert_note(notes, replacement)
        self.assertEqual(notes[0]["status"], "rejected")
        notes[0] = change_note(notes[0], action="delete")
        upsert_note(notes, replacement)
        self.assertEqual(len([n for n in notes if n["status"] != "deleted"]), 1)
        self.assertEqual(notes[-1]["id"], "new")
        ReviewNoteRequest(expected_version=0, expected_revision_id="revision", action="delete", note_id="new")

    def test_decisions_preserve_note_and_anchor(self):
        note = {"id": "note", "status": "open", "text": "Check wording",
                "anchor": {"cue_id": "cue", "token_ids": ["word"]}, "replies": []}
        accepted = change_note(note, action="accept")
        rejected = change_note(accepted, action="reject")
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(note["status"], "open")
        self.assertEqual(rejected["anchor"], note["anchor"])
        self.assertEqual(rejected["text"], note["text"])
        self.assertEqual(change_note(rejected, action="accept")["status"], "accepted")

    def test_decisions_validate_and_persist_with_version_conflicts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "notes.json"
            update_reviews(path, 0, lambda notes: notes.append({"id": "note", "status": "resolved"}))
            for version, action, status in [(1, "accept", "accepted"), (2, "reject", "rejected")]:
                request = ReviewNoteRequest(expected_version=version, expected_revision_id="revision",
                                            action=action, note_id="note")
                update_reviews(path, version, lambda notes: notes.__setitem__(0, change_note(notes[0], action=request.action)))
                self.assertEqual(read_reviews(path)["notes"][0]["status"], status)
            with self.assertRaises(ValueError):
                update_reviews(path, 1, lambda notes: notes.clear())
            self.assertEqual(read_reviews(path)["notes"][0]["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
