from __future__ import annotations

from array import array
import concurrent.futures
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping
from urllib.parse import quote

import json
import mimetypes
import shutil
import sys
import uuid
import wave
import tempfile
import zipfile

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from substar_core.artifacts import atomic_write_json
from substar_core.chinese_script import convert_chinese_script
from substar_core.config import load_settings
from substar_core.glossary import active_glossary, load_glossary, normalize_entry, save_glossary
from substar_core.glossary_xlsx import XLSX_MEDIA_TYPE, glossary_xlsx_bytes
from substar_core.prompt_registry import render_prompt, source_language_for_text
from substar_core.manuscript_matching import (
    ManuscriptMatchError,
    editor_reference_operations,
    extract_reference_text,
)
from substar_core.media import WAVEFORM_WINDOW_CACHE, prepare_playback_media, smart_forward_snap
from substar_core.stage2 import Stage2Error, call_translation_model
from substar_core.domain import (
    ChangeKind,
    ChangeProvenance,
    DocumentProperties,
    DocumentValidationError,
    EDITOR_DOCUMENT_SCHEMA,
    EditorDocument,
)
from substar_core.document_operations import (
    DocumentOperationError,
    apply_document_operation,
)
from substar_core.editor.application import RevisionService
from substar_core.editor.api import (
    DocumentOperationBatchRequest,
    DocumentOperationRequest,
    commit_operation_batch,
    commit_single_operation,
)
from substar_core.editor.domain.cue_ordering import canonicalize_document_cues
from substar_core.editor.infrastructure import SQLiteProjectRepository
from substar_core.editor.tasks.contracts import EditorAiTaskKind, EditorAiTaskState
from substar_core.editor.tasks.repository import (
    EditorAiTaskCancelled,
    EditorAiTaskConflict,
    assert_editor_write_allowed,
    finish_task as finish_editor_ai_task,
    load_task as load_editor_ai_task,
    raise_if_task_cancelled,
    request_task_cancellation,
    start_task as start_editor_ai_task,
    current_task_id as current_editor_ai_task_id,
    task_context as editor_ai_task_context,
)
from substar_core.storage import (
    ProjectConflictError,
    ProjectIntegrityError,
    ProjectStore,
    ProjectStoreError,
)
from substar_core.validation import ValidationPolicy, validate_revision
from substar_core.export import SubtitleExportMode, render_document_srt
from substar_core.editor.translation.service import (
    TranslationTaskError,
    cancel_translation_task,
    create_translation_task,
    load_translation_status,
)
from substar_core.project_exchange import (
    ProjectExchangeError,
    apply_external_generation_checkpoint,
    apply_external_prooftranslation,
    apply_external_split,
    external_edit_files,
    external_generation_files,
    external_prooftranslation_files,
    external_split_files,
    export_subtitle_project,
    import_subtitle_project,
    inspect_external_generation_checkpoint,
    inspect_external_prooftranslation,
    inspect_external_split,
    write_bytes_zip,
)
from substar_core.task_info import load_task_info, save_task_info, task_info_settings
router = APIRouter(prefix="/api", tags=["editor"])
PROJECT_DIRECTORY = "project"


class SaveDocumentRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)
    document: dict[str, Any]
    operation: str = "editor_save"


class CompleteDocumentRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)
    complete: bool


class ValidateDocumentRequest(BaseModel):
    source_hard_limit: int = Field(default=55, ge=1)
    target_hard_limit: int = Field(default=24, ge=1)
    count_spaces: bool = True
    count_punctuation: bool = True


class ProjectTaskInfoRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    language: Literal["Auto", "mixed", "zh", "zh-CN", "en", "ja", "ko"]
    target_language_mode: Literal["zh-CN", "en", "ja", "ko"]
    source_hard_limit: int = Field(ge=1, le=500)
    target_hard_limit: int = Field(ge=1, le=500)


class TranslationStartRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)
    workers: int = Field(default=3, ge=1, le=256)
    source_language: Literal["Auto", "mixed", "zh-CN", "en", "ja", "ko"]
    target_language: Literal["zh-CN", "en", "ja", "ko"]


class BatchReplacement(BaseModel):
    token_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    expected_text: str | None = None


class BatchReplaceRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    replacements: list[BatchReplacement] = Field(min_length=1)
    origin: Literal["manual", "ai_calibration", "reference_manuscript"] = "manual"
    metadata: dict[str, Any] = Field(default_factory=dict)


class CheckpointRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)
    label: str = Field(default="", max_length=120)


class RestoreRevisionRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    navigation: Literal["undo", "redo", "direct"] = "direct"
    undo_revision_ids: list[str] = Field(default_factory=list, max_length=1000)
    redo_revision_ids: list[str] = Field(default_factory=list, max_length=1000)


class SmartForwardSnapRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)


class PresentationRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    upper_punctuation: Literal["remove", "space"] | None = None
    lower_punctuation: Literal["remove", "space"] | None = None
    display_order: Literal["source_above_target", "target_above_source"] | None = None
    upper_remove: str | None = Field(default=None, max_length=128)
    upper_space: str | None = Field(default=None, max_length=128)
    lower_remove: str | None = Field(default=None, max_length=128)
    lower_space: str | None = Field(default=None, max_length=128)


class ScriptConversionRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)
    target: Literal["original", "simplified", "traditional", "traditional_tw", "traditional_hk"]


class AiCalibrationRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)


class AiReviewRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)
    instruction: str = Field(default="", max_length=4000)


class ReviewIssueStatusRequest(BaseModel):
    status: Literal["open", "dismissed", "resolved"]


class TutorialStageRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)


def _projects_root() -> Path:
    return Path(load_settings(include_secret=False)["output_dir"]).resolve()


def _safe_project_id(project_id: str) -> str:
    if not project_id or Path(project_id).name != project_id:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_project_id", "message": "项目 ID 无效"},
        )
    return project_id


def project_store_path(project_id: str, *, projects_root: Path | None = None) -> Path:
    safe_id = _safe_project_id(project_id)
    root = (projects_root or _projects_root()).resolve()
    path = (root / safe_id / PROJECT_DIRECTORY).resolve()
    if root not in path.parents:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_project_id", "message": "项目路径无效"},
        )
    return path


def open_project_store(project_id: str) -> ProjectStore:
    path = project_store_path(project_id)
    try:
        return ProjectStore.open(path)
    except ProjectIntegrityError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "project_integrity_error", "message": str(exc)},
        ) from exc
    except ProjectStoreError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "project_not_found", "message": str(exc)},
        ) from exc


def project_job_path(project_id: str) -> Path:
    return project_store_path(project_id).parent


def _tutorial_project(project_id: str) -> dict[str, Any] | None:
    path = project_job_path(project_id) / "tutorial_project.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail={
            "code": "tutorial_project_invalid", "message": str(exc)
        }) from exc
    allowed = {
        "substar.tutorial-project.v1": {"reference-script-v1"},
        "substar.tutorial-project.v2": {"reference-script-v1", "advanced-ai-v1"},
    }
    if (
        not isinstance(value, dict)
        or value.get("case_id") not in allowed.get(str(value.get("schema_version")), set())
        or not str(value.get("baseline_revision_id", ""))
    ):
        raise HTTPException(status_code=500, detail={
            "code": "tutorial_project_invalid", "message": "教程项目清单无效"
        })
    return value


def _tutorial_examples_root() -> Path:
    return Path(__file__).resolve().parents[2] / "assets" / "examples" / "tutorials"


def _tutorial_example(case_id: str) -> tuple[Path, dict[str, Any]]:
    directory_name = {"reference-script-v1": "beginner", "advanced-ai-v1": "advanced-ai"}.get(case_id)
    if directory_name is None:
        raise HTTPException(status_code=404, detail={
            "code": "tutorial_example_not_found", "message": "未知的教程案例"
        })
    root = (_tutorial_examples_root() / directory_name).resolve()
    try:
        value = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise HTTPException(status_code=500, detail={
            "code": "tutorial_example_invalid", "message": str(exc)
        }) from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "substar.tutorial-example.v1"
        or value.get("case_id") != case_id
        or not isinstance(value.get("assets"), dict)
    ):
        raise HTTPException(status_code=500, detail={
            "code": "tutorial_example_invalid", "message": "教程案例清单无效"
        })
    return root, value


