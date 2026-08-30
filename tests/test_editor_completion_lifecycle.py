from __future__ import annotations

import sqlite3
from dataclasses import replace

from substar_core.domain.editor_document import (
    ChangeProvenance,
    DisplayCue,
    DisplayToken,
    DocumentProperties,
    EditorDocument,
    SourceToken,
    TranslationTrack,
)
from substar_core.storage.project_store import ProjectStore, _compress_json, _decompress_json


def _document(*, complete: bool) -> EditorDocument:
    return EditorDocument.create(
        document_key="completion-lifecycle",
        source_tokens=(),
        display_tokens=(),
        cues=(),
        properties=DocumentProperties(complete=complete),
    )


def test_later_revision_invalidates_completed_document(tmp_path) -> None:
    store = ProjectStore.create(tmp_path / "project", project_id="completion-test")
    accepted = store.save(
        _document(complete=True),
        provenance=ChangeProvenance(
            kind="manual", operation="set_complete_attribute", actor="test"
        ),
    )

    edited = replace(
        accepted.document,
        presentation=replace(accepted.document.presentation, upper_punctuation="remove"),
    )
    saved = store.save(
        edited,
        provenance=ChangeProvenance(
            kind="manual", operation="set_presentation", actor="test"
        ),
        expected_revision_id=accepted.revision_id,
    )

    assert saved.document.complete is False
    assert store.load_latest().document.complete is False


def test_explicit_completion_revision_keeps_completed_identity(tmp_path) -> None:
    store = ProjectStore.create(tmp_path / "project", project_id="completion-test")
    saved = store.save(
        _document(complete=True),
        provenance=ChangeProvenance(
            kind="manual", operation="set_complete_attribute", actor="test"
        ),
    )

    assert saved.document.complete is True


def test_patch_revision_does_not_store_unused_inverse_payload(tmp_path) -> None:
    store = ProjectStore.create(tmp_path / "project", project_id="compact-history-test")
    first = store.save(
        _document(complete=False),
        provenance=ChangeProvenance(
            kind="manual", operation="create_document", actor="test"
        ),
    )
    edited = replace(
        first.document,
        presentation=replace(first.document.presentation, upper_punctuation="remove"),
    )
    second = store.save(
        edited,
        provenance=ChangeProvenance(
            kind="manual", operation="set_presentation", actor="test"
        ),
        expected_revision_id=first.revision_id,
    )

    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT patch_blob, inverse_patch_blob, inverse_sha256 "
            "FROM revisions WHERE revision_number=?",
            (second.revision_number,),
        ).fetchone()

    assert row is not None
    assert row[0] is not None
    assert row[1] is None
    assert row[2] is None
    ProjectStore.clear_memory_cache(store.root)
    assert store.load_revision(first.revision_id).document == first.document
    assert store.load_latest().document == second.document


def test_legacy_revision_hash_is_checked_before_schema_defaults(tmp_path) -> None:
    provenance = ChangeProvenance(
        kind="source", operation="legacy-translation", actor="test"
    )
    source = SourceToken.create(index=0, text="hello", start=0.0, end=1.0)
    display = DisplayToken.create(
        position=0,
        text="hello",
        source_token_ids=[source.token_id],
        provenance=provenance,
    )
    cue = DisplayCue.create(
        index=0,
        display_token_ids=[display.token_id],
        start=0.0,
        end=1.0,
        target=TranslationTrack(target_text="你好", provenance=provenance),
    )
    document = EditorDocument.create(
        source_tokens=[source],
        display_tokens=[display],
        cues=[cue],
        document_key="legacy-translation-hash",
    )
    store = ProjectStore.create(tmp_path / "project", project_id="legacy-hash-test")
    saved = store.save(document, provenance=provenance)

    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT snapshot_blob, payload_sha256 FROM revisions WHERE revision_number=1"
        ).fetchone()
        assert row is not None
        legacy_value = _decompress_json(row[0], row[1])
        target = legacy_value["cues"][0]["target"]
        target.pop("translation_status")
        target.pop("issue_code")
        target.pop("editable")
        legacy_blob, legacy_hash = _compress_json(legacy_value)
        connection.execute(
            "UPDATE revisions SET snapshot_blob=?, payload_sha256=?, document_hash=? "
            "WHERE revision_number=1",
            (legacy_blob, legacy_hash, legacy_hash),
        )
        connection.commit()

    ProjectStore.clear_memory_cache(store.root)
    loaded = store.load_latest()

    assert loaded.revision_id == saved.revision_id
    assert loaded.document.cues[0].target is not None
    assert loaded.document.cues[0].target.translation_status == "translated"
    assert loaded.document.cues[0].target.editable is True
    assert loaded.document_hash == loaded.document.content_hash()

