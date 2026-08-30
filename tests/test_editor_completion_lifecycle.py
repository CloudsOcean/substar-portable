from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from substar_core.domain.editor_document import (
    ChangeProvenance,
    DisplayCue,
    DisplayToken,
    DocumentProperties,
    EditorDocument,
    SourceToken,
    TranslationTrack,
)
from substar_core.storage.project_store import (
    ProjectIntegrityError,
    ProjectStore,
    _compress_json,
    _decompress_json,
)


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


def test_patch_revision_schema_has_no_inverse_payload_columns(tmp_path) -> None:
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
            "SELECT patch_blob "
            "FROM revisions WHERE revision_number=?",
            (second.revision_number,),
        ).fetchone()
        columns = {
            value[1] for value in connection.execute("PRAGMA table_info(revisions)")
        }

    assert row is not None
    assert row[0] is not None
    assert "inverse_patch_blob" not in columns
    assert "inverse_sha256" not in columns
    ProjectStore.clear_memory_cache(store.root)
    assert store.load_revision(first.revision_id).document == first.document
    assert store.load_latest().document == second.document


def test_legacy_revision_document_is_rejected(tmp_path) -> None:
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
    with pytest.raises(ProjectIntegrityError, match="revision content is invalid"):
        store.load_latest()

