from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .artifacts import atomic_write_json


def stable_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def checkpoint_envelope(
    *,
    stage: str,
    fingerprint: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "_checkpoint": {
            "schema_version": "substar.checkpoint.v1",
            "stage": stage,
            "fingerprint": fingerprint,
        },
        "result": result,
    }


def read_checkpoint(
    path: Path,
    *,
    stage: str,
    fingerprint: str,
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    meta = payload.get("_checkpoint") if isinstance(payload, dict) else None
    if not isinstance(meta, dict):
        return None
    if (
        meta.get("schema_version") != "substar.checkpoint.v1"
        or meta.get("stage") != stage
        or meta.get("fingerprint") != fingerprint
        or not isinstance(payload.get("result"), dict)
    ):
        return None
    return payload["result"]


def write_checkpoint(
    path: Path,
    *,
    stage: str,
    fingerprint: str,
    result: dict[str, Any],
) -> None:
    atomic_write_json(
        path,
        checkpoint_envelope(
            stage=stage,
            fingerprint=fingerprint,
            result=result,
        ),
    )
