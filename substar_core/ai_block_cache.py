from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from substar_core.artifacts import atomic_write_json


CACHE_SCHEMA = "substar.ai-block-cache.v1"


def fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_ai_block_cache(root: Path, key: str) -> dict[str, Any] | None:
    path = root / f"{key}.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != CACHE_SCHEMA:
        return None
    response = value.get("response")
    return dict(response) if isinstance(response, Mapping) else None


def save_ai_block_cache(root: Path, key: str, response: Mapping[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(root / f"{key}.json", {
        "schema_version": CACHE_SCHEMA,
        "fingerprint": key,
        "response": dict(response),
    })
