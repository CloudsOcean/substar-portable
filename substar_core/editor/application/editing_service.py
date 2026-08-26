from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from substar_core.document_operations import (
    DocumentOperationError,
    apply_document_operation,
)
from substar_core.domain import ChangeKind, ChangeProvenance, DocumentRevision
from substar_core.editor.ports import (
    ProjectRepository,
    RepositoryConflictError,
    RepositoryError,
)


class EmptyProjectError(RuntimeError):
    pass


class InvalidEditorOperationError(ValueError):
    def __init__(self, message: str, operation_id: str | None = None):
        super().__init__(message)
        self.operation_id = operation_id


class EditorPersistenceError(RuntimeError):
    pass


class StaleOperationError(RuntimeError):
    def __init__(self, latest: Mapping[str, str]):
        super().__init__("operation is based on a stale revision")
        self.latest = dict(latest)


@dataclass(frozen=True)
class OperationCommit:
    before: DocumentRevision
    after: DocumentRevision


class EditingService:
    """Orchestrate one editor operation without knowing HTTP or SQLite."""

    def __init__(
        self, repository_factory: Callable[[str], ProjectRepository]
    ) -> None:
        self._repository_factory = repository_factory

    def commit_operation(
        self, project_id: str, operation: Mapping[str, Any]
    ) -> OperationCommit:
        repository = self._repository_factory(project_id)
        latest = repository.load_latest()
        if latest is None:
            raise EmptyProjectError("project has no document revision")
        expected_base = {
            "document_id": latest.document.document_id,
            "revision_id": latest.revision_id,
            "document_hash": latest.document_hash
            or latest.document.content_hash(),
        }
        base = operation.get("base", {})
        if any(base.get(key) != value for key, value in expected_base.items()):
            raise StaleOperationError(expected_base)
        try:
            document = apply_document_operation(latest.document, operation)
        except (KeyError, TypeError, ValueError, DocumentOperationError) as exc:
            raise InvalidEditorOperationError(str(exc)) from exc
        provenance = ChangeProvenance(
            kind=ChangeKind.MANUAL,
            operation=f"apply_{operation.get('type', '')}",
            actor="editor",
        )
        try:
            revision = repository.save(
                document,
                provenance=provenance,
                expected_revision_id=latest.revision_id,
            )
        except RepositoryConflictError as exc:
            raise StaleOperationError(expected_base) from exc
        except RepositoryError as exc:
            raise EditorPersistenceError(str(exc)) from exc
        return OperationCommit(before=latest, after=revision)

    def commit_batch(
        self,
        project_id: str,
        *,
        base: Mapping[str, Any],
        operations: list[Mapping[str, Any]],
        batch_id: str,
    ) -> OperationCommit:
        if not operations:
            raise InvalidEditorOperationError("operation batch cannot be empty")
        repository = self._repository_factory(project_id)
        latest = repository.load_latest()
        if latest is None:
            raise EmptyProjectError("project has no document revision")
        expected_base = {
            "document_id": latest.document.document_id,
            "revision_id": latest.revision_id,
            "document_hash": latest.document_hash
            or latest.document.content_hash(),
        }
        if any(base.get(key) != value for key, value in expected_base.items()):
            raise StaleOperationError(expected_base)
        document = latest.document
        for operation in operations:
            operation_id = str(operation.get("operation_id", "")) or None
            try:
                document = apply_document_operation(document, operation)
            except (KeyError, TypeError, ValueError, DocumentOperationError) as exc:
                raise InvalidEditorOperationError(str(exc), operation_id) from exc
        provenance = ChangeProvenance(
            kind=ChangeKind.MANUAL,
            operation="operation_batch",
            actor="editor",
            metadata={
                "batch_id": batch_id,
                "operation_ids": [
                    str(operation.get("operation_id", "")) for operation in operations
                ],
                "operation_count": len(operations),
            },
        )
        try:
            revision = repository.save(
                document,
                provenance=provenance,
                expected_revision_id=latest.revision_id,
            )
        except RepositoryConflictError as exc:
            raise StaleOperationError(expected_base) from exc
        except RepositoryError as exc:
            raise EditorPersistenceError(str(exc)) from exc
        return OperationCommit(before=latest, after=revision)
