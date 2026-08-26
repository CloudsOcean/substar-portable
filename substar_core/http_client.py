"""Shared HTTP transport for provider-facing requests.

The pipeline can issue many requests concurrently.  Using ``requests.post``
for every call creates and tears down a fresh Session each time, which causes
avoidable TLS/socket churn on Windows.  This module keeps one stateless,
thread-safe urllib3-backed pool per Python process without changing the
configured request concurrency.
"""

from __future__ import annotations

import atexit
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter


_SESSION = requests.Session()
_ADAPTER = HTTPAdapter(
    pool_connections=16,
    pool_maxsize=64,
    max_retries=0,
    pool_block=True,
)
_SESSION.mount("http://", _ADAPTER)
_SESSION.mount("https://", _ADAPTER)


@atexit.register
def _close_session() -> None:
    _SESSION.close()


def request(method: str, url: str, **kwargs: Any) -> requests.Response:
    """Send a request through the shared provider connection pool."""

    return _SESSION.request(method, url, **kwargs)


def post(url: str, **kwargs: Any) -> requests.Response:
    return request("POST", url, **kwargs)


def get(url: str, **kwargs: Any) -> requests.Response:
    return request("GET", url, **kwargs)


def is_socket_permission_error(error: BaseException | None) -> bool:
    return bool(error and "10013" in str(error))


def retry_delay(error: BaseException | None, attempt: int) -> float:
    """Back off more deliberately when Windows rejects socket creation."""

    if is_socket_permission_error(error):
        return min(12.0, 2.0 * (2 ** max(0, attempt - 1)))
    return min(6.0, 0.75 * max(1, attempt))


def bounded_sleep(error: BaseException | None, attempt: int) -> None:
    time.sleep(retry_delay(error, attempt))
