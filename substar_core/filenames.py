from __future__ import annotations

import re
from pathlib import Path


def safe_filename(name: str) -> str:
    cleaned = re.sub(
        r"[^0-9A-Za-z._()\-\u3400-\u9fff ]+",
        "_",
        Path(name).name,
    ).strip(" .")
    return cleaned or "media.bin"
