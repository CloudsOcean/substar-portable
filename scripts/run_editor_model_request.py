from __future__ import annotations

import json
import sys
from typing import Any

import requests


def _response_json_utf8(response: requests.Response) -> Any:
    """Decode JSON bytes by the JSON UTF-8 contract, ignoring a bad charset header."""

    return json.loads(response.content.decode("utf-8-sig"))


def _response_text_utf8(response: requests.Response) -> str:
    return response.content.decode("utf-8-sig", errors="replace")


def _wire_json(value: Any) -> str:
    """Keep the child-to-parent pipe ASCII-only across Windows code pages."""

    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _emit(value: Any) -> None:
    print(_wire_json(value), flush=True)


def main() -> int:
    request: dict[str, Any] = json.loads(sys.stdin.read())
    try:
        response = requests.post(
            str(request["url"]),
            headers=dict(request["headers"]),
            # Send the provider body as real UTF-8 instead of letting requests
            # escape every non-ASCII character as ``\uXXXX``. Some compatible
            # endpoints reject otherwise valid escaped punctuation in message
            # content as a malformed surrogate sequence.
            data=json.dumps(request["payload"], ensure_ascii=False).encode("utf-8"),
            timeout=int(request["timeout"]),
        )
        body_text = _response_text_utf8(response)
        if not response.ok:
            _emit({
                "ok": False,
                "status": response.status_code,
                "error": body_text[:1000],
            })
            return 0
        try:
            body = _response_json_utf8(response)
        except (UnicodeDecodeError, ValueError):
            _emit({
                "ok": False,
                "status": response.status_code,
                "error": "provider response was not JSON",
            })
            return 0
        _emit({"ok": True, "body": body})
        return 0
    except requests.RequestException as exc:
        _emit({"ok": False, "status": 0, "error": str(exc)})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
