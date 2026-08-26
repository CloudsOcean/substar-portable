from __future__ import annotations

import json
import hashlib
import os
import time
import uuid
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(
        path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(text, encoding=encoding)
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # On Windows a concurrent status reader or file scanner may
                # briefly deny rename/delete sharing.  Keep the atomic replace
                # contract and retry the same completed temporary file.
                if attempt == 7:
                    raise
                time.sleep(min(0.25, 0.015 * (2 ** attempt)))
    finally:
        if temporary.exists():
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
