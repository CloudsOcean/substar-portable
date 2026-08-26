from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI
from starlette.requests import Request

from substar_core.runtime import RuntimeStore, TaskHandler, TaskRegistry, TaskService
from substar_core.runtime.api import (
    create_project_task,
    events_stream as events_stream_response,
    list_tasks,
    router,
    stream_events,
    task_events,
)


def make_request(
    app: FastAPI,
    *,
    method: str = "GET",
    path: str = "/",
    query: str = "",
    headers: dict[str, str] | None = None,
) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query.encode("ascii"),
            "headers": [
                (key.lower().encode("ascii"), value.encode("ascii"))
                for key, value in (headers or {}).items()
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8769),
            "app": app,
        }
    )


def response_json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


class TaskApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(Path(self.temporary.name) / "runtime.sqlite3")
        self.service = TaskService(self.store, "api-instance")
        self.app = FastAPI()
        self.app.state.task_service = self.service
        self.app.state.task_available_types = {"export"}
        self.app.include_router(router)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create(self, *, key: str = "create-export"):
        request = make_request(
            self.app,
            method="POST",
            path="/api/projects/project-1/tasks",
            headers={"Idempotency-Key": key, "X-Request-ID": "req-api-create"},
        )
        return create_project_task(
            "project-1",
            request,
            {
                "task_type": "export",
                "input_schema": "substar.export-input.v1",
                "input": {"track": "source"},
            },
        )

    def test_create_replay_list_and_polling_event_history(self) -> None:
        first_response = self.create()
        replay_response = self.create()
        first = response_json(first_response)
        replay = response_json(replay_response)

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(first["task_id"], replay["task_id"])
        self.assertEqual(first_response.headers["x-request-id"], "req-api-create")

        listed = response_json(
            list_tasks(
                make_request(self.app, path="/api/tasks", query="project_id=project-1")
            )
        )
        self.assertEqual([item["task_id"] for item in listed["items"]], [first["task_id"]])

        history = response_json(
            task_events(
                first["task_id"],
                make_request(
                    self.app,
                    path=f"/api/tasks/{first['task_id']}/events",
                    query="after=0",
                ),
            )
        )
        self.assertEqual(history["items"][0]["event_type"], "task.created")
        self.assertEqual(history["next_event_id"], history["items"][0]["event_id"])

    def test_unregistered_task_type_uses_canonical_error(self) -> None:
        response = create_project_task(
            "project-1",
            make_request(self.app, method="POST"),
            {
                "task_type": "transcription",
                "input_schema": "substar.transcription-input.v1",
                "input": {},
            },
        )
        payload = response_json(response)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["schema_version"], "substar.api-error.v1")
        self.assertEqual(payload["code"], "task_type_unavailable")
        self.assertTrue(payload["retryable"])

    def test_exact_idempotent_replay_survives_handler_unavailability(self) -> None:
        first = response_json(self.create(key="restart-window"))
        self.app.state.task_available_types = set()

        replay = self.create(key="restart-window")
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(response_json(replay)["task_id"], first["task_id"])

        conflict = create_project_task(
            "project-1",
            make_request(
                self.app,
                method="POST",
                headers={"Idempotency-Key": "restart-window"},
            ),
            {
                "task_type": "export",
                "input_schema": "substar.export-input.v1",
                "input": {"track": "changed"},
            },
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(response_json(conflict)["code"], "idempotency_conflict")

    def test_idempotency_compares_handler_normalized_input(self) -> None:
        registry = TaskRegistry()
        registry.register(
            TaskHandler(
                task_type="export",
                validate_input=lambda value: {
                    "track": str(value.get("track", "")).strip()
                },
                prepare=lambda _context: (_ for _ in ()).throw(
                    AssertionError("API creation must not start a worker")
                ),
            )
        )
        self.app.state.task_registry = registry
        request = make_request(
            self.app,
            method="POST",
            headers={"Idempotency-Key": "normalized-export"},
        )
        first = create_project_task(
            "project-1",
            request,
            {
                "task_type": "export",
                "input_schema": "substar.export-input.v1",
                "input": {"track": " source "},
            },
        )
        replay = create_project_task(
            "project-1",
            make_request(
                self.app,
                method="POST",
                headers={"Idempotency-Key": "normalized-export"},
            ),
            {
                "task_type": "export",
                "input_schema": "substar.export-input.v1",
                "input": {"track": "source"},
            },
        )
        conflict = create_project_task(
            "project-1",
            make_request(
                self.app,
                method="POST",
                headers={"Idempotency-Key": "normalized-export"},
            ),
            {
                "task_type": "export",
                "input_schema": "substar.export-input.v1",
                "input": {"track": "target"},
            },
        )

        self.assertEqual(response_json(first)["task_id"], response_json(replay)["task_id"])
        self.assertEqual(conflict.status_code, 409)

    def test_framework_body_validation_also_uses_canonical_error(self) -> None:
        async def send_malformed_request():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.post(
                    "/api/projects/project-1/tasks",
                    content=b"{",
                    headers={
                        "Content-Type": "application/json",
                        "X-Request-ID": "req-malformed",
                    },
                )

        response = asyncio.run(send_malformed_request())
        payload = response.json()

        self.assertEqual(response.status_code, 422)
        self.assertEqual(payload["schema_version"], "substar.api-error.v1")
        self.assertEqual(payload["code"], "request_validation_failed")
        self.assertEqual(response.headers["x-request-id"], "req-malformed")

    def test_sse_replays_committed_event_from_cursor(self) -> None:
        task = response_json(self.create(key="sse-export"))

        class ConnectedRequest:
            async def is_disconnected(self) -> bool:
                return False

        async def receive_one() -> bytes:
            stream = stream_events(
                ConnectedRequest(),  # type: ignore[arg-type]
                self.service,
                after=0,
                task_id=task["task_id"],
                poll_seconds=0.01,
            )
            try:
                return await anext(stream)
            finally:
                await stream.aclose()

        frame = asyncio.run(receive_one()).decode("utf-8")
        event_id = self.service.events(task_id=task["task_id"])[0]["event_id"]
        self.assertIn(f"id: {event_id}\n", frame)
        self.assertIn("event: task.created\n", frame)
        self.assertIn(f'"task_id":"{task["task_id"]}"', frame)

    def test_sse_rejects_invalid_filters_before_streaming_200(self) -> None:
        response = events_stream_response(
            make_request(
                self.app,
                path="/api/events",
                query="task_id=missing-task",
            )
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response_json(response)["code"], "task_not_found")

        invalid_project = events_stream_response(
            make_request(
                self.app,
                path="/api/events",
                query="project_id=contains%20spaces",
            )
        )
        self.assertEqual(invalid_project.status_code, 422)
        self.assertEqual(
            response_json(invalid_project)["code"], "task_validation_failed"
        )


if __name__ == "__main__":
    unittest.main()
