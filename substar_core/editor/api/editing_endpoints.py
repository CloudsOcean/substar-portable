from __future__ import annotations

from typing import Any, Callable, Literal, Mapping

from fastapi import HTTPException
from pydantic import BaseModel, Field

from substar_core.editor.application import (
    EditingService,
    EditorPersistenceError,
    EmptyProjectError,
    InvalidEditorOperationError,
    StaleOperationError,
)
from substar_core.editor.ports import ProjectRepository


class DocumentOperationRequest(BaseModel):
    operation_id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    base: dict[str, Any]
    payload: dict[str, Any]
    retention: dict[str, Any] = Field(default_factory=dict)


class DocumentOperationBatchRequest(BaseModel):
    schema_version: Literal["substar.editor-operation-batch.v1"] = (
        "substar.editor-operation-batch.v1"
    )
    batch_id: str = Field(min_length=1)
    base: dict[str, Any]
    operations: list[DocumentOperationRequest] = Field(min_length=1, max_length=200)


RepositoryFactory = Callable[[str], ProjectRepository]
DeltaSerializer = Callable[[Any, Any], dict[str, Any]]


def _translate_commit_error(exc: Exception, *, batch: bool) -> HTTPException:
    if isinstance(exc, EmptyProjectError):
        return HTTPException(
            status_code=404,
            detail={"code": "empty_project", "message": "项目还没有文档版本"},
        )
    if isinstance(exc, StaleOperationError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "stale_operation",
                "message": "编辑基于旧版本，请刷新后重试",
                "latest": exc.latest,
            },
        )
    if isinstance(exc, InvalidEditorOperationError):
        detail: dict[str, Any] = {
            "code": "invalid_operation_batch" if batch else "invalid_operation",
            "message": str(exc),
        }
        if batch:
            detail["operation_id"] = exc.operation_id
        return HTTPException(status_code=422, detail=detail)
    if isinstance(exc, EditorPersistenceError):
        return HTTPException(
            status_code=400,
            detail={"code": "project_save_failed", "message": str(exc)},
        )
    raise exc


def commit_single_operation(
    project_id: str,
    payload: DocumentOperationRequest,
    *,
    repository_factory: RepositoryFactory,
    serialize_delta: DeltaSerializer,
) -> dict[str, Any]:
    try:
        commit = EditingService(repository_factory).commit_operation(
            project_id, payload.model_dump()
        )
    except (
        EmptyProjectError,
        StaleOperationError,
        InvalidEditorOperationError,
        EditorPersistenceError,
    ) as exc:
        raise _translate_commit_error(exc, batch=False) from exc
    return serialize_delta(commit.before, commit.after)


def commit_operation_batch(
    project_id: str,
    payload: DocumentOperationBatchRequest,
    *,
    repository_factory: RepositoryFactory,
    serialize_delta: DeltaSerializer,
) -> dict[str, Any]:
    try:
        commit = EditingService(repository_factory).commit_batch(
            project_id,
            base=payload.base,
            operations=[item.model_dump() for item in payload.operations],
            batch_id=payload.batch_id,
        )
    except (
        EmptyProjectError,
        StaleOperationError,
        InvalidEditorOperationError,
        EditorPersistenceError,
    ) as exc:
        raise _translate_commit_error(exc, batch=True) from exc
    result = serialize_delta(commit.before, commit.after)
    result["batch_id"] = payload.batch_id
    result["acknowledged_operation_ids"] = [
        item.operation_id for item in payload.operations
    ]
    return result
