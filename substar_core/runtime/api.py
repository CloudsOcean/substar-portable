from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from fastapi import APIRouter, Body, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute

from .model import (
    IdempotencyConflictError,
    InvalidTaskError,
    TASK_TYPES,
    TaskNotFoundError,
    TaskOwnershipError,
    TaskRuntimeError,
    TaskStateConflictError,
)
from .service import TaskService


_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True)
class ApiFailure:
    status_code: int
    code: str
    category: str
    message: str
    retryable: bool = False
    details: dict[str, Any] | None = None


class TaskTypeUnavailableError(TaskRuntimeError):
    pass


class TaskRuntimeUnavailableError(TaskRuntimeError):
    pass


def request_id(request: Request) -> str:
    current = getattr(request.state, "request_id", None)
    if isinstance(current, str) and current:
        return current
    supplied = request.headers.get("x-request-id", "").strip()
    if supplied and _REQUEST_ID.fullmatch(supplied):
        current = supplied
    else:
        current = f"req_{uuid.uuid4().hex}"
    request.state.request_id = current
    return current


class CanonicalTaskRoute(APIRoute):
    """Keep framework-level validation failures inside the frozen error envelope."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def canonical_handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError as exc:
                failure = ApiFailure(
                    422,
                    "request_validation_failed",
                    "validation",
                    "The task request is malformed.",
                    details={"errors": exc.errors()},
                )
                return _error_response(failure, request_id(request))

        return canonical_handler


router = APIRouter(route_class=CanonicalTaskRoute)


def _service(request: Request) -> TaskService:
    service = getattr(request.app.state, "task_service", None)
    if not isinstance(service, TaskService):
        raise TaskRuntimeUnavailableError("task runtime is not initialized")
    return service


def _available_task_types(request: Request) -> frozenset[str]:
    registry = getattr(request.app.state, "task_registry", None)
    if registry is not None:
        available = getattr(registry, "task_types", None)
        if callable(available):
            return frozenset(str(value) for value in available())
        if available is not None:
            return frozenset(str(value) for value in available)
    configured = getattr(request.app.state, "task_available_types", ())
    return frozenset(str(value) for value in configured)


def _validated_task_input(
    request: Request, task_type: str, value: Any
) -> dict[str, Any]:
    payload = _body_object(value)
    registry = getattr(request.app.state, "task_registry", None)
    if registry is None:
        return payload
    get_handler = getattr(registry, "get", None)
    if not callable(get_handler):
        return payload
    validated = get_handler(task_type).validate_input(payload)
    if not isinstance(validated, dict):
        validated = dict(validated)
    return validated


def _json_response(
    value: Any, status_code: int, current_request_id: str
) -> JSONResponse:
    return JSONResponse(
        value,
        status_code=status_code,
        headers={"X-Request-ID": current_request_id},
    )


def _error_response(failure: ApiFailure, current_request_id: str) -> JSONResponse:
    return _json_response(
        {
            "schema_version": "substar.api-error.v1",
            "code": failure.code,
            "category": failure.category,
            "message": failure.message,
            "retryable": failure.retryable,
            "request_id": current_request_id,
            "details": failure.details or {},
        },
        failure.status_code,
        current_request_id,
    )


def _failure(exc: Exception) -> ApiFailure:
    if isinstance(exc, TaskRuntimeUnavailableError):
        return ApiFailure(503, "task_runtime_unavailable", "configuration", str(exc), True)
    if isinstance(exc, TaskTypeUnavailableError):
        return ApiFailure(503, "task_type_unavailable", "configuration", str(exc), True)
    if isinstance(exc, TaskNotFoundError):
        return ApiFailure(404, "task_not_found", "not_found", str(exc))
    if isinstance(exc, IdempotencyConflictError):
        return ApiFailure(409, "idempotency_conflict", "conflict", str(exc))
    if isinstance(exc, TaskOwnershipError):
        return ApiFailure(409, "task_ownership_conflict", "conflict", str(exc), True)
    if isinstance(exc, TaskStateConflictError):
        return ApiFailure(409, "task_state_conflict", "conflict", str(exc))
    if isinstance(exc, InvalidTaskError):
        return ApiFailure(422, "task_validation_failed", "validation", str(exc))
    if isinstance(exc, TaskRuntimeError):
        return ApiFailure(500, "task_runtime_failed", "internal", str(exc), True)
    return ApiFailure(500, "internal_error", "internal", "Unexpected task runtime failure")


def _invoke(
    request: Request,
    operation: Callable[[], Any],
    *,
    success_status: int = 200,
) -> JSONResponse:
    current_request_id = request_id(request)
    try:
        value = operation()
    except Exception as exc:
        return _error_response(_failure(exc), current_request_id)
    return _json_response(value, success_status, current_request_id)


def _body_object(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InvalidTaskError("request body must be a JSON object")
    return payload


def _integer(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidTaskError(f"{field} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise InvalidTaskError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return parsed


@router.post("/api/projects/{project_id}/tasks", status_code=202)
def create_project_task(
    project_id: str,
    request: Request,
    payload: Any = Body(...),
) -> JSONResponse:
    def create() -> dict[str, Any]:
        body = _body_object(payload)
        task_type = str(body.get("task_type", "")).strip()
        if task_type not in TASK_TYPES:
            raise InvalidTaskError(f"unknown task type: {task_type!r}")
        raw_input = _body_object(body.get("input", {}))
        service = _service(request)
        idempotency_key = request.headers.get("idempotency-key")
        available = task_type in _available_task_types(request)
        if available:
            # Idempotency is defined over the handler's canonical input, not
            # the caller's incidental spelling (whitespace, path separators,
            # omitted defaults, and so on).  Looking up the raw body first can
            # turn an exact normalized replay into a false 409 conflict.
            task_input = _validated_task_input(request, task_type, raw_input)
        else:
            task_input = raw_input
        if idempotency_key is not None:
            replay = service.find_idempotent_task(
                task_type=task_type,
                input_schema=str(body.get("input_schema", "")),
                input_payload=task_input,
                project_id=project_id,
                parent_task_id=body.get("parent_task_id"),
                idempotency_key=idempotency_key,
                expected_revision_id=body.get("expected_revision_id"),
            )
            if replay is not None:
                return replay
        if not available:
            raise TaskTypeUnavailableError(
                f"task type is not registered: {task_type!r}"
            )
        return service.create_task(
            task_type=task_type,
            input_schema=str(body.get("input_schema", "")),
            input_payload=task_input,
            project_id=project_id,
            parent_task_id=body.get("parent_task_id"),
            idempotency_key=idempotency_key,
            expected_revision_id=body.get("expected_revision_id"),
            request_id=request_id(request),
        )

    return _invoke(request, create, success_status=202)


@router.get("/api/tasks")
def list_tasks(request: Request) -> JSONResponse:
    query = request.query_params

    def load() -> dict[str, Any]:
        states = [value for value in query.getlist("state") if value]
        return {
            "items": _service(request).list_tasks(
                project_id=query.get("project_id"),
                task_type=query.get("task_type"),
                states=states or None,
                parent_task_id=query.get("parent_task_id"),
                limit=_integer(query.get("limit", "100"), "limit", minimum=1, maximum=500),
            )
        }

    return _invoke(request, load)


@router.get("/api/tasks/{task_id}")
def get_task(task_id: str, request: Request) -> JSONResponse:
    return _invoke(request, lambda: _service(request).get_task(task_id))


@router.post("/api/tasks/{task_id}/cancel", status_code=202)
def cancel_task(task_id: str, request: Request) -> JSONResponse:
    return _invoke(
        request,
        lambda: _service(request).request_cancel(
            task_id, request_id=request_id(request)
        ),
        success_status=202,
    )


@router.post("/api/tasks/{task_id}/retry", status_code=202)
def retry_task(task_id: str, request: Request) -> JSONResponse:
    return _invoke(
        request,
        lambda: _service(request).retry(task_id, request_id=request_id(request)),
        success_status=202,
    )


@router.delete("/api/tasks/{task_id}")
def delete_task(task_id: str, request: Request) -> JSONResponse:
    """Dismiss a finished task card while preserving its project revisions."""

    return _invoke(request, lambda: _service(request).delete_task(task_id))


@router.get("/api/tasks/{task_id}/events")
def task_events(task_id: str, request: Request) -> JSONResponse:
    query = request.query_params

    def load() -> dict[str, Any]:
        service = _service(request)
        service.get_task(task_id)
        items = service.events(
            after=_integer(query.get("after", "0"), "after", minimum=0, maximum=2**63 - 1),
            task_id=task_id,
            limit=_integer(query.get("limit", "100"), "limit", minimum=1, maximum=1000),
        )
        previous = _integer(
            query.get("after", "0"), "after", minimum=0, maximum=2**63 - 1
        )
        return {
            "items": items,
            "next_event_id": items[-1]["event_id"] if items else previous,
        }

    return _invoke(request, load)


@router.get("/api/tasks/{task_id}/artifacts")
def task_artifacts(task_id: str, request: Request) -> JSONResponse:
    return _invoke(
        request,
        lambda: {"items": _service(request).list_artifacts(task_id)},
    )


def _event_cursor(request: Request) -> int:
    explicit = request.query_params.get("after")
    raw = explicit if explicit is not None else request.headers.get("last-event-id", "0")
    try:
        cursor = int(raw or 0)
    except (TypeError, ValueError) as exc:
        raise InvalidTaskError("event cursor must be a non-negative integer") from exc
    if cursor < 0:
        raise InvalidTaskError("event cursor must be a non-negative integer")
    return cursor


def encode_sse_event(event: dict[str, Any]) -> bytes:
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return (
        f"id: {int(event['event_id'])}\n"
        f"event: {event['event_type']}\n"
        f"data: {payload}\n\n"
    ).encode("utf-8")


async def stream_events(
    request: Request,
    service: TaskService,
    *,
    after: int,
    project_id: str | None = None,
    task_id: str | None = None,
    poll_seconds: float = 0.5,
    heartbeat_seconds: float = 15.0,
) -> AsyncIterator[bytes]:
    cursor = after
    loop = asyncio.get_running_loop()
    last_output = loop.time()
    while not await request.is_disconnected():
        events = await asyncio.to_thread(
            service.events,
            cursor,
            project_id=project_id,
            task_id=task_id,
            limit=250,
        )
        if events:
            for event in events:
                cursor = max(cursor, int(event["event_id"]))
                last_output = loop.time()
                yield encode_sse_event(event)
            continue
        if loop.time() - last_output >= heartbeat_seconds:
            last_output = loop.time()
            yield b": heartbeat\n\n"
        await asyncio.sleep(poll_seconds)


@router.get("/api/events", response_model=None)
def events_stream(request: Request) -> Response:
    current_request_id = request_id(request)
    try:
        cursor = _event_cursor(request)
        service = _service(request)
        project_id = request.query_params.get("project_id")
        task_id = request.query_params.get("task_id")
        selected_task = service.get_task(task_id) if task_id is not None else None
        if project_id is not None:
            service.list_tasks(project_id=project_id, limit=1)
        if (
            selected_task is not None
            and project_id is not None
            and selected_task.get("project_id") != project_id
        ):
            raise InvalidTaskError("task_id does not belong to project_id")
    except Exception as exc:
        return _error_response(_failure(exc), current_request_id)
    return StreamingResponse(
        stream_events(
            request,
            service,
            after=cursor,
            project_id=project_id,
            task_id=task_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Request-ID": current_request_id,
        },
    )
