from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.config import load_settings  # noqa: E402
from substar_core.full_pipeline import run_full_pipeline_from_ingest  # noqa: E402
from substar_core.pipeline import process_media  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Substar ingest, segmentation, translation, and review."
    )
    parser.add_argument("media", type=Path)
    parser.add_argument("--job-dir", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    media = args.media.resolve()
    if not media.is_file():
        raise SystemExit(f"Media not found: {media}")
    settings = load_settings(include_secret=True)
    output_root = Path(settings["output_dir"])
    job_dir = args.job_dir or output_root / (
        time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    )
    job_dir.mkdir(parents=True, exist_ok=True)

    def progress(message: str, fraction: float) -> None:
        record = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "progress": round(float(fraction), 4),
            "message": message,
        }
        print(json.dumps(record, ensure_ascii=False), flush=True)
        (job_dir / "cli_status.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    process_media(
        media,
        job_dir,
        settings,
        lambda message, fraction: progress(message, fraction * 0.55),
    )
    run_full_pipeline_from_ingest(
        job_dir,
        settings,
        progress,
        resume=args.resume,
    )
    print(
        json.dumps(
            {"status": "completed", "job_dir": str(job_dir)},
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()

