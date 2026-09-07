import sqlite3

import pytest

from substar_core.domain.editor_document import ChangeProvenance, EditorDocument
from substar_core.editor import http_api
from substar_core.storage.project_store import ProjectStore


@pytest.mark.parametrize("legacy", [False, True])
def test_catalog_does_not_reconstruct_documents(tmp_path, monkeypatch, legacy):
    store = ProjectStore.create(tmp_path / "sample" / "project", project_id="sample")
    revision = store.save(
        EditorDocument.create(document_key="sample", source_tokens=(), display_tokens=(), cues=()),
        provenance=ChangeProvenance(kind="manual", operation="create_document", actor="test"),
    )
    if legacy:
        with sqlite3.connect(store.database_path) as connection:
            connection.execute("DELETE FROM metadata WHERE key='document_schema_version'")
    def forbidden(*args, **kwargs):
        raise AssertionError("catalog must not load full revision history or documents")
    monkeypatch.setattr(ProjectStore, "load_latest", forbidden)
    monkeypatch.setattr(ProjectStore, "load_manifest", forbidden)
    monkeypatch.setattr(http_api, "_projects_root", lambda: tmp_path)
    rows = http_api.list_projects()["projects"]
    assert len(rows) == 1
    assert rows[0]["latest_revision_id"] == revision.revision_id
    assert rows[0]["revision_count"] == 1
    assert rows[0]["updated_at"] == revision.created_at


def test_empty_store_has_no_summary(tmp_path):
    store = ProjectStore.create(tmp_path / "project", project_id="empty")
    assert store.load_summary() is None
