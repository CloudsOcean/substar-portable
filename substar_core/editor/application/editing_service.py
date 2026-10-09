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
    action_revisions: tuple[DocumentRevision, ...] = ()


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
        preserve_user_actions: bool = False,
    ) -> OperationCommit:
        if not operations:
            raise InvalidEditorOperationError("operation batch cannot be empty")
        ids = [str(op.get("operation_id") or "") for op in operations]
        if not all(ids) or len(set(ids)) != len(ids):
            raise InvalidEditorOperationError("operation IDs must be nonempty and unique")
        repository = self._repository_factory(project_id)
        # Reconcile each receipt before replay: refresh can mix an ACK-lost
        # committed prefix with fresh edits. Hash verification remains mandatory.
        requested_operations = operations
        partition = getattr(repository, "uncommitted_operations", None)
        if partition is not None:
            try:
                operations = partition(operations)
            except RepositoryConflictError as exc:
                raise InvalidEditorOperationError(str(exc)) from exc
            except RepositoryError as exc:
                raise EditorPersistenceError(str(exc)) from exc
            if not operations:
                latest = repository.load_latest()
                return OperationCommit(before=latest, after=latest, action_revisions=tuple(repository.operation_revisions(requested_operations)) if preserve_user_actions else ())
        lookup = getattr(repository, "find_operation_commit", None)
        if lookup is not None:
            try:
                committed = lookup(operations)
            except RepositoryConflictError as exc:
                raise InvalidEditorOperationError(str(exc)) from exc
            except RepositoryError as exc:
                raise EditorPersistenceError(str(exc)) from exc
            if committed is not None:
                return OperationCommit(before=committed, after=committed, action_revisions=tuple(repository.operation_revisions(operations)) if preserve_user_actions else ())
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
                (op.get("type") == "replace" and "expected_text" in op.get("payload", {}))
                or (op.get("type") == "set_target" and "expected_target_text" in op.get("payload", {}))
                for op in operations
            )
            if not can_rebase:
                raise StaleOperationError(expected_base)
        # Compute each user action once; save_sequence validates and commits
        # all stages atomically. Do not compute the whole batch before stages.
        document = None
        if not preserve_user_actions:
            try:
                document = apply_document_batch(latest.document, operations)
            except (KeyError, TypeError, ValueError, DocumentOperationError) as exc:
                if any(base.get(key) != value for key, value in expected_base.items()):
                    raise StaleOperationError(expected_base) from exc
                raise InvalidEditorOperationError(str(exc), getattr(exc, "operation_id", None)) from exc
        provenance = ChangeProvenance(
            kind=ChangeKind.MANUAL,
            operation="operation_batch",
            actor="editor",
            metadata={
                "batch_id": batch_id,
                "user_action": str(operations[0].get("type", "edit")) if len(operations) == 1 else "batch_edit",
                "operation_ids": [
                    str(operation.get("operation_id", "")) for operation in operations
                ],
                "operation_count": len(operations),
                "operation_hashes": {str(op["operation_id"]): operation_hash(op) for op in operations},
            },
        )
        try:
            if preserve_user_actions:
                groups = []
                for op in operations:
                    action = op.get("payload", {}).get("provenance", {}).get("metadata", {}).get("user_action_id") or op["operation_id"]
                    if groups and groups[-1][0] == action:
                        groups[-1][1].append(op)
                    else:
                        groups.append((action, [op]))
                if len(groups) > 1:
                    stages = []
                    current = latest.document
                    for action, items in groups:
                        current = apply_document_batch(current, items)
                        stages.append((current, ChangeProvenance(
                            kind=ChangeKind.MANUAL, operation="operation_batch", actor="editor",
                            metadata={"batch_id": batch_id, "user_action_id": action,
                                      "user_action": items[0]["type"] if len(items) == 1 else "batch_edit",
                                      "operation_ids": [op["operation_id"] for op in items],
                                      "operation_hashes": {op["operation_id"]: operation_hash(op) for op in items}}
                        )))
                    revisions = repository.save_sequence(stages, expected_revision_id=latest.revision_id)
                    return OperationCommit(before=latest, after=revisions[-1], action_revisions=tuple(revisions))
            if document is None:
                document = apply_document_batch(latest.document, operations)
            revision = repository.save(
                document,
                provenance=provenance,
                expected_revision_id=latest.revision_id,
            )
        except (KeyError, TypeError, ValueError, DocumentOperationError) as exc:
            if any(base.get(key) != value for key, value in expected_base.items()):
                raise StaleOperationError(expected_base) from exc
            raise InvalidEditorOperationError(str(exc), getattr(exc, "operation_id", None)) from exc
        except RepositoryConflictError as exc:
            raise StaleOperationError(expected_base) from exc
        except RepositoryError as exc:
            raise EditorPersistenceError(str(exc)) from exc
        return OperationCommit(before=latest, after=revision)
