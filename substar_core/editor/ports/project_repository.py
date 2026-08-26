from __future__ import annotations

from typing import Any, Protocol

from substar_core.domain import ChangeProvenance, DocumentRevision, EditorDocument


class RepositoryConflictError(RuntimeError):
    """The durable latest revision changed before a commit completed."""


class RepositoryError(RuntimeError):
    """The repository could not durably complete an otherwise valid request."""


class ProjectRepository(Protocol):
    def load_latest(self) -> DocumentRevision | None: ...

    def save(
        self,
        document: EditorDocument,
        *,
        provenance: ChangeProvenance,
        expected_revision_id: str | None = None,
    ) -> DocumentRevision: ...

    def list_revision_metadata(
        self,
        *,
        limit: int | None = None,
        before_revision_number: int | None = None,
    ) -> list[dict[str, Any]]: ...
