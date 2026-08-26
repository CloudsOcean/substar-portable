from __future__ import annotations

from typing import Any

from substar_core.domain import ChangeProvenance, DocumentRevision, EditorDocument
from substar_core.editor.ports import RepositoryConflictError, RepositoryError
from substar_core.storage import ProjectConflictError, ProjectStore, ProjectStoreError


class SQLiteProjectRepository:
    """ProjectRepository adapter for the existing patch/checkpoint store."""

    def __init__(self, store: ProjectStore):
        self.store = store

    def load_latest(self) -> DocumentRevision | None:
        return self.store.load_latest()

    def save(
        self,
        document: EditorDocument,
        *,
        provenance: ChangeProvenance,
        expected_revision_id: str | None = None,
    ) -> DocumentRevision:
        try:
            return self.store.save(
                document,
                provenance=provenance,
                expected_revision_id=expected_revision_id,
            )
        except ProjectConflictError as exc:
            raise RepositoryConflictError(str(exc)) from exc
        except ProjectStoreError as exc:
            raise RepositoryError(str(exc)) from exc

    def list_revision_metadata(
        self,
        *,
        limit: int | None = None,
        before_revision_number: int | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.list_revision_metadata(
            limit=limit,
            before_revision_number=before_revision_number,
        )
