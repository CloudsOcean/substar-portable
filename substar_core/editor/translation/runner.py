from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from substar_core.ai_progress import ai_progress
from substar_core.artifacts import atomic_write_json, atomic_write_text
from substar_core.editor.translation.artifacts import (
    TRANSLATION_MANIFEST_FILENAME,
    TRANSLATION_PROGRESS_FILENAME,
    TRANSLATION_PROGRESS_SCHEMA,
    TRANSLATION_REVISION_FILENAME,
    TRANSLATION_STAGE_ID,
    TRANSLATION_SUBTITLE_FILENAME,
)
from substar_core.editor.translation.contextual import run_contextual_translation
from substar_core.editor.translation.result_policy import (
    source_rows,
    translation_problem_cue_ids,
)
from substar_core.export import SubtitleExportMode, render_document_srt
from substar_core.storage import ProjectStore


def execute_translation(
    *,
    project_root: Path,
    artifact_directory: Path,
    expected_revision_id: str,
    settings: Mapping[str, Any],
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    store = ProjectStore.open(project_root / "project")
    source_revision = store.load_latest()
    if source_revision is None or source_revision.revision_id != expected_revision_id:
        raise RuntimeError("翻译所基于的编辑版本已变化")
    artifact_directory.mkdir(parents=True, exist_ok=True)
    tracker = {
        "planned": 0,
        "completed": 0,
        "repair_planned": 0,
        "repair_completed": 0,
        "repair_accepted": 0,
    }

    def write_progress(
        phase: str,
        *,
        completed: int = 0,
        planned: int = 0,
        repair_planned: int = 0,
        repair_accepted: int = 0,
    ) -> None:
        tracker["planned"] = max(tracker["planned"], int(planned))
        tracker["completed"] = max(tracker["completed"], int(completed))
        tracker["repair_planned"] = max(tracker["repair_planned"], int(repair_planned))
        if phase == "repair":
            tracker["repair_completed"] = int(completed)
            tracker["completed"] = tracker["planned"]
        tracker["repair_accepted"] = max(tracker["repair_accepted"], int(repair_accepted))
        primary_accepted = max(0, tracker["planned"] - tracker["repair_planned"])
        value = ai_progress(
            kind="translation",
            phase=phase,
            unit_label="个意义组",
            planned=tracker["planned"],
            completed=tracker["completed"],
            accepted=primary_accepted,
            failed=max(0, tracker["completed"] - primary_accepted),
            repair_planned=tracker["repair_planned"],
            repair_completed=tracker["repair_completed"],
            repair_accepted=tracker["repair_accepted"],
            repair_failed=max(0, tracker["repair_completed"] - tracker["repair_accepted"]),
        )
        atomic_write_json(
            artifact_directory / TRANSLATION_PROGRESS_FILENAME,
            {
                "schema_version": TRANSLATION_PROGRESS_SCHEMA,
                "ai_progress": value,
                "stages": {
                    TRANSLATION_STAGE_ID: {
                        "status": "completed" if phase == "completed" else phase,
                        "planned": tracker["planned"],
                        "accepted": tracker["planned"] - max(
                            0, tracker["repair_planned"] - tracker["repair_accepted"]
                        ),
                    }
                },
            },
        )
        if progress_callback is not None:
            progress_callback(value)

    write_progress("planning")
    result = run_contextual_translation(
        project_root,
        dict(settings),
        artifact_dir=artifact_directory,
        progress_callback=write_progress,
    )
    write_progress(
        "publishing",
        completed=tracker["planned"],
        planned=tracker["planned"],
        repair_planned=tracker["repair_planned"],
        repair_accepted=tracker["repair_accepted"],
    )
    revision = store.load_latest()
    if revision is None or revision.revision_id != result["revision_id"]:
        raise RuntimeError("正式翻译结果未写入项目版本库")
    atomic_write_text(
        artifact_directory / TRANSLATION_SUBTITLE_FILENAME,
        render_document_srt(revision.document, SubtitleExportMode.AB_DOUBLE),
    )
    atomic_write_json(
        artifact_directory / TRANSLATION_REVISION_FILENAME,
        {
            "schema_version": "substar.translation-revision.v2",
            "translation_revision_id": revision.revision_id,
            "source_revision_id": expected_revision_id,
            "route": "contextual_translation",
            "prompt": result["prompt"],
        },
    )
    problem_cue_ids = translation_problem_cue_ids(revision.document)
    rows = source_rows(source_revision.document)
    atomic_write_json(
        artifact_directory / "result.json",
        {
            "schema_version": "substar.translation-result.v2",
            "source_revision_id": expected_revision_id,
            "result_revision_id": revision.revision_id,
            "problem_cue_ids": problem_cue_ids,
            "source_rows": rows,
        },
    )
    atomic_write_json(
        artifact_directory / TRANSLATION_MANIFEST_FILENAME,
        {
            "schema_version": "substar.translation-manifest.v2",
            "route": "contextual_translation",
            "source_revision_id": expected_revision_id,
            "result_revision_id": revision.revision_id,
            "source_language": result["source_language"],
            "target_language": result["target_language"],
            "mapping_mode": result["mapping_mode"],
            "prompt": result["prompt"],
            "problem_cue_ids": problem_cue_ids,
        },
    )
    write_progress(
        "completed",
        completed=tracker["planned"],
        planned=tracker["planned"],
        repair_planned=tracker["repair_planned"],
        repair_accepted=tracker["repair_accepted"],
    )
    return {
        "result_revision_id": revision.revision_id,
        "problem_cue_ids": problem_cue_ids,
        "planned": tracker["planned"],
        "repair_planned": tracker["repair_planned"],
        "repair_completed": tracker["repair_completed"],
        "repair_accepted": tracker["repair_accepted"],
    }
