from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.pipeline import _json_safe, process_media


EVENT_PREFIX = "SUBSTAR_INGEST_EVENT "


def emit(event_type: str, **payload: Any) -> None:
    # Keep the wire format ASCII-only.  This is deliberate: the parent process
    # may run under a different Windows code page, while JSON escapes preserve
    # the original Chinese text after decoding.
    print(
        EVENT_PREFIX
        + json.dumps(_json_safe({"type": event_type, **payload}), ensure_ascii=True),
        flush=True,
    )


def _configure_stdio() -> None:
    """Use UTF-8 for diagnostics when this worker is launched on Windows."""

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run media ingest in an isolated worker process."
    )
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--job-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    _configure_stdio()
    args = parse_args()
    try:
        settings = json.loads(sys.stdin.read())
        if not isinstance(settings, dict):
            raise ValueError("worker settings must be a JSON object")
        files = process_media(
            args.media.resolve(),
            args.job_dir.resolve(),
            settings,
            lambda message, progress: emit(
                "progress", message=str(message), progress=float(progress)
            ),
        )
        emit("result", files=files)
        return 0
    except BaseException as exc:
        emit("error", message=str(exc), traceback=traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
