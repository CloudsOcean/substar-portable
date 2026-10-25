import pytest

from substar_core.domain import SourceToken, DisplayToken, DisplayCue, EditorDocument, ChangeProvenance, ChangeKind
from substar_core.document_operations import apply_document_operation, DocumentOperationError
from substar_core.storage import ProjectStore


def document():
    provenance = ChangeProvenance(ChangeKind.SOURCE, "fixture")
    source = SourceToken.create(index=0, text="hello", start=0, end=1)
    token = DisplayToken.create(position=0, text="hello", source_token_ids=(source.token_id,), provenance=provenance)
    cue = DisplayCue(cue_id="cue", index=0, display_token_ids=(token.token_id,), start=0, end=1)
    return EditorDocument("speakers", (source,), (token,), (cue,))


def operation(doc, kind, payload):
    return apply_document_operation(doc, {"operation_id":kind, "type":kind, "payload":payload})


def test_dynamic_speakers_roundtrip_and_restore(tmp_path):
    original = document()
    names = {f"speaker_{i}":f"Person {i}" for i in range(100)}
    names["guest-A"] = "Guest"
    changed = operation(original, "set_speaker_names", {"speaker_names":names})
    changed = operation(changed, "set_cue_speaker", {"cue_id":"cue", "speaker":"speaker_99"})
    assert changed.source_tokens == original.source_tokens
    assert changed.display_tokens == original.display_tokens
    assert dict(changed.properties.speaker_names) == names
    store = ProjectStore.create(tmp_path / "p", project_id="speakers")
    provenance = ChangeProvenance(ChangeKind.MANUAL, "edit")
    before = store.save(original, provenance=provenance)
    after = store.save(changed, provenance=provenance, expected_revision_id=before.revision_id)
    reopened = ProjectStore.open(tmp_path / "p")
    assert reopened.load_latest().document == changed
    assert reopened.load_revision(before.revision_id).document == original
    assert reopened.load_revision(after.revision_id).document.cues[0].speaker == "speaker_99"
    cleared = operation(changed, "set_cue_speaker", {"cue_id":"cue", "speaker":None})
    assert cleared.cues[0].speaker is None


@pytest.mark.parametrize("speaker", ["a\ninvalid", "x" * 121])
def test_invalid_speaker_is_rejected(speaker):
    with pytest.raises(DocumentOperationError):
        operation(document(), "set_cue_speaker", {"cue_id":"cue", "speaker":speaker})


def test_renaming_preserves_identifiers():
    doc = operation(document(), "set_cue_speaker", {"cue_id":"cue", "speaker":"guest-A"})
    renamed = operation(doc, "set_speaker_names", {"speaker_names":{"guest-A":"嘉宾"}})
    assert renamed.cues == doc.cues
    assert dict(renamed.properties.speaker_names) == {"guest-A":"嘉宾"}


def test_batch_speaker_assignment_is_one_change_and_preserves_other_cues(tmp_path):
    from dataclasses import replace
    original=document()
    provenance=ChangeProvenance(ChangeKind.SOURCE,"fixture")
    sources=tuple(SourceToken.create(index=i,text="hello",start=i,end=i+1) for i in range(30))
    tokens=tuple(DisplayToken.create(position=i,text="hello",source_token_ids=(src.token_id,),provenance=provenance) for i,src in enumerate(sources))
    original=replace(original,source_tokens=sources,display_tokens=tokens,cues=tuple(replace(original.cues[0],cue_id=f"cue_{i}",index=i,start=i,end=i+1,display_token_ids=(tokens[i].token_id,),speaker="speaker_0") for i in range(30)))
    ids=[f"cue_{i}" for i in range(25)]
    changed=operation(original,"set_cue_speaker",{"cue_ids":ids,"speaker":"speaker_1"})
    assert all(cue.speaker=="speaker_1" for cue in changed.cues[:25])
    assert changed.cues[25:]==original.cues[25:]
    assert len(changed.changes)==len(original.changes)+1
    assert changed.display_tokens==original.display_tokens
    assert all(cue.speaker=="speaker_0" for cue in original.cues)
    with pytest.raises(DocumentOperationError):
        operation(original,"set_cue_speaker",{"cue_ids":["cue_0","missing"],"speaker":"speaker_1"})
    from substar_core.editor.infrastructure.sqlite_project_repository import SQLiteProjectRepository
    from substar_core.editor.application.editing_service import EditingService
    store=ProjectStore.create(tmp_path / "batch",project_id="speakers")
    before=store.save(original,provenance=provenance)
    service=EditingService(lambda _:SQLiteProjectRepository(store))
    committed=service.commit_batch("speakers",base={"document_id":original.document_id,"revision_id":before.revision_id,"document_hash":before.document_hash},operations=[{"operation_id":"speaker_batch","type":"set_cue_speaker","payload":{"cue_ids":ids,"speaker":"speaker_1"}}],batch_id="speaker_batch",preserve_user_actions=True)
    assert len(store.list_revision_metadata())==2
    assert committed.after.parent_revision_id==before.revision_id
    assert store.load_revision(before.revision_id).document.cues==original.cues
    cleared=operation(changed,"set_cue_speaker",{"cue_ids":ids,"speaker":None})
    assert all(cue.speaker is None for cue in cleared.cues[:25])
