from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.config import load_settings  # noqa: E402
from substar_core.full_pipeline import run_full_pipeline_from_ingest  # noqa: E402
from substar_core.pipeline import process_media  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Substar 从视频到直出双语 SRT")
    parser.add_argument("media", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stage1-chunk-seconds", type=float)
    args = parser.parse_args()
    media = args.media.resolve()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    settings = load_settings(include_secret=True)
    if args.segmentation_chunk_seconds is not None:
        settings["segmentation_chunk_seconds"] = int(args.segmentation_chunk_seconds)

    if not (output / "alignment.json").exists():
        def progress(message: str, fraction: float) -> None:
            print(f"ingest={fraction:.3f} {message}", flush=True)

        process_media(media, output, settings, progress)
    else:
        print("ingest=resumed final alignment files already exist", flush=True)

    run_full_pipeline_from_ingest(
        output,
        settings,
        lambda message, fraction: print(
            f"text_pipeline={fraction:.3f} {message}",
            flush=True,
        ),
        resume=args.resume,
    )
    print(f"result={output / 'substar_bilingual_final.srt'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
