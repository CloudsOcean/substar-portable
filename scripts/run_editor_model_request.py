from __future__ import annotations

import json
import sys
from typing import Any

import requests


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
        body_text = response.text
        if not response.ok:
            print(json.dumps({
                "ok": False,
                "status": response.status_code,
                "error": body_text[:1000],
            }, ensure_ascii=False))
            return 0
        try:
            body = response.json()
        except ValueError:
            print(json.dumps({
                "ok": False,
                "status": response.status_code,
                "error": "provider response was not JSON",
            }, ensure_ascii=False))
            return 0
        print(json.dumps({"ok": True, "body": body}, ensure_ascii=False))
        return 0
    except requests.RequestException as exc:
        print(json.dumps({"ok": False, "status": 0, "error": str(exc)}, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
