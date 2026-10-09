from dataclasses import replace

import pytest

from substar_core.document_operations import apply_document_operation, DocumentOperationError
from substar_core.domain import EntityState, TranslationTrack, ChangeProvenance, ChangeKind
from test_architecture_audit import document


def merge(doc, **settings):
    result = apply_document_operation(doc, {
        "operation_id": "auto_merge_test", "type": "auto_merge_cues", "payload": settings,
    })
    result.validate()
    return result


@pytest.mark.parametrize("direction", ["previous", "next"])
def test_snap_only_preserves_text_and_closes_gap(direction):
    doc = document(6)
    result = merge(doc, absorb=False, snap_direction=direction)
    assert len(result.cues) == 3
    assert result.display_tokens == doc.display_tokens
    assert all(left.end == right.start for left, right in zip(result.cues, result.cues[1:]))
    assert result.cues[0].start == doc.cues[0].start
    assert result.cues[-1].end == doc.cues[-1].end
    assert len(result.changes) == len(doc.changes) + 1


@pytest.mark.parametrize("direction", ["previous", "next"])
def test_absorb_chain_keeps_tokens_translations_and_one_change(direction):
    doc = document(6)
    provenance = ChangeProvenance(ChangeKind.MANUAL, "test")
    doc = replace(doc, cues=tuple(replace(cue, target=TranslationTrack(str(i), provenance)) for i, cue in enumerate(doc.cues)))
    result = merge(doc, absorb_direction=direction)
    assert len(result.cues) == 1
    assert result.cues[0].display_token_ids == tuple(token.token_id for token in doc.display_tokens)
    assert result.cues[0].target.target_text == "0 1 2"
    assert len(result.changes) == 1


def test_zero_gap_absorbs_touching_but_does_not_snap():
    doc = document()
    assert len(merge(doc, gap_ms=0).cues) == 2
    doc = replace(doc, cues=(replace(doc.cues[0], end=2), doc.cues[1]))
    assert len(merge(doc, gap_ms=0).cues) == 1


@pytest.mark.parametrize("boundary", ["deleted", "speaker", "overlay", "overlap"])
def test_protected_boundaries_are_not_merged(boundary):
    doc = document()
    right = doc.cues[1]
    if boundary == "deleted":
        doc = apply_document_operation(doc, {"operation_id": "delete", "type": "delete", "payload": {"cue_ids": [right.cue_id]}})
    elif boundary == "speaker":
        doc = replace(doc, cues=(doc.cues[0], replace(right, speaker="speaker_1")))
    elif boundary == "overlay":
        doc = replace(doc, cues=(doc.cues[0], replace(right, mapping={"subtitle_track": "overlay"})))
    else:
        doc = replace(doc, source_tokens=(), display_tokens=(), cues=(replace(doc.cues[0], end=2.1, source_text="前文", display_token_ids=()), replace(right, source_text="短句", display_token_ids=())))
    result = merge(doc)
    assert result.cues == doc.cues


@pytest.mark.parametrize("text,expected", [("甲乙丙", 2), ("甲乙", 1), ("one two three", 2), ("one two", 1), ("one甲乙", 2)])
def test_imported_text_counts_chars_and_words_strictly(text, expected):
    doc = document()
    doc = replace(doc, cues=tuple(replace(cue, source_text="前文" if i == 0 else text, display_token_ids=()) for i, cue in enumerate(doc.cues)), display_tokens=(), source_tokens=())
    result = merge(doc)
    assert len(result.cues) == expected
    if expected == 1:
        assert "前文" in result.cues[0].source_text and text in result.cues[0].source_text


@pytest.mark.parametrize("settings", [{"gap_ms": -1}, {"gap_ms": float("nan")}, {"short_count": 0}, {"absorb": "false"}, {"snap_direction": "bad"}])
def test_invalid_settings_rejected(settings):
    with pytest.raises(DocumentOperationError):
        merge(document(), **settings)


def test_bulk_merge_is_one_durable_revision_and_retry_is_idempotent(tmp_path):
    from substar_core.storage import ProjectStore
    from substar_core.editor.application.editing_service import EditingService
    from substar_core.editor.infrastructure.sqlite_project_repository import SQLiteProjectRepository

    store = ProjectStore.create(tmp_path / "project", project_id="audit")
    initial = store.save(document(6), provenance=ChangeProvenance(ChangeKind.IMPORT, "init"))
    service = EditingService(lambda _: SQLiteProjectRepository(store))
    base = {"document_id": "audit", "revision_id": initial.revision_id, "document_hash": initial.document_hash}
    operation = {"operation_id": "merge_once", "type": "auto_merge_cues", "payload": {}}
    committed = service.commit_batch("audit", base=base, operations=[operation], batch_id="merge")
    replay = service.commit_batch("audit", base=base, operations=[operation], batch_id="retry")
    assert replay.after.revision_id == committed.after.revision_id
    assert store.load_latest().revision_number == 2
    assert len(store.load_latest().document.cues) == 1
    assert store.load_revision(committed.after.parent_revision_id).document == initial.document
    restored = store.save(initial.document, provenance=ChangeProvenance(ChangeKind.MANUAL, "restore_revision"), expected_revision_id=committed.after.revision_id)
    assert restored.document == initial.document