def _load_tutorial_document(root: Path, manifest: Mapping[str, Any], stage: str) -> EditorDocument:
    filename = str(manifest["assets"].get(stage, ""))
    path = (root / filename).resolve()
    if not filename or root not in path.parents:
        raise HTTPException(status_code=500, detail={
            "code": "tutorial_example_invalid", "message": f"教程缺少 {stage} 阶段"
        })
    try:
        raw = path.read_bytes()
        expected = str(manifest.get("sha256", {}).get(filename, ""))
        if expected and sha256(raw).hexdigest() != expected:
            raise ValueError(f"{filename} 摘要不匹配")
        return EditorDocument.from_dict(json.loads(raw.decode("utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, DocumentValidationError) as exc:
        raise HTTPException(status_code=500, detail={
            "code": "tutorial_example_invalid", "message": str(exc)
        }) from exc


def _project_name_for_hotwords(project_id: str) -> str:
    job_dir = project_job_path(project_id)
    try:
        manifest = json.loads((job_dir / "run_manifest.json").read_text(encoding="utf-8"))
        configuration = manifest.get("configuration")
        if isinstance(configuration, dict) and str(configuration.get("project_name", "")).strip():
            return str(configuration["project_name"]).strip()
    except (OSError, json.JSONDecodeError, UnicodeError, AttributeError):
        pass
    try:
        status = json.loads((job_dir / "creation_state.json").read_text(encoding="utf-8"))
        return str(status.get("display_name") or project_id).strip()
    except (OSError, json.JSONDecodeError, UnicodeError, AttributeError):
        return project_id


def _collect_generated_hotwords(project_id: str, project_name: str) -> list[dict[str, Any]]:
    path = project_job_path(project_id) / "asr_enhancement.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return active_glossary(project_name)
    generated = payload.get("hotwords") if isinstance(payload, dict) else None
    if not isinstance(generated, list):
        return active_glossary(project_name)
    existing = load_glossary()
    by_key = {
        (
            str(item.get("scope", "global")),
            str(item.get("project", "")).casefold(),
            str(item.get("source", "")).casefold(),
        ): item
        for item in existing
    }
    changed = False
    for item in generated:
        if not isinstance(item, dict) or not str(item.get("text") or item.get("source") or "").strip():
            continue
        text = str(item.get("text") or item.get("source")).strip()
        key = ("project", project_name.casefold(), text.casefold())
        previous = by_key.get(key)
        candidate = normalize_entry(
            {
                "id": item.get("id") or (previous or {}).get("id", ""),
                "source": text,
                "standard_source": item.get("standard_source") or text,
                "target": item.get("target", ""),
                "aliases": item.get("aliases", []),
                "type": item.get("type", "other"),
                "scope": "project",
                "project": project_name,
                "enabled": True,
                "hotword_weight": item.get("weight", item.get("hotword_weight", 4)),
                "notes": item.get("notes", "ASR 增强候选热词"),
            }
        )
        if by_key.get(key) != candidate:
            by_key[key] = candidate
            changed = True
    if changed:
        save_glossary(list(by_key.values()))
    return active_glossary(project_name)


def _editor_task_path(project_id: str, kind: str) -> Path:
    return project_job_path(project_id) / "editor_tasks" / f"{kind}.json"


def _write_editor_task(
    project_id: str, kind: str, *, status: str, progress: float,
    message: str, error: str = "", task_id: str = "",
) -> dict[str, Any]:
    path = _editor_task_path(project_id, kind)
    previous: dict[str, Any] = {}
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    value = {
        "schema_version": "substar.editor-task.v1",
        "task_id": task_id or str(previous.get("task_id") or f"{kind}_{uuid.uuid4().hex[:16]}"),
        "project_id": project_id,
        "kind": kind,
        "status": status,
        "progress": max(0.0, min(1.0, float(progress))),
        "message": message,
        "error": error,
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
        "finished_at": now if status in {"completed", "failed", "cancelled"} else None,
    }
    atomic_write_json(path, value)
    return value


def _project_document_payload(document: EditorDocument) -> dict[str, Any]:
    """Serialize one revision with its non-destructive script projection."""
    value = document.to_dict()
    target = document.properties.script_projection
    if target == "original":
        return value
    for token in value.get("display_tokens", []):
        token["text"] = convert_chinese_script(str(token.get("text", "")), target)
    for cue in value.get("cues", []):
        translation = cue.get("target")
        if isinstance(translation, dict):
            translation["target_text"] = convert_chinese_script(
                str(translation.get("target_text", "")), target
            )
    return value


def revision_payload(revision: Any) -> dict[str, Any]:
    return {
        "revision_id": revision.revision_id,
        "revision_number": revision.revision_number,
        "parent_revision_id": revision.parent_revision_id,
        "created_at": revision.created_at,
        "document_hash": revision.document_hash or revision.document.content_hash(),
        "document": _project_document_payload(revision.document),
    }


def _revision_id(revision: Any) -> str:
    """Read the id from either a domain revision or an API payload.

    Internal save endpoints intentionally return the serialized payload, while
    storage returns a ``DocumentRevision``.  Keeping this boundary explicit
    prevents post-save audit code from mixing the two representations.
    """
    value = revision.get("revision_id") if isinstance(revision, Mapping) else getattr(revision, "revision_id", "")
    value = str(value or "").strip()
    if not value:
        raise ValueError("revision payload is missing revision_id")
    return value


def _entity_delta(
    before: tuple[Any, ...], after: tuple[Any, ...], *, id_attribute: str
) -> dict[str, Any]:
    before_by_id = {getattr(item, id_attribute): item for item in before}
    after_by_id = {getattr(item, id_attribute): item for item in after}
    return {
        "upsert": [
            item.to_dict()
            for item in after
            if before_by_id.get(getattr(item, id_attribute)) != item
        ],
        "remove": [item_id for item_id in before_by_id if item_id not in after_by_id],
    }


def _ordered_entity_delta(
    before: tuple[Any, ...], after: tuple[Any, ...], *, id_attribute: str
) -> dict[str, Any]:
    delta = _entity_delta(before, after, id_attribute=id_attribute)
    before_ids = [getattr(item, id_attribute) for item in before]
    after_ids = [getattr(item, id_attribute) for item in after]
    prefix = 0
    while (
        prefix < len(before_ids)
        and prefix < len(after_ids)
        and before_ids[prefix] == after_ids[prefix]
    ):
        prefix += 1
    suffix = 0
    while (
        suffix < len(before_ids) - prefix
        and suffix < len(after_ids) - prefix
        and before_ids[-1 - suffix] == after_ids[-1 - suffix]
    ):
        suffix += 1
    order_splice = None
    if before_ids != after_ids:
        before_end = len(before_ids) - suffix if suffix else len(before_ids)
        after_end = len(after_ids) - suffix if suffix else len(after_ids)
        order_splice = {
            "start": prefix,
            "delete_count": before_end - prefix,
            "insert_ids": after_ids[prefix:after_end],
        }
    return {**delta, "order_splice": order_splice}


def _cue_delta(before: tuple[Any, ...], after: tuple[Any, ...]) -> dict[str, Any]:
    before_by_id = {item.cue_id: item for item in before}
    after_by_id = {item.cue_id: item for item in after}

    def without_index(item: Any) -> dict[str, Any]:
        value = item.to_dict()
        value.pop("index", None)
        return value

    before_ids = [item.cue_id for item in before]
    after_ids = [item.cue_id for item in after]
    prefix = 0
    while (
        prefix < len(before_ids)
        and prefix < len(after_ids)
        and before_ids[prefix] == after_ids[prefix]
    ):
        prefix += 1
    suffix = 0
    while (
        suffix < len(before_ids) - prefix
        and suffix < len(after_ids) - prefix
        and before_ids[-1 - suffix] == after_ids[-1 - suffix]
    ):
        suffix += 1
    order_splice = None
    if before_ids != after_ids:
        before_end = len(before_ids) - suffix if suffix else len(before_ids)
        after_end = len(after_ids) - suffix if suffix else len(after_ids)
        order_splice = {
            "start": prefix,
            "delete_count": before_end - prefix,
            "insert_ids": after_ids[prefix:after_end],
        }
    return {
        "upsert": [
            item.to_dict()
            for item in after
            if item.cue_id not in before_by_id
            or without_index(before_by_id[item.cue_id]) != without_index(item)
        ],
        "remove": [item_id for item_id in before_by_id if item_id not in after_by_id],
        "order_splice": order_splice,
    }


def revision_delta_payload(before: Any, after: Any) -> dict[str, Any]:
    before_document = before.document
    after_document = after.document
    before_changes = before_document.changes
    changes_append = (
        after_document.changes[len(before_changes) :]
        if after_document.changes[: len(before_changes)] == before_changes
        else after_document.changes
    )
    value = {
        "schema_version": "substar.editor-delta.v1",
        "base_revision_id": before.revision_id,
        "revision_id": after.revision_id,
        "revision_number": after.revision_number,
        "parent_revision_id": after.parent_revision_id,
        "created_at": after.created_at,
        "document_id": after_document.document_id,
        "document_hash": after.document_hash or after_document.content_hash(),
        "properties": (
            after_document.properties.to_dict()
            if before_document.properties != after_document.properties
            else None
        ),
        "presentation": (
            after_document.presentation.to_dict()
            if before_document.presentation != after_document.presentation
            else None
        ),
        "source_tokens": _entity_delta(
            before_document.source_tokens,
            after_document.source_tokens,
            id_attribute="token_id",
        ),
        "display_tokens": _ordered_entity_delta(
            before_document.display_tokens,
            after_document.display_tokens,
            id_attribute="token_id",
        ),
        "cues": _cue_delta(before_document.cues, after_document.cues),
        "groups": _entity_delta(
            before_document.groups,
            after_document.groups,
            id_attribute="group_id",
        ),
        "changes_append": [item.to_dict() for item in changes_append],
        "changes_replaced": after_document.changes[: len(before_changes)] != before_changes,
    }
    target = after_document.properties.script_projection
    if target != "original":
        for token in value["display_tokens"]["upsert"]:
            token["text"] = convert_chinese_script(str(token.get("text", "")), target)
        for cue in value["cues"]["upsert"]:
            translation = cue.get("target")
            if isinstance(translation, dict):
                translation["target_text"] = convert_chinese_script(
                    str(translation.get("target_text", "")), target
                )
    return value


@router.get("/projects")
def list_projects() -> dict[str, Any]:
    root = _projects_root()
    projects: list[dict[str, Any]] = []
    if root.is_dir():
        for directory in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
            try:
                manifest = directory / PROJECT_DIRECTORY / "manifest.json"
                if not directory.is_dir() or not manifest.is_file():
                    continue
                store = ProjectStore.open(manifest.parent)
                value = store.load_manifest()
                latest_revision = store.load_latest()
            except (OSError, ProjectStoreError, DocumentValidationError):
                continue
            # Beta is a clean schema cutover. Old on-disk projects remain untouched,
            # but they are not advertised to the new editor and therefore cannot
            # trigger a late 500 after project selection.
            if latest_revision is None or latest_revision.document.schema_version != EDITOR_DOCUMENT_SCHEMA:
                continue
            latest = value["revisions"][-1] if value["revisions"] else None
            try:
                display_name = load_task_info(directory, directory.name)["display_name"]
            except (OSError, TypeError, ValueError):
                display_name = directory.name
            try:
                tutorial = _tutorial_project(directory.name)
            except HTTPException:
                # A corrupt per-project tutorial binding is isolated exactly
                # like an unreadable project store; it must not hide healthy projects.
                continue
            project = {
                    "project_id": directory.name,
                    "document_id": value["document_id"],
                    "latest_revision_id": value["latest_revision_id"],
                    "revision_count": value["revision_count"],
                    "complete": bool(latest["complete"]) if latest else False,
                    "updated_at": latest_revision.created_at,
                    "tutorial_case_id": str(tutorial["case_id"]) if tutorial else "",
                }
            if display_name:
                project["display_name"] = display_name
            projects.append(project)
    projects.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return {"schema_version": "substar.project-list.v1", "projects": projects}


@router.get("/editor-tasks")
def list_editor_tasks() -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    root = _projects_root()
    if not root.is_dir():
        return {"schema_version": "substar.editor-task-list.v1", "tasks": tasks}
    for directory in root.iterdir():
        if not directory.is_dir() or not (directory / PROJECT_DIRECTORY / "manifest.json").is_file():
            continue
        project_id = directory.name
        display_name = project_id
        try:
            display_name = load_task_info(directory, project_id)["display_name"]
        except (OSError, TypeError, ValueError):
            pass
        translation = load_translation_status(directory)
        if translation and translation.get("state") in {
            "queued", "running", "cancelling", "failed", "interrupted"
        }:
            tasks.append({
                **translation,
                "kind": "translation",
                "display_name": display_name,
            })
        task_dir = directory / "editor_tasks"
        if not task_dir.is_dir():
            continue
        for path in task_dir.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if value.get("status") not in {"queued", "running", "failed"}:
                continue
            tasks.append({**value, "display_name": display_name})
    tasks.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return {"schema_version": "substar.editor-task-list.v1", "tasks": tasks}


@router.get("/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    revision = open_project_store(project_id).load_latest()
    if revision is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "empty_project", "message": "项目还没有文档版本"},
        )
    return revision_payload(revision)


@router.post("/projects/{project_id}/tutorial/reset")
def reset_tutorial_project(project_id: str) -> dict[str, Any]:
    tutorial = _tutorial_project(project_id)
    if tutorial is None:
        raise HTTPException(status_code=409, detail={
            "code": "not_tutorial_project", "message": "当前项目不是教程案例"
        })
    store = open_project_store(project_id)
    latest = store.load_latest()
    if latest is None:
        raise HTTPException(status_code=404, detail={
            "code": "empty_project", "message": "项目还没有文档版本"
        })
    try:
        baseline = store.load_revision(str(tutorial["baseline_revision_id"]))
    except KeyError as exc:
        raise HTTPException(status_code=500, detail={
            "code": "tutorial_baseline_missing", "message": str(exc)
        }) from exc
    for path in (
        project_job_path(project_id) / "review" / "latest.json",
        project_job_path(project_id) / "review" / "source_latest.json",
        project_job_path(project_id) / "review" / "translation_latest.json",
        project_job_path(project_id) / "tutorial_progress.json",
    ):
        path.unlink(missing_ok=True)
    if latest.document.content_hash() == baseline.document.content_hash() and not latest.document.complete:
        return revision_payload(latest)
    document = replace(
        baseline.document,
        properties=replace(baseline.document.properties, complete=False),
    )
    return _save_document(
        project_id,
        expected_revision_id=latest.revision_id,
        document=document,
        operation="tutorial_reset",
        provenance=ChangeProvenance(
            kind=ChangeKind.MANUAL,
            operation="tutorial_reset",
            actor="editor-tutorial",
            metadata={"case_id": tutorial["case_id"], "baseline_revision_id": baseline.revision_id},
        ),
    )


@router.post("/examples/tutorials/{case_id}/launch")
def launch_tutorial_example(case_id: str) -> dict[str, Any]:
    """Materialize one packaged tutorial as a resettable user-data project."""
    root, manifest = _tutorial_example(case_id)
    project_id = {
        "reference-script-v1": "tutorial_beginner_v1",
        "advanced-ai-v1": "tutorial_advanced_ai_v1",
    }[case_id]
    job_dir = _projects_root() / project_id
    store_path = job_dir / PROJECT_DIRECTORY

    def register_task_info() -> None:
        save_task_info(job_dir, project_id, {
            "display_name": str(manifest["display_name"]),
            "language": str(manifest["source_language"]),
            "target_language_mode": str(manifest["target_language"]),
            "source_hard_limit": int(manifest["source_hard_limit"]),
            "target_hard_limit": int(manifest["target_hard_limit"]),
        })

    def materialize_media() -> None:
        media_name = str(manifest["assets"]["media"])
        media_source = (root / media_name).resolve()
        expected_media = str(manifest.get("sha256", {}).get(media_name, ""))
        media_bytes = media_source.read_bytes()
        media_digest = sha256(media_bytes).hexdigest()
        if root not in media_source.parents or (
            expected_media and media_digest != expected_media
        ):
            raise ValueError("教程媒体摘要不匹配")
        targets = (
            job_dir / "input" / "audio_16k_mono.wav",
            job_dir / "audio_16k_mono.wav",
        )
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or sha256(target.read_bytes()).hexdigest() != media_digest:
                shutil.copy2(media_source, target)

    if store_path.is_dir():
        tutorial = _tutorial_project(project_id)
        if tutorial is None or tutorial.get("case_id") != case_id:
            raise HTTPException(status_code=409, detail={
                "code": "tutorial_project_conflict", "message": "教程项目 ID 已被其他项目占用"
            })
        revision = reset_tutorial_project(project_id)
        register_task_info()
        materialize_media()
        return {
            "schema_version": "substar.tutorial-launch.v1",
            "case_id": case_id,
            "level": manifest.get("level"),
            "project_id": project_id,
            "revision": revision,
            "simulated": True,
        }
    if case_id != "advanced-ai-v1":
        raise HTTPException(status_code=409, detail={
            "code": "tutorial_creation_required",
            "message": "初级教程请从引导流程创建案例项目",
        })
    job_dir.mkdir(parents=True, exist_ok=False)
    try:
        source_document = _load_tutorial_document(root, manifest, "segmentation")
        store = ProjectStore.create(store_path, project_id=project_id)
        baseline = store.save(
            source_document,
            provenance=ChangeProvenance(
                kind=ChangeKind.IMPORT,
                operation="tutorial_snapshot_segmentation",
                actor="packaged-example-finalizer",
                metadata={"case_id": case_id, "simulated": True},
            ),
        )
        materialize_media()
        atomic_write_json(job_dir / "run_manifest.json", {
            "schema_version": "substar.run-manifest.v1",
            "source_file": "audio_16k_mono.wav",
            "media": {"duration_seconds": max((cue.end for cue in source_document.cues), default=0.0)},
            "tutorial_example": {"case_id": case_id, "simulated": True},
        })
        register_task_info()
        atomic_write_json(job_dir / "project_creation.json", {
            "schema_version": "substar.project-creation.v1",
            "input_mode": "packaged_example",
            "source_file": "audio_16k_mono.wav",
            "tutorial_case_id": case_id,
            "simulated": True,
        })
        atomic_write_json(job_dir / "tutorial_project.json", {
            "schema_version": "substar.tutorial-project.v2",
            "case_id": case_id,
            "level": "advanced",
            "display_name": "进阶教程",
            "baseline_revision_id": baseline.revision_id,
            "baseline_document_hash": baseline.document_hash,
            "available_stages": ["segmentation", "calibration", "translation", "review"],
            "simulated": True,
        })
    except Exception:
        if job_dir.is_dir():
            shutil.rmtree(job_dir)
        raise
    return {
        "schema_version": "substar.tutorial-launch.v1",
        "case_id": case_id,
        "level": "advanced",
        "project_id": project_id,
        "revision": revision_payload(baseline),
        "simulated": True,
    }


@router.get("/examples/tutorials/{case_id}/assets/{asset_name}")
def get_tutorial_example_asset(case_id: str, asset_name: Literal["media", "reference"]) -> FileResponse:
    root, manifest = _tutorial_example(case_id)
    filename = str(manifest["assets"].get(asset_name, ""))
    path = (root / filename).resolve()
    if not filename or root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail={
            "code": "tutorial_asset_not_found", "message": "教程素材不存在"
        })
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0])


