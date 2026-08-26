from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

import app as backend


def _request(instance_id: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/runtime/shutdown",
            "raw_path": b"/api/runtime/shutdown",
            "query_string": b"",
            "headers": [
                (b"x-substar-instance-id", instance_id.encode("ascii")),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8769),
        }
    )


class RuntimeHttpTests(unittest.TestCase):
    def test_shutdown_requires_the_live_instance_identity(self) -> None:
        with patch.object(backend, "APP_INSTANCE_ID", "instance-a"):
            with self.assertRaises(HTTPException) as raised:
                backend.request_runtime_shutdown(_request("instance-b"))
        self.assertEqual(raised.exception.status_code, 403)

    def test_shutdown_requests_uvicorn_exit_after_identity_check(self) -> None:
        class Server:
            should_exit = False

        server = Server()
        with (
            patch.object(backend, "APP_INSTANCE_ID", "instance-a"),
            patch.object(backend, "_UVICORN_SERVER", server),
        ):
            response = backend.request_runtime_shutdown(_request("instance-a"))
        self.assertTrue(response["accepted"])
        self.assertEqual(response["instance_id"], "instance-a")
        self.assertTrue(server.should_exit)

    def test_shutdown_never_acknowledges_when_server_control_is_unavailable(self) -> None:
        with (
            patch.object(backend, "APP_INSTANCE_ID", "instance-a"),
            patch.object(backend, "_UVICORN_SERVER", None),
            self.assertRaises(HTTPException) as raised,
        ):
            backend.request_runtime_shutdown(_request("instance-a"))
        self.assertEqual(raised.exception.status_code, 503)

    def test_health_reports_runtime_readiness_separately_from_liveness(self) -> None:
        original = getattr(backend.app.state, "task_service", None)
        try:
            backend.app.state.task_service = object()
            response = backend.runtime_health()
        finally:
            backend.app.state.task_service = original
        self.assertEqual(response["status"], "ready")
        self.assertTrue(response["task_runtime"])


if __name__ == "__main__":
    unittest.main()
