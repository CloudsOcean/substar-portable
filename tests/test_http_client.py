from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from substar_core import http_client


class SharedHttpClientTests(unittest.TestCase):
    def test_provider_requests_use_the_process_pool(self) -> None:
        response = Mock()
        with patch.object(http_client._SESSION, "request", return_value=response) as request:
            result = http_client.post(
                "https://llm.example/chat/completions",
                json={"ok": True},
                timeout=30,
            )

        self.assertIs(result, response)
        request.assert_called_once_with(
            "POST",
            "https://llm.example/chat/completions",
            json={"ok": True},
            timeout=30,
        )

    def test_socket_backoff_is_longer_without_changing_concurrency(self) -> None:
        self.assertEqual(http_client._ADAPTER._pool_maxsize, 64)
        self.assertTrue(http_client.is_socket_permission_error(OSError("[WinError 10013]")))
        self.assertGreater(
            http_client.retry_delay(OSError("[WinError 10013]"), 1),
            http_client.retry_delay(None, 1),
        )


if __name__ == "__main__":
    unittest.main()