@router.post("/projects/{project_id}/tutorial/stages/{stage}")
def apply_tutorial_stage(
    project_id: str,
    stage: Literal["calibration", "translation", "review"],
    payload: TutorialStageRequest,
) -> dict[str, Any]:
    """Validate and commit a packaged stage without contacting a provider."""
    tutorial = _tutorial_project(project_id)
    if tutorial is None or tutorial.get("case_id") != "advanced-ai-v1":
        raise HTTPException(status_code=409, detail={
            "code": "advanced_tutorial_required", "message": "当前项目不是进阶教程"
        })
    root, manifest = _tutorial_example("advanced-ai-v1")
    store = open_project_store(project_id)
    latest = store.load_latest()
    if latest is None:
        raise HTTPException(status_code=404, detail={"code": "empty_project", "message": "教程项目为空"})
    if latest.revision_id != payload.expected_revision_id:
        raise HTTPException(status_code=409, detail={
            "code": "revision_conflict", "message": "教程阶段所依据的版本已经变化"
        })
    if stage == "review":
        try:
            snapshot = json.loads((root / str(manifest["assets"]["review"])).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=500, detail={
                "code": "tutorial_example_invalid", "message": str(exc)
            }) from exc
        snapshot["based_on_revision_id"] = latest.revision_id
        snapshot["schema_version"] = "substar.tutorial-review-result.v1"
        _token_map, cue_rows = _editor_ai_cues(latest)
        cues_by_id = {str(cue["cue_id"]): cue for cue in cue_rows}
        for issue in snapshot.get("issues", []):
            issue["cue_basis"] = _review_issue_cue_basis(
                issue, track=str(issue.get("track", "source")), cues_by_id=cues_by_id
            )
        by_issue_id = {str(issue["issue_id"]): issue for issue in snapshot.get("issues", [])}
        snapshot["source_issues"] = [
            by_issue_id.get(str(issue.get("issue_id")), issue)
            for issue in snapshot.get("source_issues", [])
        ]
        snapshot["translation_issues"] = [
            by_issue_id.get(str(issue.get("issue_id")), issue)
            for issue in snapshot.get("translation_issues", [])
        ]
        review_dir = project_job_path(project_id) / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(review_dir / "latest.json", snapshot)
        atomic_write_json(project_job_path(project_id) / "tutorial_progress.json", {
            "schema_version": "substar.tutorial-progress.v1", "stage": "review"
        })
        return {**snapshot, "simulated": True}
    document = _load_tutorial_document(root, manifest, stage)
    if document.document_id != latest.document.document_id:
        raise HTTPException(status_code=500, detail={
            "code": "tutorial_example_invalid", "message": "教程阶段文档身份不一致"
        })
    revision = store.save(
        document,
        expected_revision_id=latest.revision_id,
        provenance=ChangeProvenance(
            kind=ChangeKind.AI,
            operation=f"tutorial_snapshot_{stage}",
            actor="packaged-example-finalizer",
            metadata={"case_id": tutorial["case_id"], "simulated": True},
        ),
    )
    atomic_write_json(project_job_path(project_id) / "tutorial_progress.json", {
        "schema_version": "substar.tutorial-progress.v1", "stage": stage,
        "revision_id": revision.revision_id,
    })
    return {**revision_payload(revision), "simulated": True, "tutorial_stage": stage}


@router.get("/projects/{project_id}/ai-task")
def get_project_editor_ai_task(project_id: str) -> dict[str, Any] | None:
    try:
        return load_editor_ai_task(project_job_path(project_id))
    except EditorAiTaskConflict as exc:
        raise HTTPException(status_code=500, detail={
            "code": "editor_ai_task_state_invalid", "message": str(exc)
        }) from exc


@router.delete("/projects/{project_id}/ai-task")
def cancel_project_editor_ai_task(project_id: str) -> dict[str, Any]:
    job_dir = project_job_path(project_id)
    try:
        task = request_task_cancellation(job_dir)
    except EditorAiTaskConflict as exc:
        raise HTTPException(status_code=409, detail={
            "code": "editor_ai_task_cancel_rejected", "message": str(exc)
        }) from exc
    if task.get("kind") == EditorAiTaskKind.TRANSLATION.value:
        cancel_translation_task(job_dir, str(task["task_id"]))
    return task


@router.get("/projects/{project_id}/task-info")
def get_project_task_info(project_id: str) -> dict[str, Any]:
    """Return the project's sole mutable task-information authority."""
    open_project_store(project_id)
    job_dir = project_job_path(project_id)
    try:
        return load_task_info(job_dir, project_id)
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/projects/{project_id}/task-info")
def set_project_task_info(
    project_id: str, payload: ProjectTaskInfoRequest
) -> dict[str, Any]:
    """Atomically update metadata without mutating any existing subtitle content."""
    open_project_store(project_id)
    try:
        return save_task_info(project_job_path(project_id), project_id, payload.model_dump())
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


_AUDIO_MEDIA_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
_VIDEO_MEDIA_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def _project_media_source(project_id: str) -> tuple[Path, dict[str, Any]]:
    job_dir = project_job_path(project_id)
    manifest_path = job_dir / "run_manifest.json"
    # The manifest is metadata, not the media itself.  Older/interrupted
    # ingests can leave it missing or empty while the uploaded file is still
    # present under input/.  Keep the editor usable in that situation.
    manifest: dict[str, Any] = {}
    try:
        if manifest_path.is_file() and manifest_path.stat().st_size:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                manifest = value
    except (OSError, json.JSONDecodeError, UnicodeError):
        # Fall through to deterministic input-directory discovery.
        manifest = {}
    explicit = str(manifest.get("source_path", "")).strip()
    source_name = Path(str(manifest.get("source_file", ""))).name
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if source_name:
        candidates.extend((job_dir / "input" / source_name, job_dir / source_name))
    candidates.extend(
        sorted(
            path
            for path in (job_dir / "input").glob("*")
            if path.is_file()
            and path.suffix.lower()
            in _VIDEO_MEDIA_SUFFIXES | _AUDIO_MEDIA_SUFFIXES
        )
    )
    source_path = next(
        (
            candidate.resolve()
            for candidate in candidates
            if candidate.resolve().is_file()
            and job_dir.resolve() in candidate.resolve().parents
        ),
        None,
    )
    if source_path is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "media_missing", "message": "项目媒体文件不存在"},
        )
    return source_path, manifest


def _project_media_kind(source_path: Path, manifest: Mapping[str, Any]) -> Literal["audio", "video"]:
    media = manifest.get("media") if isinstance(manifest, Mapping) else None
    streams = media.get("streams") if isinstance(media, Mapping) else None
    if isinstance(streams, list):
        stream_types = {
            str(stream.get("codec_type", "")).casefold()
            for stream in streams
            if isinstance(stream, Mapping)
        }
        if "video" in stream_types:
            return "video"
        if "audio" in stream_types:
            return "audio"
    return "audio" if source_path.suffix.casefold() in _AUDIO_MEDIA_SUFFIXES else "video"


@router.get("/projects/{project_id}/media-info")
def get_project_media_info(project_id: str) -> dict[str, Any]:
    source_path, manifest = _project_media_source(project_id)
    media = manifest.get("media") if isinstance(manifest, Mapping) else None
    raw_duration = media.get("duration_seconds") if isinstance(media, Mapping) else None
    try:
        duration = float(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        duration = None
    return {
        "schema_version": "substar.media-info.v1",
        "kind": _project_media_kind(source_path, manifest),
        "filename": source_path.name,
        "content_type": mimetypes.guess_type(source_path.name)[0] or "application/octet-stream",
        "duration": duration,
    }


@router.get("/projects/{project_id}/media")
def get_project_media(project_id: str) -> FileResponse:
    job_dir = project_job_path(project_id)
    source_path, _manifest = _project_media_source(project_id)
    playback_path = prepare_playback_media(source_path, job_dir / "playback_cache")
    return FileResponse(playback_path, media_type=None)


def _pcm_peak_buckets(samples: array, bucket: int) -> list[float]:
    return [
        round(max(abs(value) for value in samples[start : start + bucket]) / 32768, 4)
        for start in range(0, len(samples), bucket)
    ]


def _waveform_overview(audio: Path, cache: Path) -> dict[str, Any]:
    if cache.is_file() and cache.stat().st_mtime >= audio.stat().st_mtime:
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("schema_version") == "substar.waveform.v1":
                return cached
        except (OSError, json.JSONDecodeError):
            pass
    with wave.open(str(audio), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        frame_count = source.getnframes()
        if width != 2:
            raise HTTPException(
                status_code=415,
                detail={"code": "waveform_format_unsupported", "message": "波形预览只支持 16-bit PCM"},
            )
        raw = source.readframes(frame_count)
    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    if channels > 1:
        samples = array(
            "h",
            (
                max(abs(samples[offset + channel]) for channel in range(channels))
                for offset in range(0, len(samples), channels)
            ),
        )
    bucket = max(1, len(samples) // 12000)
    result = {
        "schema_version": "substar.waveform.v1",
        "duration": frame_count / rate,
        "sample_rate": rate,
        "peaks": _pcm_peak_buckets(samples, bucket),
    }
    atomic_write_json(cache, result)
    return result


def _aggregate_peaks(values: list[float], points: int) -> list[float]:
    if not values:
        return []
    bucket = max(1, (len(values) + points - 1) // points)
    return [max(values[start : start + bucket]) for start in range(0, len(values), bucket)]


@router.get("/projects/{project_id}/waveform")
def get_project_waveform(
    project_id: str,
    start: float | None = Query(default=None, ge=0),
    end: float | None = Query(default=None, ge=0),
    points: int = Query(default=1600, ge=128, le=4096),
) -> dict[str, Any]:
    job_dir = project_job_path(project_id)
    audio = (job_dir / "audio_16k_mono.wav").resolve()
    if not audio.is_file() or job_dir.resolve() not in audio.parents:
        raise HTTPException(
            status_code=404,
            detail={"code": "waveform_audio_missing", "message": "项目没有可用的波形音频"},
        )
    cache = project_store_path(project_id) / "waveform_peaks.json"
    with wave.open(str(audio), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        frame_count = source.getnframes()
        if width != 2:
            raise HTTPException(
                status_code=415,
                detail={"code": "waveform_format_unsupported", "message": "波形预览只支持 16-bit PCM"},
            )
        duration = frame_count / rate
        # No range keeps the original endpoint contract for older clients.
        if start is None and end is None:
            return _waveform_overview(audio, cache)
        window_start = min(duration, max(0.0, float(start or 0.0)))
        window_end = min(duration, max(window_start, float(duration if end is None else end)))
        if window_end - window_start <= 0:
            return {
                "schema_version": "substar.waveform.window.v1",
                "duration": duration,
                "sample_rate": rate,
                "window_start": window_start,
                "window_end": window_end,
                "peaks": [],
            }
        audio_stat = audio.stat()
        window_cache_key = (
            str(audio),
            audio_stat.st_size,
            audio_stat.st_mtime_ns,
            round(window_start, 3),
            round(window_end, 3),
            int(points),
        )
        cached_window = WAVEFORM_WINDOW_CACHE.get(window_cache_key)
        if cached_window is not None:
            return cached_window
        # Wide views already have more overview samples than screen pixels.  Reuse
        # that cache instead of decoding many minutes of PCM whenever the user zooms.
        if window_end - window_start > 120:
            overview = _waveform_overview(audio, cache)
            overview_peaks = overview["peaks"]
            first = max(0, int(window_start / duration * len(overview_peaks)))
            last = min(len(overview_peaks), int(window_end / duration * len(overview_peaks)) + 1)
            peaks = _aggregate_peaks(overview_peaks[first:last], points)
        else:
            first_frame = int(window_start * rate)
            last_frame = min(frame_count, max(first_frame + 1, int(window_end * rate)))
            source.setpos(first_frame)
            raw = source.readframes(last_frame - first_frame)
            samples = array("h")
            samples.frombytes(raw)
            if sys.byteorder != "little":
                samples.byteswap()
            if channels > 1:
                samples = array(
                    "h",
                    (
                        max(abs(samples[offset + channel]) for channel in range(channels))
                        for offset in range(0, len(samples), channels)
                    ),
                )
            bucket = max(1, (len(samples) + points - 1) // points)
            peaks = _pcm_peak_buckets(samples, bucket)
    result = {
        "schema_version": "substar.waveform.window.v1",
        "duration": duration,
        "sample_rate": rate,
        "window_start": window_start,
        "window_end": window_end,
        "peaks": peaks,
    }
    WAVEFORM_WINDOW_CACHE.put(window_cache_key, result)
    return result


@router.post("/projects/{project_id}/auto-snap/preview")
def preview_project_auto_snap(
    project_id: str, payload: SmartForwardSnapRequest
) -> dict[str, Any]:
    job_dir = project_job_path(project_id)
    latest = ProjectStore.open(project_store_path(project_id)).load_latest()
    if latest is None:
        raise HTTPException(status_code=404, detail="项目没有可编辑版本")
    if latest.revision_id != payload.expected_revision_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "revision_conflict",
                "message": "智能吸附所基于的编辑版本已变化",
                "revision_id": latest.revision_id,
            },
        )
    audio = (job_dir / "audio_16k_mono.wav").resolve()
    if not audio.is_file() or job_dir.resolve() not in audio.parents:
        raise HTTPException(
            status_code=404,
            detail={"code": "waveform_audio_missing", "message": "项目没有可用的波形音频"},
        )
    display_tokens = {
        token.token_id: token for token in latest.document.display_tokens
    }
    active_cues = [
        cue for cue in latest.document.cues if cue.state.value == "active"
    ]
    candidates: list[dict[str, Any]] = []
    previous_end = 0.0
    for cue in active_cues:
        is_manual = all(
            not display_tokens[token_id].source_token_ids
            for token_id in cue.display_token_ids
        )
        if not is_manual:
            candidates.append(
                {
                    "cue_id": cue.cue_id,
                    "start": cue.start,
                    "minimum_start": previous_end,
                }
            )
        previous_end = max(previous_end, cue.end)
    result = smart_forward_snap(audio, candidates)
    return {**result, "revision_id": latest.revision_id}


def _commit_binary_export(
    filename: str, artifact_type: str, writer: Any
) -> FileResponse:
    directory = Path(tempfile.mkdtemp(prefix="substar-export-"))
    destination = directory / filename
    try:
        writer(destination)
    except OSError as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(
            status_code=422,
            detail={"code": "export_build_failed", "message": f"导出文件生成失败：{exc}"},
        ) from exc
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return FileResponse(
        path=destination,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-Substar-Artifact-Type": artifact_type,
        },
        background=BackgroundTask(shutil.rmtree, directory, ignore_errors=True),
    )


@router.get("/projects/{project_id}/export/{mode}")
def export_project(project_id: str, mode: SubtitleExportMode) -> Response:
    revision = open_project_store(project_id).load_latest()
    if revision is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "empty_project", "message": "项目还没有文档版本"},
        )
    content = render_document_srt(revision.document, mode)
    filename = f"{project_id}_{mode.value}.srt"
    return Response(
        content=("\ufeff" + content).encode("utf-8"),
        media_type="application/x-subrip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Substar-Artifact-Type": "subtitle",
        },
    )


