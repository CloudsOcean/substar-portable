from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from substar_core.document_operations import (
    DocumentOperationError,
    apply_document_operation, apply_document_batch,
)
from substar_core.domain import ChangeKind, ChangeProvenance, DocumentRevision
from substar_core.editor.ports import (
    ProjectRepository,
    RepositoryConflictError,
    RepositoryError,
)


def operation_hash(operation: Mapping[str, Any]) -> str:
    import hashlib, json
    return hashlib.sha256(json.dumps({"type": operation.get("type"), "payload": operation.get("payload", {})}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


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
        operation_id = str(operation.get("operation_id") or "")
        if not operation_id:
            raise InvalidEditorOperationError("operation_id is required")
        lookup = getattr(repository, "find_operation_commit", None)
        if lookup is not None:
            try:
                committed = lookup([operation])
            except RepositoryConflictError as exc:
                raise InvalidEditorOperationError(str(exc)) from exc
            except RepositoryError as exc:
                raise EditorPersistenceError(str(exc)) from exc
            if committed is not None:
                return OperationCommit(before=committed, after=committed)
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
            metadata={"operation_hashes": {operation_id: operation_hash(operation)}},
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
        ids = [str(op.get("operation_id") or "") for op in operations]
        if not all(ids) or len(set(ids)) != len(ids):
            raise InvalidEditorOperationError("operation IDs must be nonempty and unique")
        repository = self._repository_factory(project_id)
        lookup = getattr(repository, "find_operation_commit", None)
        if lookup is not None:
            try:
                committed = lookup(operations)
            except RepositoryConflictError as exc:
                raise InvalidEditorOperationError(str(exc)) from exc
            except RepositoryError as exc:
                raise EditorPersistenceError(str(exc)) from exc
            if committed is not None:
                return OperationCommit(before=committed, after=committed)
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
            can_rebase = base.get("document_id") == expected_base["document_id"] and all(
                op.get("type") == "replace" and "expected_text" in op.get("payload", {}) for op in operations
            )
            if not can_rebase:
                raise StaleOperationError(expected_base)
            try:
                apply_document_batch(latest.document, operations)
            except (KeyError, TypeError, ValueError, DocumentOperationError) as exc:
                raise StaleOperationError(expected_base) from exc
        try:
            document = apply_document_batch(latest.document, operations)
        except (KeyError, TypeError, ValueError, DocumentOperationError) as exc:
            raise InvalidEditorOperationError(str(exc), getattr(exc, "operation_id", None)) from exc
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
                "operation_hashes": {str(op["operation_id"]): operation_hash(op) for op in operations},
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
