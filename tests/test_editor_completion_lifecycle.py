from __future__ import annotations

from dataclasses import replace

from substar_core.domain.editor_document import (
    ChangeProvenance,
    DocumentProperties,
    EditorDocument,
)
from substar_core.storage.project_store import ProjectStore


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

