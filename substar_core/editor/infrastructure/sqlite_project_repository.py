from __future__ import annotations

from typing import Any, Mapping

from substar_core.domain import ChangeProvenance, DocumentRevision, EditorDocument
from substar_core.editor.ports import RepositoryConflictError, RepositoryError
from substar_core.storage import ProjectConflictError, ProjectStore, ProjectStoreError


class SQLiteProjectRepository:
    """ProjectRepository adapter for the existing patch/checkpoint store."""

    def __init__(self, store: ProjectStore):
        self.store = store

    def find_operation_commit(self, operations: list[Mapping[str, Any]]) -> DocumentRevision | None:
        from substar_core.editor.application.editing_service import operation_hash
        try:
            revisions = [self.store.find_receipt("operation:" + str(op["operation_id"]), operation_hash(op)) for op in operations]
        except ProjectConflictError as exc:
            raise RepositoryConflictError(str(exc)) from exc
        except ProjectStoreError as exc:
            raise RepositoryError(str(exc)) from exc
        if all(revisions):
            return max(revisions, key=lambda r: r.revision_number)
        if any(revisions):
            raise RepositoryConflictError("batch mixes committed and uncommitted operations")
        return None

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