@router.get("/projects/{project_id}/hotwords/export")
def export_project_hotwords(project_id: str) -> Response:
    project_name = _project_name_for_hotwords(project_id)
    entries = _collect_generated_hotwords(project_id, project_name)
    content = glossary_xlsx_bytes(entries)
    filename = f"{project_id}_hotwords.xlsx"
    return Response(
        content=content,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/projects/{project_id}/revisions/{revision_id}")
def get_project_revision(project_id: str, revision_id: str) -> dict[str, Any]:
    try:
        revision = open_project_store(project_id).load_revision(revision_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "revision_not_found", "message": str(exc)},
        ) from exc
    return revision_payload(revision)


@router.get("/projects/{project_id}/revisions")
def list_project_revisions(
    project_id: str,
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
    before: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    service = RevisionService(
        lambda requested_project_id: SQLiteProjectRepository(
            open_project_store(requested_project_id)
        )
    )
    page = service.list_metadata(
        project_id,
        limit=limit,
        before_revision_number=before,
    )
    latest_id = page.latest_revision_id
    revisions = [
        {
            "revision_id": item["revision_id"],
            "revision_number": item["revision_number"],
            "parent_revision_id": item["parent_revision_id"],
            "created_at": item["created_at"],
            "complete": item["complete"],
            "is_latest": item["revision_id"] == latest_id,
            "provenance": item["provenance"],
        }
        for item in page.items
    ]
    return {
        "schema_version": "substar.revision-list.v1",
        "project_id": project_id,
        "latest_revision_id": latest_id,
        "revisions": revisions,
        "next_before": page.next_before,
    }


def _save_revision(
    project_id: str,
    *,
    expected_revision_id: str,
    document: EditorDocument,
    operation: str,
    provenance: ChangeProvenance | None = None,
) -> Any:
    try:
        assert_editor_write_allowed(project_job_path(project_id))
    except EditorAiTaskConflict as exc:
        raise HTTPException(
            status_code=423,
            detail={"code": "editor_ai_task_locked", "message": str(exc)},
        ) from exc
    store = open_project_store(project_id)
    provenance = provenance or ChangeProvenance(
        kind=ChangeKind.MANUAL, operation=operation, actor="editor"
    )
    try:
        revision = store.save(
            canonicalize_document_cues(document),
            provenance=provenance,
            expected_revision_id=expected_revision_id,
        )
    except ProjectConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "revision_conflict", "message": str(exc)},
        ) from exc
    except ProjectStoreError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "project_save_failed", "message": str(exc)},
        ) from exc
    return revision


def _save_document(
    project_id: str,
    *,
    expected_revision_id: str,
    document: EditorDocument,
    operation: str,
    provenance: ChangeProvenance | None = None,
) -> dict[str, Any]:
    return revision_payload(_save_revision(
        project_id,
        expected_revision_id=expected_revision_id,
        document=document,
        operation=operation,
        provenance=provenance,
    ))


@router.put("/projects/{project_id}/document")
def save_project_document(
    project_id: str, payload: SaveDocumentRequest
) -> dict[str, Any]:
    try:
        document = EditorDocument.from_dict(payload.document)
    except (KeyError, TypeError, ValueError, DocumentValidationError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_editor_document", "message": str(exc)},
        ) from exc
    return _save_document(
        project_id,
        expected_revision_id=payload.expected_revision_id,
        document=document,
        operation=payload.operation,
    )


@router.post("/projects/{project_id}/operations")
def apply_project_operation(
    project_id: str, payload: DocumentOperationRequest
) -> dict[str, Any]:
    try:
        assert_editor_write_allowed(project_job_path(project_id))
    except EditorAiTaskConflict as exc:
        raise HTTPException(status_code=423, detail={
            "code": "editor_ai_task_locked", "message": str(exc)
        }) from exc
    return commit_single_operation(
        project_id,
        payload,
        repository_factory=lambda requested_project_id: SQLiteProjectRepository(
            open_project_store(requested_project_id)
        ),
        serialize_delta=revision_delta_payload,
    )


@router.post("/projects/{project_id}/operation-batches")
def apply_project_operation_batch(
    project_id: str, payload: DocumentOperationBatchRequest
) -> dict[str, Any]:
    try:
        assert_editor_write_allowed(project_job_path(project_id))
    except EditorAiTaskConflict as exc:
        raise HTTPException(status_code=423, detail={
            "code": "editor_ai_task_locked", "message": str(exc)
        }) from exc
    return commit_operation_batch(
        project_id,
        payload,
        repository_factory=lambda requested_project_id: SQLiteProjectRepository(
            open_project_store(requested_project_id)
        ),
        serialize_delta=revision_delta_payload,
    )


@router.post("/projects/{project_id}/batch-replace")
def batch_replace_project(
    project_id: str, payload: BatchReplaceRequest
) -> dict[str, Any]:
    store = open_project_store(project_id)
    latest = store.load_latest()
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "empty_project", "message": "项目还没有文档版本"},
        )
    if payload.expected_revision_id != latest.revision_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "revision_conflict", "message": "编辑基于旧版本，请刷新后重试"},
        )
    provenance_map = {
        "manual": (ChangeKind.MANUAL, "batch_replace", "editor"),
        "ai_calibration": (ChangeKind.AI, "ai_calibration_apply", "ai-calibration"),
        "reference_manuscript": (
            ChangeKind.IMPORT,
            "reference_manuscript_apply",
            "reference-manuscript",
        ),
    }
    kind, operation_name, actor = provenance_map[payload.origin]
    provenance = ChangeProvenance(
        kind=kind,
        operation=operation_name,
        actor=actor,
        metadata={**payload.metadata, "replacement_count": len(payload.replacements)},
    )
    operation = {
        "operation_id": payload.operation_id,
        "type": "batch_replace",
        "payload": {
            "replacements": [item.model_dump() for item in payload.replacements],
            "provenance": provenance.to_dict(),
        },
    }
    try:
        document = apply_document_operation(latest.document, operation)
    except (KeyError, TypeError, ValueError, DocumentOperationError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_operation", "message": str(exc)},
        ) from exc
    return _save_document(
        project_id,
        expected_revision_id=latest.revision_id,
        document=document,
        operation=operation_name,
        provenance=provenance,
    )


@router.post("/projects/{project_id}/checkpoints")
def create_project_checkpoint(
    project_id: str, payload: CheckpointRequest
) -> dict[str, Any]:
    store = open_project_store(project_id)
    latest = store.load_latest()
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "empty_project", "message": "项目还没有文档版本"},
        )
    revision_metadata = store.list_revision_metadata()
    checkpoint_number = 1 + sum(
        1
        for item in revision_metadata
        if item.get("provenance", {}).get("operation") == "checkpoint"
    )
    provenance = ChangeProvenance(
        kind=ChangeKind.MANUAL,
        operation="checkpoint",
        actor="editor",
        metadata={
            "label": payload.label.strip(),
            "checkpoint_number": checkpoint_number,
        },
    )
    return _save_document(
        project_id,
        expected_revision_id=payload.expected_revision_id,
        document=latest.document,
        operation="checkpoint",
        provenance=provenance,
    )


@router.post("/projects/{project_id}/restore")
def restore_project_revision(
    project_id: str, payload: RestoreRevisionRequest
) -> dict[str, Any]:
    store = open_project_store(project_id)
    latest = store.load_latest()
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "empty_project", "message": "项目还没有文档版本"},
        )
    if payload.expected_revision_id != latest.revision_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "revision_conflict", "message": "恢复请求基于旧版本，请刷新后重试"},
        )
    try:
        target = store.load_revision(payload.revision_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "revision_not_found", "message": str(exc)},
        ) from exc
    provenance = ChangeProvenance(
        kind=ChangeKind.MANUAL,
        operation="restore_revision",
        actor="editor",
        metadata={
            "restored_revision_id": target.revision_id,
            "navigation": payload.navigation,
            "undo_revision_ids": payload.undo_revision_ids,
            "redo_revision_ids": payload.redo_revision_ids,
        },
    )
    return _save_document(
        project_id,
        expected_revision_id=latest.revision_id,
        document=target.document,
        operation="restore_revision",
        provenance=provenance,
    )


@router.put("/projects/{project_id}/presentation")
def set_project_presentation(
    project_id: str, payload: PresentationRequest
) -> dict[str, Any]:
    store = open_project_store(project_id)
    latest = store.load_latest()
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "empty_project", "message": "项目还没有文档版本"},
        )
    if payload.expected_revision_id != latest.revision_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "revision_conflict", "message": "设置基于旧版本，请刷新后重试"},
        )
    values = payload.model_dump(exclude_none=True)
    operation = {
        "operation_id": payload.operation_id,
        "type": "set_presentation",
        "payload": {
            key: value
            for key, value in values.items()
            if key not in {"expected_revision_id", "operation_id"}
        },
    }
    operation["payload"]["provenance"] = {
        "kind": "manual",
        "operation": "set_presentation",
        "actor": "editor",
    }
    try:
        document = apply_document_operation(latest.document, operation)
    except (KeyError, TypeError, ValueError, DocumentOperationError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_operation", "message": str(exc)},
        ) from exc
    return _save_document(
        project_id,
        expected_revision_id=latest.revision_id,
        document=document,
        operation="set_presentation",
    )


@router.post("/projects/{project_id}/convert-script")
def convert_project_script(
    project_id: str, payload: ScriptConversionRequest
) -> dict[str, Any]:
    store = open_project_store(project_id)
    latest = store.load_latest()
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "empty_project", "message": "项目还没有文档版本"},
        )
    if payload.expected_revision_id != latest.revision_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "revision_conflict", "message": "繁简转换基于旧版本，请刷新后重试"},
        )
    provenance = ChangeProvenance(
        kind=ChangeKind.MANUAL,
        operation="set_script_projection",
        actor="editor",
        metadata={"target": payload.target},
    )
    document = replace(
        latest.document,
        properties=replace(
            latest.document.properties,
            script_projection=payload.target,
        ),
        changes=(*latest.document.changes, provenance),
    )
    return _save_document(
        project_id,
        expected_revision_id=latest.revision_id,
        document=document,
        operation="set_script_projection",
        provenance=provenance,
    )


