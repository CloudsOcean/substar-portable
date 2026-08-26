from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

from substar_core.runtime import TaskService
from substar_core.segmentation import (
    SEGMENTATION_INPUT_SCHEMA,
    build_segmentation_request,
    sha256_file,
    sha256_tree,
)
from substar_core.transcription import TRANSCRIPTION_INPUT_SCHEMA


PROMPT_SNAPSHOT_RELATIVE = "task_inputs/segmentation_prompts"


def freeze_prompt_snapshot(
    project_directory: Path, application_root: Path
) -> dict[str, Any]:
    source = (application_root / "prompts").resolve()
    destination = (project_directory / PROMPT_SNAPSHOT_RELATIVE).resolve()
    if project_directory.resolve() not in destination.parents:
        raise ValueError("prompt snapshot destination escapes the project")
    if not source.is_dir():
        raise ValueError("packaged prompt directory is missing")
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
    digest, count = sha256_tree(destination)
    if count < 1:
        raise ValueError("prompt snapshot is empty")
    return {
        "relative_path": PROMPT_SNAPSHOT_RELATIVE,
        "sha256": digest,
        "file_count": count,
    }


def reference_document_snapshot(
    project_directory: Path, reference_path: Path | None
) -> dict[str, Any] | None:
    if reference_path is None:
        return None
    project = project_directory.resolve()
    source = reference_path.resolve()
    if project not in source.parents or not source.is_file():
        raise ValueError("reference document is outside the project")
    return {
        "relative_path": source.relative_to(project).as_posix(),
        "sha256": sha256_file(source),
        "byte_size": source.stat().st_size,
    }


def create_subtitle_creation_graph(
    *,
    service: TaskService,
    project_id: str,
    transcription_request: Mapping[str, Any],
    segmentation_enabled: bool,
    language: str,
    reference_document: Mapping[str, Any] | None,
    prompt_snapshot: Mapping[str, Any],
    glossary_snapshot: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    request_id = f"project-creation-{project_id}"
    transcription = service.create_task(
        task_type="transcription",
        input_schema=TRANSCRIPTION_INPUT_SCHEMA,
        input_payload=transcription_request,
        project_id=project_id,
        idempotency_key=f"project-creation:transcription:{project_id}",
        request_id=request_id,
    )
    segmentation_request = build_segmentation_request(
        transcription_task_id=str(transcription["task_id"]),
        transcription_input_fingerprint=str(transcription_request["input_fingerprint"]),
        media_sha256=str(transcription_request["media"]["sha256"]),
        source_asset_id=project_id,
        language=language,
        segmentation_enabled=segmentation_enabled,
        reference_document=reference_document,
        prompt_snapshot=prompt_snapshot,
        glossary_snapshot=glossary_snapshot,
        settings=settings,
    )
    segmentation = service.create_task(
        task_type="segmentation",
        input_schema=SEGMENTATION_INPUT_SCHEMA,
        input_payload=segmentation_request,
        project_id=project_id,
        parent_task_id=str(transcription["task_id"]),
        idempotency_key=f"project-creation:segmentation:{project_id}",
        request_id=request_id,
        depends_on_task_ids=(str(transcription["task_id"]),),
    )
    return transcription, segmentation
