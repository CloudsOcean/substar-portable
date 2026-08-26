from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from substar_core.artifacts import atomic_write_json, atomic_write_text  # noqa: E402
from substar_core.config import load_settings  # noqa: E402
from substar_core.export import SubtitleExportMode, render_document_srt  # noqa: E402
from substar_core.storage import ProjectStore  # noqa: E402
from substar_core.editor.translation.contextual import run_contextual_translation  # noqa: E402
from substar_core.editor.translation.artifacts import (  # noqa: E402
    TRANSLATION_MANIFEST_FILENAME,
    TRANSLATION_PROGRESS_FILENAME,
    TRANSLATION_PROGRESS_SCHEMA,
    TRANSLATION_REVISION_FILENAME,
    TRANSLATION_STAGE_ID,
    TRANSLATION_SUBTITLE_FILENAME,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 Substar 正式 Contextual translation 翻译链路")
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-revision-id", required=True)
    parser.add_argument("--settings-file", type=Path)
    args = parser.parse_args()

    store = ProjectStore.open(args.job_dir / "project")
    source_revision = store.load_latest()
    if source_revision is None:
        raise RuntimeError("项目缺少可翻译的编辑版本")
    if source_revision.revision_id != args.expected_revision_id:
        raise RuntimeError("翻译所基于的编辑版本已变化")

    settings = load_settings(include_secret=True)
    if args.settings_file and args.settings_file.is_file():
        snapshot = json.loads(args.settings_file.read_text(encoding="utf-8"))
        if isinstance(snapshot, dict):
            secret = settings.get("translation_api_key", "")
            settings.update(snapshot)
            settings["translation_api_key"] = secret
    if not settings.get("translation_api_key"):
        raise RuntimeError("缺少翻译 API Key")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        args.output_dir / TRANSLATION_PROGRESS_FILENAME,
        {
            "schema_version": TRANSLATION_PROGRESS_SCHEMA,
            "stages": {
                TRANSLATION_STAGE_ID: {
                    "status": "running",
                    "planned": 1,
                    "accepted": 0,
                },
            },
        },
    )

    result = run_contextual_translation(
        args.job_dir,
        settings,
        artifact_dir=args.output_dir,
    )
    route = "contextual_translation"

    revision = store.load_latest()
    if revision is None or revision.revision_id != result["revision_id"]:
        raise RuntimeError("正式翻译结果未写入项目版本库")
    final_srt = render_document_srt(revision.document, SubtitleExportMode.AB_DOUBLE)
    atomic_write_text(args.output_dir / TRANSLATION_SUBTITLE_FILENAME, final_srt)
    atomic_write_json(
        args.output_dir / TRANSLATION_REVISION_FILENAME,
        {
            "translation_revision_id": revision.revision_id,
            "source_revision_id": args.expected_revision_id,
            "route": route,
            "prompt": result["prompt"],
        },
    )
    atomic_write_json(
        args.output_dir / TRANSLATION_PROGRESS_FILENAME,
        {
            "schema_version": TRANSLATION_PROGRESS_SCHEMA,
            "stages": {
                TRANSLATION_STAGE_ID: {
                    "status": "completed",
                    "planned": 1,
                    "accepted": 1,
                },
            },
        },
    )
    atomic_write_json(
        args.output_dir / TRANSLATION_MANIFEST_FILENAME,
        {
            "route": route,
            "source_revision_id": args.expected_revision_id,
            "result_revision_id": revision.revision_id,
            "source_language": result["source_language"],
            "target_language": result["target_language"],
            "prompt": result["prompt"],
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