@router.post("/projects/{project_id}/reference-manuscript")
async def match_project_reference_manuscript(
    project_id: str,
    expected_revision_id: str = Form(min_length=1),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Match a reference document onto the current editor token track.

    The reference owns aligned spelling, casing and punctuation. Text found
    only in ASR remains active with an explicit retained-source marker. This
    surface never silently changes cue boundaries.
    """

    store = open_project_store(project_id)
    latest = store.load_latest()
    if latest is None:
        raise HTTPException(status_code=404, detail={"code": "empty_project", "message": "项目还没有文档版本"})
    if expected_revision_id != latest.revision_id:
        raise HTTPException(status_code=409, detail={"code": "revision_conflict", "message": "参考文稿基于旧版本，请刷新后重试"})
    payload = await file.read(50 * 1024 * 1024 + 1)
    if len(payload) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail={"code": "reference_too_large", "message": "参考文稿不能超过 50 MB"})
    active_tokens = [token for token in latest.document.display_tokens if token.state.value == "active"]
    units = [{"index": index, "text": token.text} for index, token in enumerate(active_tokens)]
    configured_language = "Auto"
    try:
        configured_language = str(get_project_task_info(project_id).get("language") or "Auto")
    except HTTPException:
        pass
    source_language = (
        source_language_for_text("".join(token.text for token in active_tokens))
        if configured_language.strip().lower() in {"", "auto", "automatic"}
        else configured_language
    )
    try:
        reference_text = extract_reference_text(payload, file.filename or "reference.txt")
        result = editor_reference_operations(reference_text, units, source_language)
    except ManuscriptMatchError as exc:
        raise HTTPException(status_code=422, detail={"code": "reference_match_failed", "message": str(exc)}) from exc
    replacements = []
    for edit in result.get("edits", []):
        index = int(edit.get("index", -1))
        text = str(edit.get("text", "")).strip()
        if 0 <= index < len(active_tokens) and text and text != active_tokens[index].text:
            replacements.append(BatchReplacement(
                token_id=active_tokens[index].token_id,
                text=text,
                expected_text=active_tokens[index].text,
            ))
    reference_changes = []
    for raw in result.get("reference_changes", []):
        source_indices = [
            int(value) for value in raw.get("source_indices", [])
            if 0 <= int(value) < len(active_tokens)
        ]
        token_ids = [active_tokens[index].token_id for index in source_indices]
        if not token_ids:
            continue
        reference_changes.append({
            "change_id": str(raw.get("id", f"reference-{len(reference_changes)}")),
            "type": str(raw.get("type", "replace")),
            "token_ids": token_ids,
            "source_indexes": source_indices,
            "before": str(raw.get("original", "")),
            "after": str(raw.get("text", "")),
            "status": str(raw.get("status", "applied")),
        })
    reference_metadata = {
        "filename": file.filename or "reference.txt",
        "similarity": result.get("similarity"),
        "reference_changes": reference_changes,
        "retained_source_count": sum(
            1 for item in reference_changes if item["type"] == "retained_source"
        ),
    }
    if not replacements:
        if not reference_changes:
            return {"revision": latest.to_dict(), "match": result, "applied": 0}
        provenance = ChangeProvenance(
            kind=ChangeKind.IMPORT,
            operation="reference_manuscript_apply",
            actor="reference-manuscript",
            metadata={**reference_metadata, "replacement_count": 0},
        )
        document = replace(
            latest.document,
            changes=(*latest.document.changes, provenance),
        )
        revision = _save_document(
            project_id,
            expected_revision_id=latest.revision_id,
            document=document,
            operation="reference_manuscript_apply",
            provenance=provenance,
        )
        return {"revision": revision, "match": result, "applied": 0}
    revision = batch_replace_project(project_id, BatchReplaceRequest(
        expected_revision_id=latest.revision_id,
        operation_id=f"op_reference_{latest.revision_id}",
        replacements=replacements,
        origin="reference_manuscript",
        metadata=reference_metadata,
    ))
    return {"revision": revision, "match": result, "applied": len(replacements)}


def _editor_ai_cues(revision: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    token_map = {
        token.token_id: token
        for token in revision.document.display_tokens
        if token.state.value == "active"
    }
    group_map = {group.group_id: group for group in revision.document.groups}
    cues: list[dict[str, Any]] = []
    for cue in revision.document.cues:
        if cue.state.value != "active":
            continue
        token_ids = [token_id for token_id in cue.display_token_ids if token_id in token_map]
        group = group_map.get(cue.group_id or "")
        cues.append({
            "cue_id": cue.cue_id,
            "start": cue.start,
            "end": cue.end,
            "group_id": cue.group_id,
            "group_origin": group.origin if group else None,
            "execution_block_ids": list(group.execution_block_ids) if group else [],
            "group_dirty_flags": list(group.dirty_flags) if group else [],
            "group_migration_confidence": group.migration_confidence if group else None,
            "tokens": [
                {"token_id": token_id, "text": token_map[token_id].text}
                for token_id in token_ids
            ],
            "target_text": cue.target.target_text if cue.target else "",
        })
    return token_map, cues


def _editor_ai_group_blocks(
    cues: list[dict[str, Any]], *, halo_groups: int = 1
) -> dict[str, list[dict[str, Any]]]:
    """Keep semantic groups atomic and inherit their accepted execution plan."""
    ordered_groups: list[tuple[str, list[int]]] = []
    group_positions: dict[str, int] = {}
    for index, cue in enumerate(cues):
        group_id = str(cue.get("group_id") or f"ungrouped:{cue['cue_id']}")
        if group_id not in group_positions:
            group_positions[group_id] = len(ordered_groups)
            ordered_groups.append((group_id, []))
        ordered_groups[group_positions[group_id]][1].append(index)

    owners: dict[str, list[int]] = {}
    for group_index, (_group_id, indexes) in enumerate(ordered_groups):
        inherited = [
            str(value)
            for value in cues[indexes[0]].get("execution_block_ids", [])
            if str(value)
        ]
        owner = inherited[0] if inherited else "manual"
        owners.setdefault(owner, []).append(group_index)

    blocks: dict[str, list[dict[str, Any]]] = {}
    for owner, owned_group_indexes in owners.items():
        first_group = max(0, owned_group_indexes[0] - halo_groups)
        last_group = min(len(ordered_groups), owned_group_indexes[-1] + halo_groups + 1)
        owned = set(owned_group_indexes)
        block_cues: list[dict[str, Any]] = []
        for group_index in range(first_group, last_group):
            group_id, indexes = ordered_groups[group_index]
            for cue_index in indexes:
                block_cues.append({
                    **cues[cue_index],
                    "group_id": group_id,
                    "editable": group_index in owned,
                })
        blocks[owner] = block_cues
    return blocks


def _editor_ai_blocks(cues: list[dict[str, Any]], *, halo: int = 3) -> dict[str, list[dict[str, Any]]]:
    owners: dict[str, list[int]] = {}
    for index, cue in enumerate(cues):
        inherited = [
            str(value)
            for value in cue.get("execution_block_ids", [])
            if str(value)
        ]
        owners.setdefault(inherited[0] if inherited else "manual", []).append(index)
    blocks: dict[str, list[dict[str, Any]]] = {}
    for owner, indexes in owners.items():
        first = max(0, indexes[0] - halo)
        last = min(len(cues), indexes[-1] + halo + 1)
        owned = set(indexes)
        blocks[owner] = [
            {**cues[index], "editable": index in owned}
            for index in range(first, last)
        ]
    return blocks


def _run_editor_ai_blocks(
    *,
    settings: Mapping[str, Any],
    system_prompt: str,
    blocks: Mapping[str, list[dict[str, Any]]],
    failure_key: str,
    stage_name: str,
    retry_stage: str | None = "audit_repair",
    response_validator: Any | None = None,
    progress_callback: Any | None = None,
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    api_key = str(settings.get("translation_api_key", "")).strip()
    if not api_key:
        raise HTTPException(status_code=400, detail={"code": "api_key_missing", "message": "尚未配置翻译 API Key"})

    owned_task_id = current_editor_ai_task_id()

    def run(item: tuple[str, list[dict[str, Any]]]) -> tuple[str, dict[str, Any], dict[str, Any]]:
        if owned_task_id:
            with editor_ai_task_context(owned_task_id):
                return run_owned(item)
        return run_owned(item)

    def run_owned(item: tuple[str, list[dict[str, Any]]]) -> tuple[str, dict[str, Any], dict[str, Any]]:
        block_id, block_cues = item
        last_error: Exception | None = None
        attempt_count = 2 if retry_stage else 1
        for attempt in range(1, attempt_count + 1):
            active_stage = stage_name if attempt == 1 else str(retry_stage)
            try:
                value, request_metadata = call_translation_model(
                    base_url=str(settings.get("translation_api_base_url", "https://api.deepseek.com")),
                    api_key=api_key,
                    model=str(settings.get(f"stage_{active_stage}_model") or settings.get("translation_api_model", "deepseek-v4-flash")),
                    system_prompt=system_prompt,
                    groups=[{"block_id": block_id, "cues": block_cues}],
                    timeout=min(600, int(settings.get("translation_api_timeout_seconds", 300))),
                    thinking_mode=str(settings.get(f"stage_{active_stage}_thinking_mode", "disabled")),
                    reasoning_effort=str(settings.get(f"stage_{active_stage}_reasoning_effort", "high")),
                    request_attempts=(
                        max(1, int(settings.get("http_retry_attempts", 2)) + 1)
                        if retry_stage else 1
                    ),
                    max_tokens=int(settings.get(f"stage_{active_stage}_max_tokens", 65536)),
                    temperature=float(settings.get(f"stage_{active_stage}_temperature", 0.0)),
                )
                if response_validator is not None and not response_validator(value):
                    raise Stage2Error(
                        f"{active_stage} returned an invalid response contract"
                    )
                return block_id, value, {"attempt": attempt, **request_metadata}
            except Stage2Error as exc:
                last_error = exc
        return block_id, {failure_key: []}, {
            "attempt": attempt_count,
            "error": str(last_error),
        }

    if not blocks:
        return []
    results: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    workers = min(len(blocks), max(1, int(settings.get("translation_workers", 8))))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run, item) for item in blocks.items()]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
            if progress_callback is not None:
                progress_callback(len(results), len(blocks))
    return results


_CALIBRATION_PUNCTUATION = ".,?!;:，。？！；：、…"


def _calibration_signature(text: str) -> str:
    return "".join(
        char.casefold() for char in text
        if char not in _CALIBRATION_PUNCTUATION
    )


def _calibration_attach_mark(text: str, mark: str) -> str:
    base = str(text).rstrip(_CALIBRATION_PUNCTUATION)
    return base + mark


def _calibration_core(text: str) -> str:
    return str(text).rstrip(_CALIBRATION_PUNCTUATION)


def _calibration_suffix(text: str) -> str:
    value = str(text)
    return value[len(_calibration_core(value)):]


def _calibration_capitalize(text: str) -> str:
    chars = list(str(text))
    for index, char in enumerate(chars):
        if char.isalpha():
            chars[index] = char.upper()
            break
    return "".join(chars)


def _calibration_model_blocks(
    blocks: Mapping[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Preserve the exact current token text used by action preconditions."""
    return {
        block_id: [
            {
                **cue,
                "tokens": [dict(token) for token in cue["tokens"]],
            }
            for cue in block_cues
        ]
        for block_id, block_cues in blocks.items()
    }


_CALIBRATION_ACTION_FIELDS = {
    "action_id", "kind", "token_ids", "before_text", "after_text",
    "confidence", "evidence", "disposition", "affects_translation",
}
_CALIBRATION_EVIDENCE_KINDS = {
    "glossary", "reference_document", "document_consistency", "context",
    "user_instruction",
}


def _validated_calibration_contract_actions(
    value: Any,
    owned_token_ids: list[str],
    token_map: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    if not isinstance(value, Mapping) or set(value) != {"actions"}:
        return accepted, [{"code": "invalid_response", "fatal": True}]
    raw_actions = value.get("actions")
    if not isinstance(raw_actions, list):
        return accepted, [{"code": "invalid_actions", "fatal": True}]
    positions = {token_id: index for index, token_id in enumerate(owned_token_ids)}
    seen_action_ids: set[str] = set()
    occupied_apply_tokens: set[str] = set()
    for item_index, raw in enumerate(raw_actions):
        reason = ""
        if not isinstance(raw, Mapping) or set(raw) != _CALIBRATION_ACTION_FIELDS:
            reason = "action fields do not match the frozen contract"
        action = dict(raw) if isinstance(raw, Mapping) else {}
        action_id = str(action.get("action_id", "")).strip()
        kind = str(action.get("kind", ""))
        token_ids = action.get("token_ids")
        before_text = str(action.get("before_text", ""))
        after_text = str(action.get("after_text", ""))
        confidence = str(action.get("confidence", ""))
        disposition = str(action.get("disposition", ""))
        evidence = action.get("evidence")
        if not reason and (
            not action_id or action_id in seen_action_ids
            or kind not in {"set_case", "set_punctuation", "replace_token", "replace_span"}
            or confidence not in {"high", "medium", "low"}
            or disposition not in {"apply", "review"}
            or not isinstance(action.get("affects_translation"), bool)
        ):
            reason = "action identity, kind, confidence, or disposition is invalid"
        if not reason and (
            not isinstance(token_ids, list) or not token_ids
            or len(set(str(item) for item in token_ids)) != len(token_ids)
            or any(str(item) not in positions for item in token_ids)
        ):
            reason = "token_ids are not wholly owned by this block"
        normalized_ids = [str(item) for item in token_ids] if isinstance(token_ids, list) else []
        if not reason:
            indexes = [positions[token_id] for token_id in normalized_ids]
            if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
                reason = "token_ids must be contiguous and ordered"
        expected_before = ""
        if not reason:
            expected_before = " ".join(
                str(token_map[token_id].text) for token_id in normalized_ids
            )
        if not reason and (not before_text or before_text != expected_before or not after_text):
            reason = "before_text does not reproduce the bound source tokens"
        if not reason and kind in {"set_case", "set_punctuation", "replace_token"} and len(normalized_ids) != 1:
            reason = f"{kind} must target one token"
        if not reason and kind == "set_case" and (
            any(char.isspace() for char in after_text)
            or _calibration_signature(after_text) != _calibration_signature(before_text)
            or _calibration_suffix(after_text) != _calibration_suffix(before_text)
        ):
            reason = "set_case may change only case"
        if not reason and kind == "set_punctuation" and (
            any(char.isspace() for char in after_text)
            or _calibration_core(after_text) != _calibration_core(before_text)
        ):
            reason = "set_punctuation may change only light punctuation"
        if not reason and kind == "replace_span" and len(after_text.split()) != len(normalized_ids):
            reason = "replace_span must preserve token count"
        if not reason and (
            not isinstance(evidence, list) or not evidence
            or any(
                not isinstance(row, Mapping)
                or set(row) != {"kind", "reference"}
                or str(row.get("kind")) not in _CALIBRATION_EVIDENCE_KINDS
                or not str(row.get("reference", "")).strip()
                for row in evidence
            )
        ):
            reason = "evidence is missing or invalid"
        if not reason and disposition == "apply" and occupied_apply_tokens.intersection(normalized_ids):
            reason = "multiple apply actions target the same token"
        if reason:
            rejected.append({
                "code": "invalid_action", "fatal": False,
                "item_index": item_index, "action_id": action_id, "detail": reason,
            })
            continue
        seen_action_ids.add(action_id)
        if disposition == "apply":
            occupied_apply_tokens.update(normalized_ids)
        accepted.append({**action, "token_ids": normalized_ids})
    return accepted, rejected


@router.post("/projects/{project_id}/ai-calibrate")
def ai_calibrate_project(project_id: str, payload: AiCalibrationRequest) -> dict[str, Any]:
    """Apply model-authored punctuation, casing, terminology and ASR corrections."""
    latest = open_project_store(project_id).load_latest()
    if latest is None:
        raise HTTPException(status_code=404, detail={
            "code": "empty_project", "message": "项目还没有文稿版本"
        })
    if payload.expected_revision_id != latest.revision_id:
        raise HTTPException(status_code=409, detail={
            "code": "revision_conflict", "message": "AI 校准基于旧版本，请刷新后重试"
        })
    try:
        exclusive_task = start_editor_ai_task(
            project_job_path(project_id),
            project_id=project_id,
            kind=EditorAiTaskKind.CALIBRATION,
            based_on_revision_id=latest.revision_id,
        )
    except EditorAiTaskConflict as exc:
        raise HTTPException(status_code=423, detail={
            "code": "editor_ai_task_locked", "message": str(exc)
        }) from exc
    task = _write_editor_task(
        project_id, "calibration", status="running", progress=0.02,
        message="AI 校准准备中", task_id=exclusive_task["task_id"],
    )
    try:
        with editor_ai_task_context(task["task_id"]):
            result = _ai_calibrate_project(project_id, payload, task["task_id"])
        finish_editor_ai_task(
            project_job_path(project_id),
            task["task_id"],
            EditorAiTaskState.SUCCEEDED,
            result_revision_id=_revision_id(result["revision"]),
        )
        return result
    except EditorAiTaskCancelled as exc:
        _write_editor_task(
            project_id, "calibration", status="cancelled", progress=0.0,
            message="AI 校准已取消", task_id=task["task_id"],
        )
        finish_editor_ai_task(
            project_job_path(project_id),
            task["task_id"],
            EditorAiTaskState.CANCELLED,
        )
        raise HTTPException(status_code=409, detail={
            "code": "editor_ai_task_cancelled", "message": str(exc)
        }) from exc
    except Exception as exc:
        _write_editor_task(
            project_id, "calibration", status="failed", progress=0.0,
            message="AI 校准失败", error=str(exc), task_id=task["task_id"],
        )
        finish_editor_ai_task(
            project_job_path(project_id),
            task["task_id"],
            EditorAiTaskState.FAILED,
            error={"code": "calibration_failed", "message": str(exc)[:2000]},
        )
        raise


def _exchange_prompt_options(project_id: str) -> dict[str, Any]:
    try:
        settings = get_project_task_info(project_id)
    except HTTPException:
        settings = {}
    source_language = str(settings.get("language") or "Auto")
    target_language = str(settings.get("target_language_mode") or "zh-CN")
    source_limit = int(settings.get("source_hard_limit") or settings.get({
        "en": "english_hard_limit", "zh-CN": "chinese_hard_limit",
        "ja": "japanese_hard_limit", "ko": "korean_hard_limit",
    }.get(source_language, "mixed_hard_limit"), 55))
    target_limit = int(settings.get("target_hard_limit") or settings.get({
        "en": "english_hard_limit", "zh-CN": "chinese_hard_limit",
        "ja": "japanese_hard_limit", "ko": "korean_hard_limit",
    }.get(target_language, "mixed_hard_limit"), 55))
    return {
        "source_language": source_language,
        "target_language": target_language,
        "source_hard_limit": source_limit,
        "target_hard_limit": target_limit,
        "glossary": active_glossary(project_id),
    }


@router.get("/projects/{project_id}/exchange/external-ai-prooftranslation")
def export_external_ai_prooftranslation(project_id: str) -> FileResponse:
    revision = open_project_store(project_id).load_latest()
    if revision is None:
        raise HTTPException(status_code=404, detail="项目还没有文档版本")
    files = external_prooftranslation_files(
        project_id, revision, **_exchange_prompt_options(project_id)
    )
    return _commit_binary_export(
        f"{project_id}_外部AI校译.zip",
        "external-ai-prooftranslation",
        lambda path: write_bytes_zip(path, files),
    )


@router.get("/projects/{project_id}/exchange/external-ai-split")
def export_external_ai_split(project_id: str) -> FileResponse:
    revision = open_project_store(project_id).load_latest()
    if revision is None:
        raise HTTPException(status_code=404, detail="项目还没有文档版本")
    options = _exchange_prompt_options(project_id)
    try:
        files = external_split_files(
            project_id,
            revision,
            source_language=options["source_language"],
            source_hard_limit=options["source_hard_limit"],
            glossary=options["glossary"],
        )
    except ProjectExchangeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _commit_binary_export(
        f"{project_id}_外部AI切分.zip",
        "external-ai-split",
        lambda path: write_bytes_zip(path, files),
    )


@router.get("/projects/{project_id}/exchange/external-ai-edit")
def export_external_ai_edit(project_id: str) -> FileResponse:
    revision = open_project_store(project_id).load_latest()
    if revision is None:
        raise HTTPException(status_code=404, detail="项目还没有文档版本")
    try:
        files = external_edit_files(
            project_id, revision, **_exchange_prompt_options(project_id)
        )
    except ProjectExchangeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _commit_binary_export(
        f"{project_id}_外部AI编辑.zip",
        "external-ai-edit",
        lambda path: write_bytes_zip(path, files),
    )


@router.get("/projects/{project_id}/exchange/external-ai-generation")
def export_external_ai_generation(project_id: str) -> FileResponse:
    revision = open_project_store(project_id).load_latest()
    if revision is None:
        raise HTTPException(status_code=404, detail="项目还没有文档版本")
    try:
        files = external_generation_files(
            project_id, revision, **_exchange_prompt_options(project_id)
        )
    except ProjectExchangeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _commit_binary_export(
        f"{project_id}_外部AI生成.zip",
        "external-ai-generation",
        lambda path: write_bytes_zip(path, files),
    )


@router.get("/projects/{project_id}/exchange/subtitle-project")
def export_subtitle_project_package(project_id: str) -> FileResponse:
    revision = open_project_store(project_id).load_latest()
    if revision is None:
        raise HTTPException(status_code=404, detail="项目还没有文档版本")
    get_project_task_info(project_id)
    return _commit_binary_export(
        f"{project_id}_字幕工程.zip",
        "subtitle-project",
        lambda path: export_subtitle_project(
            path,
            project_id=project_id,
            job_dir=project_job_path(project_id),
            revision=revision,
        ),
    )


@router.post("/projects/{project_id}/external-ai-prooftranslation")
async def import_external_ai_prooftranslation(
    project_id: str,
    file: UploadFile = File(...),
    apply: bool = Form(default=False),
) -> dict[str, Any]:
    revision = open_project_store(project_id).load_latest()
    if revision is None:
        raise HTTPException(status_code=404, detail="项目还没有文档版本")
    try:
        raw = await file.read(20 * 1024 * 1024 + 1)
        if len(raw) > 20 * 1024 * 1024:
            raise ProjectExchangeError("外部 AI 校译文件不能超过 20 MB")
        payload = json.loads(raw.decode("utf-8-sig"))
        if str(payload.get("revision_id", "")) != revision.revision_id:
            # Per-item source hashes still permit a partial safe apply.
            payload = {**payload, "basis_revision_changed": True}
        inspection = inspect_external_prooftranslation(revision.document, payload)
    except (UnicodeError, json.JSONDecodeError, ProjectExchangeError) as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_external_ai_prooftranslation", "message": str(exc)}) from exc
    finally:
        await file.close()
    tracks = (inspection["source"], inspection["translation"])
    result: dict[str, Any] = {
        "schema_version": "substar.external-ai-prooftranslation-inspection.v1",
        "summary": {
            "applicable": sum(len(track["applicable"]) for track in tracks),
            "content_changed": sum(len(track["content_changed"]) for track in tracks),
            "invalid": sum(len(track["invalid"]) for track in tracks),
        },
        **inspection,
    }
    applicable = result["summary"]["applicable"]
    if apply and applicable:
        document = apply_external_prooftranslation(revision.document, inspection)
        provenance = ChangeProvenance(
            kind=ChangeKind.IMPORT,
            operation="external_ai_prooftranslation",
            actor="external-ai",
            metadata={"label": "外部 AI 校译", "applied": applicable},
        )
        result["revision"] = _save_document(
            project_id,
            expected_revision_id=revision.revision_id,
            document=document,
            operation="external_ai_prooftranslation",
            provenance=provenance,
        )
    return result


@router.post("/projects/{project_id}/external-ai-split")
async def import_external_ai_split(
    project_id: str,
    file: UploadFile = File(...),
    apply: bool = Form(default=False),
) -> dict[str, Any]:
    revision = open_project_store(project_id).load_latest()
    if revision is None:
        raise HTTPException(status_code=404, detail="项目还没有文档版本")
    options = _exchange_prompt_options(project_id)
    try:
        raw = await file.read(20 * 1024 * 1024 + 1)
        if len(raw) > 20 * 1024 * 1024:
            raise ProjectExchangeError("外部 AI 切分文件不能超过 20 MB")
        payload = json.loads(raw.decode("utf-8-sig"))
        inspection = inspect_external_split(
            revision.document,
            payload,
            revision_id=revision.revision_id,
            document_hash=revision.document_hash,
            source_hard_limit=options["source_hard_limit"],
        )
    except (UnicodeError, json.JSONDecodeError, ProjectExchangeError) as exc:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_external_ai_split", "message": str(exc)
        }) from exc
    finally:
        await file.close()
    result = dict(inspection)
    if apply and inspection["summary"]["applicable"]:
        document = apply_external_split(revision.document, inspection)
        provenance = ChangeProvenance(
            kind=ChangeKind.IMPORT,
            operation="external_ai_split",
            actor="external-ai",
            metadata={
                "label": "外部 AI 切分",
                "current_cues": inspection["summary"]["current_cues"],
                "proposed_cues": inspection["summary"]["proposed_cues"],
            },
        )
        result["revision"] = _save_document(
            project_id,
            expected_revision_id=revision.revision_id,
            document=document,
            operation="external_ai_split",
            provenance=provenance,
        )
    return result


