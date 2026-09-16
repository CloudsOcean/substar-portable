from dataclasses import replace
import pytest
from substar_core.editor.application.review_notes import add_note, change_note, project_note
from substar_core.domain import SourceToken, DisplayToken, DisplayCue, EditorDocument, ChangeProvenance, ChangeKind, EntityState


def document():
    source = SourceToken.create(index=0, text="Hello", start=1, end=2)
    token = DisplayToken.create(position=0, text="Hello", source_token_ids=(source.token_id,), provenance=ChangeProvenance(ChangeKind.SOURCE, "test"))
    cue = DisplayCue("cue", 0, (token.token_id,), 1, 2)
    return EditorDocument("review-test", (source,), (token,), (cue,))


def test_notes_follow_id_not_index_and_do_not_change_document():
    doc = document()
    note = add_note(doc, cue_id="cue", track="source", text="Check wording")
    moved = replace(doc, cues=(replace(doc.cues[0], index=9, start=4, end=5),))
    assert project_note(moved, note)["current_start"] == 4
    assert note["anchor"]["start"] == 1


def test_changed_and_deleted_text_retains_original_snapshot():
    doc = document()
    note = add_note(doc, cue_id="cue", track="source", text="Check")
    changed = replace(doc, display_tokens=(replace(doc.display_tokens[0], text="Hi"),))
    assert project_note(changed, note)["anchor_status"] == "text_changed"
    deleted = replace(doc, cues=(replace(doc.cues[0], state=EntityState.DELETED),))
    assert project_note(deleted, note)["anchor_status"] == "needs_relocation"
    assert note["anchor"]["text_snapshot"] == "Hello"


def test_reply_resolve_reopen_are_explicit():
    note = add_note(document(), cue_id="cue", track="source", text="Check")
    replied = change_note(note, action="reply", text="Reviewed")
    assert len(replied["replies"]) == 1 and not note["replies"]
    assert change_note(replied, action="resolve")["status"] == "resolved"
    assert change_note(change_note(replied, action="resolve"), action="reopen")["status"] == "open"


def test_invalid_anchor_and_empty_note_rejected():
    with pytest.raises(ValueError):
        add_note(document(), cue_id="cue", track="source", token_ids=["foreign"], text="Check")
    with pytest.raises(ValueError):
        add_note(document(), cue_id="cue", track="source", text=" ")


def test_sidecar_conflict_does_not_overwrite_existing_notes(tmp_path):
    from substar_core.editor.application.review_store import read_reviews, update_reviews
    path = tmp_path / "review" / "notes.json"
    note = add_note(document(), cue_id="cue", track="source", text="Check")
    update_reviews(path, 0, lambda notes: notes.append(note))
    with pytest.raises(ValueError):
        update_reviews(path, 0, lambda notes: notes.clear())
    assert read_reviews(path)["notes"] == [note]


def test_plain_text_fragment_anchors_and_changed_offsets():
    from substar_core.editor.application.subtitle_project_import import build_subtitle_document, preview_subtitle_project
    doc = build_subtitle_document(preview_subtitle_project("1\n00:00:01,000 --> 00:00:02,000\n甲😀乙\n"), "unicode")
    cue = doc.cues[0]
    note = add_note(doc, cue_id=cue.cue_id, track="source", text="Check", text_range=[1, 2])
    assert note["anchor"]["text_snapshot"] == "😀"
    assert project_note(doc, note)["anchor_status"] == "current"
    changed = replace(doc, cues=(replace(cue, source_text="甲乙"),))
    assert project_note(changed, note)["anchor_status"] == "text_changed"