@router.post("/projects/{project_id}/external-ai-generation")
async def import_external_ai_generation(
    project_id: str,
    file: UploadFile = File(...),
    apply: bool = Form(default=False),
) -> dict[str, Any]:
    store = open_project_store(project_id)
    revision = store.load_latest()
    if revision is None:
        raise HTTPException(status_code=404, detail="项目还没有文档版本")
    options = _exchange_prompt_options(project_id)
    try:
        raw = await file.read(20 * 1024 * 1024 + 1)
        if len(raw) > 20 * 1024 * 1024:
            raise ProjectExchangeError("外部 AI 生成文件不能超过 20 MB")
        payload = json.loads(raw.decode("utf-8-sig"))
        source_revision_id = str(payload.get("source_revision_id", ""))
        if not source_revision_id:
            raise ProjectExchangeError("外部 AI 生成文件缺少源项目版本")
        try:
            source_revision = store.load_revision(source_revision_id)
        except KeyError as exc:
            raise ProjectExchangeError(
                "外部 AI 生成所依据的源项目版本不存在"
            ) from exc
        inspection = inspect_external_generation_checkpoint(
            source_revision.document,
            payload,
            project_id=project_id,
            revision_id=source_revision.revision_id,
            document_hash=source_revision.document_hash,
            source_hard_limit=options["source_hard_limit"],
            target_hard_limit=options["target_hard_limit"],
        )
        proposed_document = apply_external_generation_checkpoint(
            source_revision.document, inspection
        )
    except (UnicodeError, json.JSONDecodeError, ProjectExchangeError) as exc:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_external_ai_generation", "message": str(exc)
        }) from exc
    finally:
        await file.close()
    result = dict(inspection)
    result["source_revision_id"] = source_revision.revision_id
    result["current_revision_id"] = revision.revision_id
    result["summary"] = {
        **inspection["summary"],
        "current_cues": len(revision.document.cues),
        "overwrite_current": source_revision.revision_id != revision.revision_id,
        "applicable": proposed_document.content_hash() != revision.document_hash,
    }
    if apply and result["summary"]["applicable"]:
        provenance = ChangeProvenance(
            kind=ChangeKind.IMPORT,
            operation="external_ai_generation",
            actor="external-ai",
            metadata={
                "label": "外部 AI 生成",
                "checkpoint": inspection["checkpoint"],
                "source_revision_id": source_revision.revision_id,
                "checkpoint_sha256": str(payload["checkpoint_sha256"]),
                **result["summary"],
            },
        )
        result["revision"] = _save_document(
            project_id,
            expected_revision_id=revision.revision_id,
            document=proposed_document,
            operation="external_ai_generation",
            provenance=provenance,
        )
    return result


@router.post("/project-imports/subtitle-project")
async def import_subtitle_project_package(file: UploadFile = File(...)) -> dict[str, Any]:
    upload_handle = tempfile.NamedTemporaryFile(prefix="substar-upload-", suffix=".zip", delete=False)
    upload_path = Path(upload_handle.name)
    try:
        while chunk := await file.read(1024 * 1024):
            upload_handle.write(chunk)
        upload_handle.flush()
        upload_handle.close()
        with upload_path.open("rb") as source:
            project_id = import_subtitle_project(source, projects_root=_projects_root())
    except (ProjectExchangeError, zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_subtitle_project", "message": str(exc)}) from exc
    finally:
        if not upload_handle.closed:
            upload_handle.close()
        upload_path.unlink(missing_ok=True)
        await file.close()
    return {"schema_version": "substar.subtitle-project-import.v1", "project_id": project_id}


def _ai_calibrate_project(
    project_id: str, payload: AiCalibrationRequest, task_id: str
) -> dict[str, Any]:
    store = open_project_store(project_id)
    latest = store.load_latest()
    if latest is None:
        raise HTTPException(status_code=404, detail={"code": "empty_project", "message": "项目还没有文稿版本"})
    if payload.expected_revision_id != latest.revision_id:
        raise HTTPException(status_code=409, detail={"code": "revision_conflict", "message": "AI 校准基于旧版本，请刷新后重试"})
    token_map, cues = _editor_ai_cues(latest)
    if not token_map:
        _write_editor_task(project_id, "calibration", status="completed", progress=1.0,
                           message="AI 校准完成", task_id=task_id)
        return {"revision": latest.to_dict(), "corrections": [], "failed_blocks": []}

    blocks = _editor_ai_group_blocks(cues)
    request_blocks = _calibration_model_blocks(blocks)
    settings = load_settings(include_secret=True)
    calibration_glossary = [
        {
            "source": row.get("source"),
            "standard_source": row.get("standard_source"),
            "aliases": row.get("aliases", []),
        }
        for row in active_glossary(project_id)
    ]
    calibration_prompt = render_prompt(
        "calibration",
        variant=(
            "zh"
            if source_language_for_text(
                " ".join(
                    str(token["text"])
                    for cue in cues
                    for token in cue["tokens"]
                )
            ) == "zh-CN"
            else "en"
        ),
    ).text
    if calibration_glossary:
        calibration_prompt += (
            "\n\nAuthoritative glossary snapshot:\n"
            + json.dumps(calibration_glossary, ensure_ascii=False, separators=(",", ":"))
        )
    results = _run_editor_ai_blocks(
        settings=settings,
        system_prompt=calibration_prompt,
        blocks=request_blocks,
        failure_key="actions",
        stage_name="calibration",
        retry_stage="audit_repair",
        response_validator=lambda value: (
            isinstance(value, Mapping)
            and isinstance(value.get("actions"), list)
        ),
        progress_callback=lambda done, total: _write_editor_task(
            project_id, "calibration", status="running",
            progress=0.08 + 0.82 * done / max(1, total),
            message=f"AI 校准 {done}/{total} 块", task_id=task_id,
        ),
    )

    desired_text: dict[str, str] = {}
    failed_blocks: list[str] = []
    problem_cue_ids: list[str] = []
    request_metadata: list[dict[str, Any]] = []
    calibration_audit_blocks: list[dict[str, Any]] = []
    accepted_contract_actions: list[dict[str, Any]] = []
    review_actions: list[dict[str, Any]] = []
    token_to_cue_id = {
        str(token["token_id"]): str(cue["cue_id"])
        for cue in cues
        for token in cue["tokens"]
    }
    sentence_count = internal_count = case_suggested = lexical_count = filtered_count = 0
    for block_id, value, metadata in sorted(results):
        request_metadata.append({"block_id": block_id, "request_metadata": metadata})
        owned_cues = [cue for cue in blocks.get(block_id, []) if cue["editable"]]
        token_ids = [
            str(token["token_id"])
            for cue in owned_cues for token in cue["tokens"]
        ]
        if metadata.get("error"):
            actions: list[dict[str, Any]] = []
            validation_rejections = [{
                "code": "model_request_failed",
                "fatal": True,
                "detail": str(metadata.get("error")),
            }]
            failed_blocks.append(block_id)
        else:
            actions, validation_rejections = _validated_calibration_contract_actions(
                value, token_ids, token_map
            )
        accepted_contract_actions.extend(actions)
        filtered_count += len(validation_rejections)
        block_problem_cue_ids: set[str] = set()
        for rejection in validation_rejections:
            rejected_action_id = str(rejection.get("action_id", ""))
            rejected_action = next(
                (
                    row for row in value.get("actions", [])
                    if isinstance(row, Mapping)
                    and str(row.get("action_id", "")) == rejected_action_id
                ),
                None,
            ) if isinstance(value, Mapping) else None
            if isinstance(rejected_action, Mapping):
                block_problem_cue_ids.update(
                    token_to_cue_id.get(str(token_id), "")
                    for token_id in rejected_action.get("token_ids", [])
                    if token_to_cue_id.get(str(token_id))
                )
            if rejection.get("fatal"):
                block_problem_cue_ids.update(str(cue["cue_id"]) for cue in owned_cues)
        problem_cue_ids.extend(sorted(block_problem_cue_ids))
        calibration_audit_blocks.append({
            "block_id": block_id,
            "owned_cue_ids": [str(cue["cue_id"]) for cue in owned_cues],
            "request_metadata": metadata,
            "raw_response": value,
            "accepted_actions": actions,
            "filtered_actions": validation_rejections,
        })
        if metadata.get("error"):
            continue
        for action in actions:
            kind = str(action["kind"])
            if action["disposition"] == "review":
                review_actions.append(action)
                block_problem_cue_ids.update(
                    token_to_cue_id.get(token_id, "")
                    for token_id in action["token_ids"]
                    if token_to_cue_id.get(token_id)
                )
                continue
            replacement_parts = (
                str(action["after_text"]).split()
                if kind == "replace_span"
                else [str(action["after_text"])]
            )
            for token_id, replacement_text in zip(
                action["token_ids"], replacement_parts, strict=True
            ):
                desired_text[token_id] = replacement_text
            if kind == "set_case":
                case_suggested += 1
            elif kind == "set_punctuation":
                if _calibration_suffix(str(action["after_text"])) in {".", "?", "!"}:
                    sentence_count += 1
                else:
                    internal_count += 1
            else:
                lexical_count += 1
        problem_cue_ids.extend(sorted(block_problem_cue_ids))

    replacements = [
        BatchReplacement(
            token_id=token_id,
            text=text,
            expected_text=token_map[token_id].text,
        )
        for token_id, text in desired_text.items()
        if text != token_map[token_id].text
        and text.strip() == text
        and text
        and not any(char.isspace() for char in text)
    ]
    applied_case_count = sum(
        _calibration_core(item.text)
        != _calibration_core(str(item.expected_text or ""))
        for item in replacements
    )
    applied_punctuation_count = sum(
        _calibration_suffix(item.text)
        != _calibration_suffix(str(item.expected_text or ""))
        for item in replacements
    )
    translation_stale_cue_ids = list(dict.fromkeys(
        token_to_cue_id[token_id]
        for action in accepted_contract_actions
        if action["disposition"] == "apply" and action["affects_translation"]
        for token_id in action["token_ids"]
        if token_id in token_to_cue_id
    ))
    calibration_metadata = {
        "execution_blocks": request_metadata,
        "failed_blocks": failed_blocks,
        "calibration_problem_cue_ids": list(dict.fromkeys(problem_cue_ids)),
        "allowed_changes": "frozen_calibration_contract",
        "sentence_count": sentence_count,
        "filtered_count": filtered_count,
        "lexical_replacement_count": lexical_count,
        "translation_stale_cue_ids": translation_stale_cue_ids,
        "semantic_group_count": len({str(cue.get("group_id")) for cue in cues}),
        "single_attempt_delivery": True,
    }
    raise_if_task_cancelled(task_id)
    if blocks and len(failed_blocks) == len(blocks):
        # A provider-wide failure is not a completed calibration. In
        # particular, do not append an empty AI revision whose fatal block
        # markers make every owned cue appear in the problem-subtitle list.
        # Persist this attempt before raising so the next diagnosis never has
        # to rely on a stale successful/failed audit artifact.
        failure_errors = list(dict.fromkeys(
            str(item.get("request_metadata", {}).get("error", "")).strip()
            for item in request_metadata
            if str(item.get("request_metadata", {}).get("error", "")).strip()
        ))
        failure_audit_path = project_job_path(project_id) / "calibration" / "audit.json"
        atomic_write_json(
            failure_audit_path,
            {
                "schema_version": "substar.calibration-audit.v1",
                "project_id": project_id,
                "task_id": task_id,
                "based_on_revision_id": latest.revision_id,
                "result_revision_id": None,
                "blocks": calibration_audit_blocks,
                "summary": {
                    "checked_cues": len(cues),
                    "block_count": len(blocks),
                    "failed_blocks": failed_blocks,
                    "filtered_count": filtered_count,
                    "replacement_count": 0,
                    "sentence_count": sentence_count,
                    "case_applied_count": 0,
                    "punctuation_applied_count": 0,
                    "lexical_replacement_count": lexical_count,
                },
            },
        )
        detail = failure_errors[0] if failure_errors else "模型未返回错误详情"
        raise RuntimeError(
            f"AI 校准所有执行块均失败：{detail}"
        )
    if replacements:
        revision = batch_replace_project(project_id, BatchReplaceRequest(
            expected_revision_id=latest.revision_id,
            operation_id=f"op_calibration_{latest.revision_id}",
            replacements=replacements,
            origin="ai_calibration",
            metadata=calibration_metadata,
        ))
    else:
        provenance = ChangeProvenance(
            kind=ChangeKind.AI,
            operation="ai_calibration_apply",
            actor="ai-calibration",
            metadata={**calibration_metadata, "replacement_count": 0},
        )
        document = replace(latest.document, changes=(*latest.document.changes, provenance))
        revision = _save_document(
            project_id,
            expected_revision_id=latest.revision_id,
            document=document,
            operation="ai_calibration_apply",
            provenance=provenance,
        )
    calibration_directory = project_job_path(project_id) / "calibration"
    result_path = calibration_directory / "latest.json"
    audit_path = calibration_directory / "audit.json"
    audit_error = ""
    try:
        atomic_write_json(
            audit_path,
            {
                "schema_version": "substar.calibration-audit.v1",
                "project_id": project_id,
                "task_id": task_id,
                "based_on_revision_id": latest.revision_id,
                "result_revision_id": _revision_id(revision),
                "blocks": calibration_audit_blocks,
                "summary": {
                    "checked_cues": len(cues),
                    "block_count": len(blocks),
                    "failed_blocks": failed_blocks,
                    "filtered_count": filtered_count,
                    "replacement_count": len(replacements),
                    "sentence_count": sentence_count,
                    "case_applied_count": applied_case_count,
                    "punctuation_applied_count": applied_punctuation_count,
                    "lexical_replacement_count": lexical_count,
                },
            },
        )
        atomic_write_json(
            result_path,
            {
                "schema_version": "substar.calibration-result.v1",
                "task_id": task_id,
                "project_id": project_id,
                "based_on_revision_id": latest.revision_id,
                "actions": accepted_contract_actions,
            },
        )
    except (OSError, TypeError, ValueError) as exc:
        audit_error = str(exc)
    result = {
        "revision": revision,
        "corrections": [item.model_dump() for item in replacements],
        "failed_blocks": failed_blocks,
        "problem_cue_ids": list(dict.fromkeys(problem_cue_ids)),
        "checked_cues": len(cues),
        "block_count": len(blocks),
        "semantic_group_count": calibration_metadata["semantic_group_count"],
        "suggested_count": sentence_count + internal_count + case_suggested + lexical_count + len(review_actions),
        "filtered_count": filtered_count,
        "sentence_count": sentence_count,
        "case_applied_count": applied_case_count,
        "punctuation_applied_count": applied_punctuation_count,
        "lexical_replacement_count": lexical_count,
        "review_actions": review_actions,
        "translation_stale_cue_ids": translation_stale_cue_ids,
        "calibration_result_path": str(result_path.relative_to(project_job_path(project_id))),
        "calibration_audit_path": str(audit_path.relative_to(project_job_path(project_id))),
        "duration_seconds": round(sum(
            float(item.get("request_metadata", {}).get("duration_seconds", 0) or 0)
            for item in request_metadata
        ), 3),
    }
    if audit_error:
        result["calibration_audit_error"] = audit_error
    _write_editor_task(project_id, "calibration", status="completed", progress=1.0,
                       message=(
                           f"AI 校准完成：应用 {len(replacements)} 项，过滤 {filtered_count} 项"
                       ), task_id=task_id)
    return result




_SOURCE_REVIEW_TYPES = {
    "suspected_misrecognition", "suspected_omission", "suspected_repetition",
    "named_entity_or_term", "number_or_unit", "context_incoherence",
    "source_consistency",
}
_TRANSLATION_REVIEW_TYPES = {
    "mistranslation", "omission", "addition", "factual_mismatch",
    "polarity_or_logic", "reference_resolution", "terminology_consistency",
    "grammar_or_fluency", "subtitle_flow",
}
_SOURCE_REVIEW_ACTIONS = {
    "inspect_audio", "replace_source", "verify_entity", "verify_number",
    "normalize_source_occurrences", "manual_edit",
}
_TRANSLATION_REVIEW_ACTIONS = {
    "replace_translation", "retranslate_cue", "verify_fact", "inspect_context",
    "normalize_translation_occurrences", "manual_edit",
}


def _review_text_is_damaged(value: Any) -> bool:
    if isinstance(value, str):
        return "\ufffd" in value
    if isinstance(value, Mapping):
        return any(_review_text_is_damaged(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_review_text_is_damaged(child) for child in value)
    return False


def _review_response_valid(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and any(
            isinstance(value.get(key), list)
            for key in ("source_issues", "translation_issues")
        )
        and not _review_text_is_damaged(value)
    )


def _validate_review_issue(
    raw: Any,
    *,
    track: str,
    owned_cue_ids: set[str],
    token_ids_by_cue: Mapping[str, set[str]],
) -> dict[str, Any] | None:
    token_field = "token_ids" if track == "source" else "source_token_ids"
    expected = {
        "issue_type", "cue_ids", token_field, "impact", "confidence",
        "description", "evidence", "suggested_text", "recommended_action",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        return None
    issue_types = _SOURCE_REVIEW_TYPES if track == "source" else _TRANSLATION_REVIEW_TYPES
    actions = _SOURCE_REVIEW_ACTIONS if track == "source" else _TRANSLATION_REVIEW_ACTIONS
    cue_ids = [str(value) for value in raw.get("cue_ids", [])]
    token_ids = [str(value) for value in raw.get(token_field, [])]
    allowed_tokens = {
        token_id for cue_id in cue_ids for token_id in token_ids_by_cue.get(cue_id, set())
    }
    suggested = raw.get("suggested_text")
    if (
        not cue_ids or len(set(cue_ids)) != len(cue_ids)
        or any(cue_id not in owned_cue_ids for cue_id in cue_ids)
        or len(set(token_ids)) != len(token_ids)
        or any(token_id not in allowed_tokens for token_id in token_ids)
        or str(raw.get("issue_type")) not in issue_types
        or str(raw.get("recommended_action")) not in actions
        or str(raw.get("impact")) not in {"major", "moderate", "minor"}
        or str(raw.get("confidence")) not in {"high", "medium", "low"}
        or not str(raw.get("description", "")).strip()
        or not str(raw.get("evidence", "")).strip()
        or (suggested is not None and not isinstance(suggested, str))
    ):
        return None
    return {
        "issue_type": str(raw["issue_type"]),
        "cue_ids": cue_ids,
        token_field: token_ids,
        "impact": str(raw["impact"]),
        "confidence": str(raw["confidence"]),
        "description": str(raw["description"]).strip(),
        "evidence": str(raw["evidence"]).strip(),
        "suggested_text": suggested.strip() if isinstance(suggested, str) else None,
        "recommended_action": str(raw["recommended_action"]),
        "status": "open",
    }


def _review_issue_cue_basis(
    issue: Mapping[str, Any],
    *,
    track: str,
    cues_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Freeze only the Cue content on which one advisory issue depends."""
    basis: list[dict[str, Any]] = []
    for cue_id in issue.get("cue_ids", []):
        cue = cues_by_id.get(str(cue_id))
        if cue is None:
            continue
        snapshot: dict[str, Any] = {
            "cue_id": str(cue_id),
            "source_tokens": [
                {
                    "token_id": str(token.get("token_id", "")),
                    "text": str(token.get("text", "")),
                }
                for token in cue.get("tokens", [])
            ],
        }
        if track == "translation":
            snapshot["target_text"] = str(cue.get("target_text", ""))
        basis.append(snapshot)
    return basis


@router.post("/projects/{project_id}/ai-review")
def ai_review_project(project_id: str, payload: AiReviewRequest) -> dict[str, Any]:
    """Return advisory review issues without mutating the document."""
    latest = open_project_store(project_id).load_latest()
    if latest is None:
        raise HTTPException(status_code=404, detail={
            "code": "empty_project", "message": "项目还没有文稿版本"
        })
    if payload.expected_revision_id != latest.revision_id:
        raise HTTPException(status_code=409, detail={
            "code": "revision_conflict", "message": "AI 审阅基于旧版本，请刷新后重试"
        })
    try:
        exclusive_task = start_editor_ai_task(
            project_job_path(project_id),
            project_id=project_id,
            kind=EditorAiTaskKind.REVIEW,
            based_on_revision_id=latest.revision_id,
        )
    except EditorAiTaskConflict as exc:
        raise HTTPException(status_code=423, detail={
            "code": "editor_ai_task_locked", "message": str(exc)
        }) from exc
    task = _write_editor_task(
        project_id, "review", status="running", progress=0.02,
        message="AI 审阅准备中", task_id=exclusive_task["task_id"],
    )
    try:
        with editor_ai_task_context(task["task_id"]):
            result = _ai_review_project(project_id, payload, task["task_id"])
        finish_editor_ai_task(
            project_job_path(project_id),
            task["task_id"],
            EditorAiTaskState.SUCCEEDED,
        )
        return result
    except EditorAiTaskCancelled as exc:
        _write_editor_task(
            project_id, "review", status="cancelled", progress=0.0,
            message="AI 审阅已取消", task_id=task["task_id"],
        )
        finish_editor_ai_task(
            project_job_path(project_id),
            task["task_id"],
            EditorAiTaskState.CANCELLED,
        )
        raise HTTPException(status_code=409, detail={
            "code": "editor_ai_task_cancelled", "message": str(exc)
        }) from exc
    except Exception as exc:
        _write_editor_task(
            project_id, "review", status="failed", progress=0.0,
            message="AI 审阅失败", error=str(exc), task_id=task["task_id"],
        )
        finish_editor_ai_task(
            project_job_path(project_id),
            task["task_id"],
            EditorAiTaskState.FAILED,
            error={"code": "review_failed", "message": str(exc)[:2000]},
        )
        raise


def _ai_review_project(
    project_id: str, payload: AiReviewRequest, task_id: str
) -> dict[str, Any]:

    store = open_project_store(project_id)
    latest = store.load_latest()
    if latest is None:
        raise HTTPException(status_code=404, detail={"code": "empty_project", "message": "项目还没有文档版本"})
    if payload.expected_revision_id != latest.revision_id:
        raise HTTPException(status_code=409, detail={"code": "revision_conflict", "message": "AI 审阅基于旧版本，请刷新后重试"})
    _token_map, cues = _editor_ai_cues(latest)
    blocks = _editor_ai_blocks(cues)
    prompt = render_prompt("editor_review").text
    review_glossary = [
        {
            "source": row.get("source"),
            "standard_source": row.get("standard_source"),
            "aliases": row.get("aliases", []),
        }
        for row in active_glossary(project_id)
    ]
    if review_glossary:
        prompt += (
            "\n\nAuthoritative glossary snapshot:\n"
            + json.dumps(review_glossary, ensure_ascii=False, separators=(",", ":"))
        )
    if payload.instruction.strip():
        prompt += "\n\n用户本次补充审阅重点：\n" + payload.instruction.strip()
    settings = load_settings(include_secret=True)
    results = _run_editor_ai_blocks(
        settings=settings, system_prompt=prompt, blocks=blocks,
        failure_key="issues", stage_name="review",
        retry_stage=None,
        response_validator=_review_response_valid,
        progress_callback=lambda done, total: _write_editor_task(
            project_id, "review", status="running",
            progress=0.08 + 0.82 * done / max(1, total),
            message=f"AI 审阅 {done}/{total} 块", task_id=task_id,
        ),
    )
    result_by_block = {block_id: (value, metadata) for block_id, value, metadata in results}
    repair_blocks: dict[str, list[dict[str, Any]]] = {}
    repair_tracks: dict[str, list[str]] = {}
    for block_id, block_cues in blocks.items():
        value, metadata = result_by_block.get(block_id, ({}, {"error": "missing response"}))
        missing = [
            track
            for track, key in (("source", "source_issues"), ("translation", "translation_issues"))
            if metadata.get("error")
            or not isinstance(value, Mapping)
            or not isinstance(value.get(key), list)
        ]
        if missing:
            repair_tracks[block_id] = missing
            repair_blocks[block_id] = block_cues
    if repair_blocks:
        repaired = _run_editor_ai_blocks(
            settings=settings,
            system_prompt=(
                prompt
                + "\n\n这是精确修复请求。只补齐 requested_tracks；"
                  "不得改写已经验收成功的另一轨结果。\nrequested_tracks="
                + json.dumps(repair_tracks, ensure_ascii=False, separators=(",", ":"))
            ),
            blocks=repair_blocks,
            failure_key="issues",
            stage_name="audit_repair",
            retry_stage=None,
            response_validator=_review_response_valid,
        )
        for block_id, repair_value, repair_metadata in repaired:
            original, original_metadata = result_by_block.get(block_id, ({}, {}))
            merged = dict(original) if isinstance(original, Mapping) else {}
            if isinstance(repair_value, Mapping):
                for track, key in (("source", "source_issues"), ("translation", "translation_issues")):
                    if track in repair_tracks[block_id] and isinstance(repair_value.get(key), list):
                        merged[key] = repair_value[key]
            result_by_block[block_id] = (merged, {**original_metadata, "repair": repair_metadata})
        results = [(block_id, *result_by_block[block_id]) for block_id in blocks]
    owned_cues = {
        block_id: {cue["cue_id"] for cue in block_cues if cue["editable"]}
        for block_id, block_cues in blocks.items()
    }
    token_ids_by_cue = {
        str(cue["cue_id"]): {str(token["token_id"]) for token in cue["tokens"]}
        for cue in cues
    }
    cues_by_id = {str(cue["cue_id"]): cue for cue in cues}
    source_issues: list[dict[str, Any]] = []
    translation_issues: list[dict[str, Any]] = []
    rejected_issue_count = 0
    failed_blocks: list[str] = []
    for block_id, value, metadata in sorted(results):
        if metadata.get("error"):
            failed_blocks.append(block_id)
        if not isinstance(value, Mapping):
            rejected_issue_count += 1
            continue
        for track, key, destination in (
            ("source", "source_issues", source_issues),
            ("translation", "translation_issues", translation_issues),
        ):
            raw_rows = value.get(key)
            if not isinstance(raw_rows, list):
                rejected_issue_count += 1
                continue
            for raw in raw_rows:
                issue = _validate_review_issue(
                    raw,
                    track=track,
                    owned_cue_ids=owned_cues.get(block_id, set()),
                    token_ids_by_cue=token_ids_by_cue,
                )
                if issue is None:
                    rejected_issue_count += 1
                    continue
                issue["issue_id"] = (
                    f"review_{track}_{block_id}_{len(destination) + 1:04d}"
                )
                destination.append(issue)
    review_id = f"review_{latest.revision_id}_{task_id[-8:]}"
    source_result = {
        "schema_version": "substar.source-review-result.v1",
        "review_id": review_id,
        "project_id": project_id,
        "based_on_revision_id": latest.revision_id,
        "issues": source_issues,
    }
    translation_result = {
        "schema_version": "substar.translation-review-result.v1",
        "review_id": review_id,
        "project_id": project_id,
        "based_on_revision_id": latest.revision_id,
        "issues": translation_issues,
    }
    result = {
        "review_id": review_id,
        "based_on_revision_id": latest.revision_id,
        "source_issues": source_issues,
        "translation_issues": translation_issues,
        "issues": [
            *(
                {
                    **row,
                    "track": "source",
                    "cue_basis": _review_issue_cue_basis(
                        row, track="source", cues_by_id=cues_by_id
                    ),
                }
                for row in source_issues
            ),
            *(
                {
                    **row,
                    "track": "translation",
                    "cue_basis": _review_issue_cue_basis(
                        row, track="translation", cues_by_id=cues_by_id
                    ),
                }
                for row in translation_issues
            ),
        ],
        "failed_blocks": failed_blocks,
        "rejected_issue_count": rejected_issue_count,
        "execution_blocks": [
            {"block_id": block_id, "request_metadata": metadata}
            for block_id, _value, metadata in sorted(results)
        ],
        "provider_seconds": round(sum(
            float(metadata.get("duration_seconds", 0) or 0)
            for _block_id, _value, metadata in results
        ), 3),
        "failed_block_errors": {
            block_id: str(metadata.get("error", ""))
            for block_id, _value, metadata in results
            if metadata.get("error")
        },
    }
    raise_if_task_cancelled(task_id)
    review_dir = store.root.parent / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(review_dir / "source_latest.json", source_result)
    atomic_write_json(review_dir / "translation_latest.json", translation_result)
    atomic_write_json(review_dir / "latest.json", result)
    if blocks and len(failed_blocks) == len(blocks):
        raise RuntimeError("AI 审阅所有执行块均失败，请检查发行文件或模型连接")
    _write_editor_task(project_id, "review", status="completed", progress=1.0,
                       message="AI 审阅完成", task_id=task_id)
    return result


@router.get("/projects/{project_id}/ai-review/latest")
def latest_ai_review_project(project_id: str) -> dict[str, Any]:
    path = project_job_path(project_id) / "review" / "latest.json"
    if not path.is_file():
        return {"review_id": "", "based_on_revision_id": "", "issues": [], "failed_blocks": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail={"code": "review_artifact_invalid", "message": str(exc)}) from exc
    if _review_text_is_damaged(value):
        return {
            "review_id": str(value.get("review_id", "")),
            "based_on_revision_id": str(value.get("based_on_revision_id", "")),
            "issues": [],
            "failed_blocks": list(value.get("failed_blocks", [])),
            "encoding_error": True,
        }
    return value


@router.patch("/projects/{project_id}/ai-review/issues/{issue_id}")
def set_ai_review_issue_status(
    project_id: str, issue_id: str, payload: ReviewIssueStatusRequest
) -> dict[str, Any]:
    review_dir = project_job_path(project_id) / "review"
    combined_path = review_dir / "latest.json"
    try:
        combined = json.loads(combined_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail={
            "code": "review_not_found", "message": "项目还没有可更新的审阅结果"
        }) from exc
    selected: dict[str, Any] | None = None
    for issue in combined.get("issues", []):
        if isinstance(issue, dict) and issue.get("issue_id") == issue_id:
            issue["status"] = payload.status
            selected = issue
            break
    if selected is None:
        raise HTTPException(status_code=404, detail={
            "code": "review_issue_not_found", "message": "审阅问题不存在"
        })
    track = str(selected.get("track"))
    track_path = review_dir / (
        "source_latest.json" if track == "source" else "translation_latest.json"
    )
    try:
        track_result = json.loads(track_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail={
            "code": "review_artifact_invalid", "message": str(exc)
        }) from exc
    for issue in track_result.get("issues", []):
        if isinstance(issue, dict) and issue.get("issue_id") == issue_id:
            issue["status"] = payload.status
            break
    atomic_write_json(track_path, track_result)
    atomic_write_json(combined_path, combined)
    return selected


@router.post("/projects/{project_id}/complete")
def set_project_complete(
    project_id: str, payload: CompleteDocumentRequest
) -> dict[str, Any]:
    if _tutorial_project(project_id) is not None:
        raise HTTPException(status_code=409, detail={
            "code": "tutorial_completion_forbidden", "message": "教程案例始终以绿色角标识，不能标记完成"
        })
    store = open_project_store(project_id)
    latest = store.load_latest()
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "empty_project", "message": "项目还没有文档版本"},
        )
    document = replace(
        latest.document,
        properties=replace(latest.document.properties, complete=payload.complete),
    )
    # Completion is a new editable document revision, never a lifecycle lock.
    return _save_document(
        project_id,
        expected_revision_id=payload.expected_revision_id,
        document=document,
        operation="set_complete_attribute",
    )


@router.post("/projects/{project_id}/validate")
def validate_project(
    project_id: str, payload: ValidateDocumentRequest
) -> dict[str, Any]:
    revision = open_project_store(project_id).load_latest()
    if revision is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "empty_project", "message": "项目还没有文档版本"},
        )
    report = validate_revision(
        revision.document,
        revision_id=revision.revision_id,
        policy=ValidationPolicy(
            source_hard_limit=payload.source_hard_limit,
            target_hard_limit=payload.target_hard_limit,
            count_spaces=payload.count_spaces,
            count_punctuation=payload.count_punctuation,
        ),
    )
    return report.to_dict()


@router.post("/projects/{project_id}/translation", status_code=202)
def start_project_translation(
    project_id: str, payload: TranslationStartRequest
) -> dict[str, Any]:
    job_dir = project_job_path(project_id)
    settings = load_settings(include_secret=False)
    try:
        settings.update(task_info_settings(load_task_info(job_dir, project_id)))
    except (OSError, TypeError, ValueError):
        pass
    settings["target_language_mode"] = payload.target_language
    latest = open_project_store(project_id).load_latest()
    if latest is not None:
        source_text = " ".join(
            token.text for token in latest.document.display_tokens
            if token.state.value == "active"
        )
        source_language = (
            source_language_for_text(source_text)
            if payload.source_language == "Auto"
            else payload.source_language
        )
        settings["translation_source_language_selection"] = payload.source_language
        settings["translation_source_language"] = source_language
        if source_language == payload.target_language:
            language_names = {"zh-CN": "简体中文", "en": "英文", "ja": "日文", "ko": "韩文"}
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "same_translation_language",
                    "message": (
                        f"本次翻译的原始语言为{language_names.get(source_language, source_language)}，"
                        "请选择其他目标语言"
                    ),
                },
            )
    if latest is None:
        raise HTTPException(status_code=404, detail={
            "code": "empty_project", "message": "项目还没有文稿版本"
        })
    if latest.revision_id != payload.expected_revision_id:
        raise HTTPException(status_code=409, detail={
            "code": "revision_conflict", "message": "翻译基于旧版本，请刷新后重试"
        })
    try:
        exclusive_task = start_editor_ai_task(
            job_dir,
            project_id=project_id,
            kind=EditorAiTaskKind.TRANSLATION,
            based_on_revision_id=latest.revision_id,
        )
    except EditorAiTaskConflict as exc:
        raise HTTPException(status_code=423, detail={
            "code": "editor_ai_task_locked", "message": str(exc)
        }) from exc
    try:
        return create_translation_task(
            job_dir,
            expected_revision_id=payload.expected_revision_id,
            workers=payload.workers,
            settings=settings,
            task_id=exclusive_task["task_id"],
        )
    except TranslationTaskError as exc:
        finish_editor_ai_task(
            job_dir,
            exclusive_task["task_id"],
            EditorAiTaskState.FAILED,
            error={"code": "translation_start_failed", "message": str(exc)[:2000]},
        )
        raise HTTPException(
            status_code=409,
            detail={"code": "translation_start_rejected", "message": str(exc)},
        ) from exc
    except (ProjectStoreError, ProjectIntegrityError) as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "project_not_found", "message": str(exc)},
        ) from exc


@router.get("/projects/{project_id}/translation")
def get_project_translation(project_id: str) -> dict[str, Any]:
    job_dir = project_job_path(project_id)
    try:
        state = load_translation_status(job_dir)
    except TranslationTaskError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "translation_status_invalid", "message": str(exc)},
        ) from exc
    if state is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "translation_not_started", "message": "项目尚未启动翻译"},
        )
    return state
