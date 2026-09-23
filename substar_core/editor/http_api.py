from __future__ import annotations

from array import array
import concurrent.futures
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Mapping
from urllib.parse import quote

import json
import mimetypes
import shutil
import sys
import time
import uuid
import wave
import tempfile
import zipfile

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from substar_core.artifacts import atomic_write_json
from substar_core.ai_progress import ai_progress
from substar_core.ai_block_cache import fingerprint, load_ai_block_cache, save_ai_block_cache
from substar_core.model_routing import resolve_stage_request
from substar_core.chinese_script import convert_chinese_script
from substar_core.config import load_settings, settings_for_model_provider
from substar_core.glossary import (
    active_glossary, collect_candidates,
    glossary_prompt,
    load_glossary,
    normalize_entry,
    save_glossary,
)
from substar_core.glossary_xlsx import XLSX_MEDIA_TYPE, glossary_xlsx_bytes
from substar_core.prompt_registry import (
    calibration_variant,
    normalize_source_language,
    render_prompt,
    source_language_for_text,
)
from substar_core.model_providers import MODEL_PROVIDER_CATALOG
from substar_core.manuscript_matching import (
    ManuscriptMatchError,
    editor_reference_operations,
    extract_reference_text,
)
from substar_core.media import (
    WAVEFORM_WINDOW_CACHE,
    pcm_wave_frame_count,
    prepare_playback_media,
    smart_forward_snap,
)
from substar_core.model_gateway import (
    ModelGatewayError,
    ModelGatewayRequestError,
    call_text_model,
    call_translation_model,
)
from substar_core.cue_script import (
    finalize_calibration,
    finalize_calibration_candidate,
    output_contract,
    render_cue_request,
)
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
from substar_core.editor.domain.cue_timing import smart_snap_search_minimum
from substar_core.editor.infrastructure import SQLiteProjectRepository
from substar_core.storage import (
    ProjectConflictError,
    ProjectIntegrityError,
    ProjectStore,
    ProjectStoreError,
)
from substar_core.validation import ValidationPolicy, validate_revision
from substar_core.export import SubtitleExportMode, render_document_srt
from substar_core.editor.translation.artifacts import TRANSLATION_INPUT_SCHEMA
from substar_core.editor.calibration.handler import CALIBRATION_INPUT_SCHEMA
from substar_core.credential_store import model_provider_credential_ref
from substar_core.project_exchange import (
    ProjectExchangeError,
    apply_external_generation_checkpoint,
    apply_external_prooftranslation,
    apply_external_split,
    external_edit_files,
    external_generation_files,
    external_prooftranslation_files,
    external_split_files,
    import_subtitle_project,
    inspect_external_generation_checkpoint,
    inspect_external_prooftranslation,
    inspect_external_split,
    stream_subtitle_project,
    write_bytes_zip,
)
from substar_core.task_info import load_task_info, save_task_info, task_info_settings

from substar_core.editor.calibration.service import (
    AiCalibrationRequest,
    BatchReplacement,
    _apply_ai_calibration_operations,
    _calibration_alnum_signature,
    _calibration_core,
    _calibration_model_blocks,
    _calibration_punctuation_signature,
    _calibration_signature,
    _calibration_suffix,
    _editor_ai_blocks,
    _editor_ai_cues,
    _revision_id,
    _run_editor_ai_blocks,
    _validated_calibration_contract_actions
)

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
    media_selection_token: str = Field(default="", max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    language: Literal["Auto", "mixed", "zh", "zh-CN", "en", "ja", "ko"]
    target_language_mode: Literal["zh-CN", "en", "ja", "ko"]
    source_hard_limit: int = Field(ge=1, le=500)
    target_hard_limit: int = Field(ge=1, le=500)
    glossary_id: str = Field(default="", max_length=80)
    llm_provider_id: Literal[
        "inherit", "deepseek", "glm", "openai", "azure_openai", "deerapi",
        "gemini", "siliconflow", "qwen", "custom",
    ] = "inherit"


class TranslationStartRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)
    workers: int = Field(default=3, ge=1, le=256)
    source_language: Literal["Auto", "mixed", "zh-CN", "en", "ja", "ko"]
    target_language: Literal["zh-CN", "en", "ja", "ko"]
    mapping_mode: Literal["one_to_one", "many_to_many"] = "many_to_many"




class BatchReplaceRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    replacements: list[BatchReplacement] = Field(min_length=1)
    origin: Literal["manual", "ai_calibration", "reference_manuscript"] = "manual"
    metadata: dict[str, Any] = Field(default_factory=dict)


class CheckpointRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)
    label: str = Field(default="", max_length=120)


class SelectiveRestoreRequest(BaseModel):
    expected_revision_id: str
    revision_id: str
    cue_ids: list[str] = Field(min_length=1, max_length=10000)
    scope: Literal["text", "timing", "translation", "style"]


class SplitTokenRequest(BaseModel):
    expected_revision_id: str
    token_id: str
    offset: int = Field(gt=0)


class RestoreRevisionRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    navigation: Literal["undo", "redo", "direct"] = "direct"
    undo_revision_ids: list[str] = Field(default_factory=list, max_length=1000)
    redo_revision_ids: list[str] = Field(default_factory=list, max_length=1000)


class SmartForwardSnapRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)
    pre_roll_ms: int = Field(default=0, ge=0, le=100)
    sensitivity: int = Field(default=50, ge=0, le=100)


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


class ReviewNoteRequest(BaseModel):
    expected_version: int = Field(ge=0)
    expected_revision_id: str
    action: Literal["add", "reply", "resolve", "reopen", "relocate", "accept", "reject", "delete", "keep_local"]
    note_id: str = ""
    cue_id: str = ""
    track: Literal["source", "target"] = "source"
    token_ids: list[str] = Field(default_factory=list, max_length=10000)
    text_range: list[int] | None = None
    text: str = Field(default="", max_length=10000)
    author: str = Field(default="", max_length=100)


@router.get("/projects/{project_id}/review-notes")
def get_review_notes(project_id: str):
    from .application.review_notes import project_note
    from .application.review_store import read_reviews
    latest = open_project_store(project_id).load_latest()
    if latest is None:
        raise HTTPException(404, detail={"message": "工程没有字幕"})
    value = read_reviews(project_job_path(project_id) / "review" / "notes.json")
    return {**value, "revision_id": latest.revision_id,
            "notes": [project_note(latest.document, note) for note in value["notes"] if note.get("status") != "deleted" or note.get("import_conflicts")]}


class ReviewImportRequest(BaseModel):
    expected_version: int = Field(ge=0)
    expected_revision_id: str
    package: dict[str, Any]


@router.get("/projects/{project_id}/review-opinions/export")
def export_review_opinions(project_id: str):
    from .application.review_exchange import export_opinions
    from .application.review_store import read_reviews
    latest = open_project_store(project_id).load_latest()
    if latest is None:
        raise HTTPException(404, detail={"message": "工程没有字幕"})
    value = read_reviews(project_job_path(project_id) / "review" / "notes.json")
    return export_opinions(project_id, latest.revision_id, value["notes"])


@router.post("/projects/{project_id}/review-opinions/import")
def import_review_opinions(project_id: str, payload: ReviewImportRequest):
    from .application.review_exchange import validate_package, merge_opinions
    from .application.review_store import update_reviews
    latest = open_project_store(project_id).load_latest()
    if latest is None or latest.revision_id != payload.expected_revision_id:
        raise HTTPException(409, detail={"message": "字幕版本已变化，请刷新后重新导入"})
    try:
        if len(json.dumps(payload.package, ensure_ascii=False)) > 10_000_000:
            raise ValueError("审阅意见文件不能超过 10 MB")
        incoming = validate_package(payload.package, project_id)
        summary = {}
        def mutate(notes):
            summary.update(merge_opinions(notes, incoming))
        update_reviews(project_job_path(project_id) / "review" / "notes.json", payload.expected_version, mutate)
    except ValueError as exc:
        raise HTTPException(409, detail={"message": str(exc)}) from exc
    return {**get_review_notes(project_id), "import_summary": summary}


@router.post("/projects/{project_id}/review-notes")
def save_review_note(project_id: str, payload: ReviewNoteRequest):
    from .application.review_notes import add_note, change_note, anchor_for, upsert_note
    from .application.review_store import update_reviews
    latest = open_project_store(project_id).load_latest()
    if latest is None or latest.revision_id != payload.expected_revision_id:
        raise HTTPException(409, detail={"message": "字幕版本已变化，请刷新后重新批注"})
    def mutate(notes):
        if payload.action == "add":
            upsert_note(notes, add_note(latest.document, cue_id=payload.cue_id, track=payload.track,
                                  text=payload.text, author=payload.author, token_ids=payload.token_ids, text_range=payload.text_range))
        else:
            index = next((i for i, note in enumerate(notes) if note["id"] == payload.note_id), None)
            if index is None:
                raise ValueError("批注不存在")
            if payload.action == "relocate":
                updated = dict(notes[index])
                updated["anchor_history"] = [*updated.get("anchor_history", []), updated["anchor"]]
                updated["anchor"] = anchor_for(latest.document, payload.cue_id, payload.track, payload.token_ids, payload.text_range)
                notes[index] = updated
            else:
                notes[index] = change_note(notes[index], action=payload.action, text=payload.text, author=payload.author)
    try:
        update_reviews(project_job_path(project_id) / "review" / "notes.json", payload.expected_version, mutate)
    except ValueError as exc:
        raise HTTPException(409, detail={"message": str(exc)}) from exc
    return get_review_notes(project_id)


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
        or value.get("schema_version") != "substar.tutorial-example.v2"
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


def _project_glossary_id(project_id: str) -> str:
    try:
        return str(load_task_info(project_job_path(project_id), project_id).get("glossary_id") or "")
    except (OSError, ValueError):
        return ""


def _collect_generated_hotwords(project_id: str, glossary_id: str) -> list[dict[str, Any]]:
    path = project_job_path(project_id) / "asr_enhancement.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return active_glossary(glossary_id)
    generated = payload.get("hotwords") if isinstance(payload, dict) else None
    if not isinstance(generated, list):
        return active_glossary(glossary_id)
    collect_candidates(generated, "asr_generated", project_id)
    return active_glossary(glossary_id)


def _project_document_payload(document: EditorDocument) -> dict[str, Any]:
    """Serialize one revision with its non-destructive script projection."""
    value = document.to_dict()
    # Full model responses belong to the revision audit, not every display token.
    # Only trim the detached browser projection; stored revisions stay intact.
    for token in value.get("display_tokens", []):
        token.get("provenance", {}).get("metadata", {}).pop("execution_blocks", None)
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
                value = ProjectStore(manifest.parent).load_summary()
            except (OSError, ProjectStoreError, DocumentValidationError):
                continue
            # Beta is a clean schema cutover. Old on-disk projects remain untouched,
            # but they are not advertised to the new editor and therefore cannot
            # trigger a late 500 after project selection.
            if value is None or value["document_schema_version"] != EDITOR_DOCUMENT_SCHEMA:
                continue
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
                    "complete": value["complete"],
                    "updated_at": value["updated_at"],
                    "subtitle_basis": "sentence" if (directory / "subtitle_import.json").is_file() else "word",
                    "tutorial_case_id": str(tutorial["case_id"]) if tutorial else "",
                }
            if display_name:
                project["display_name"] = display_name
            projects.append(project)
    projects.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return {"schema_version": "substar.project-list.v1", "projects": projects}


@router.get("/editor-tasks")
def list_editor_tasks(request: Request) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    for task in request.app.state.task_service.list_tasks(limit=500):
        if task["task_type"] not in {"calibration", "translation"}:
            continue
        project_id = str(task.get("project_id") or "")
        display_name = project_id
        try:
            display_name = load_task_info(project_job_path(project_id), project_id)["display_name"]
        except (OSError, TypeError, ValueError, HTTPException):
            pass
        projection = _runtime_ai_task_projection(task)
        tasks.append({
            **projection,
            "status": task["state"],
            "display_name": display_name,
        })
    tasks.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return {"schema_version": "substar.editor-task-list.v2", "tasks": tasks}


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
    for path in (project_job_path(project_id) / "tutorial_progress.json",):
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

    def register_creation() -> None:
        """Keep packaged tutorials on the same current project shell as real jobs."""
        atomic_write_json(job_dir / "project_creation.json", {
            "schema_version": "substar.project-creation.v2",
            "input_mode": "packaged_example",
            "source_file": "audio_16k_mono.wav",
            "reference_document": "",
            "settings_overrides": {
                "language": str(manifest["source_language"]),
                "target_language_mode": str(manifest["target_language"]),
                "english_hard_limit": int(manifest["source_hard_limit"]),
                "chinese_hard_limit": int(manifest["target_hard_limit"]),
                "segmentation_enabled": True,
                "reference_script_mode": False,
            },
            "profile": {},
            "recognition_profile": {},
            "created_at": time.time(),
            "tutorial_case_id": case_id,
            "simulated": True,
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
        register_creation()
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
        register_creation()
        atomic_write_json(job_dir / "tutorial_project.json", {
            "schema_version": "substar.tutorial-project.v2",
            "case_id": case_id,
            "level": "advanced",
            "display_name": "进阶教程",
            "baseline_revision_id": baseline.revision_id,
            "baseline_document_hash": baseline.document_hash,
            "available_stages": ["segmentation", "calibration", "translation"],
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
    stage: Literal["calibration", "translation"],
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
def get_project_editor_ai_task(project_id: str, request: Request) -> dict[str, Any] | None:
    tasks = request.app.state.task_service.list_tasks(project_id=project_id, limit=20)
    tasks = [task for task in tasks if task["task_type"] in {"calibration", "translation"}]
    if not tasks:
        return None
    task = tasks[0]
    task_input = request.app.state.task_service.get_task_input(task["task_id"])
    return _runtime_ai_task_projection(task, task_input)


def _runtime_ai_task_projection(
    task: Mapping[str, Any], task_input: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Return the single UI projection shared by calibration and translation."""

    completed = int(task.get("completed_units") or 0)
    total = int(task.get("total_units") or 0)
    result = task.get("result") if isinstance(task.get("result"), Mapping) else {}
    error = task.get("error") if isinstance(task.get("error"), Mapping) else {}
    frozen = task_input if isinstance(task_input, Mapping) else {}
    runtime_phase = str(task.get("phase") or "primary")
    display_phase = {
        "primary": "executing",
        "validation": "validating",
        "delivery": (
            "completed"
            if str(task.get("state") or "") in {"succeeded", "succeeded_with_issues"}
            else "publishing"
        ),
        "repair": "repair",
    }.get(runtime_phase, runtime_phase)
    live_progress = task.get("progress_payload")
    result_progress = result.get("ai_progress")
    terminal = str(task.get("state") or "") in {
        "succeeded", "succeeded_with_issues", "failed", "cancelled",
    }
    projected_progress = dict(result_progress) if terminal and isinstance(result_progress, Mapping) else dict(live_progress) if isinstance(live_progress, Mapping) else dict(result_progress) if isinstance(result_progress, Mapping) else {
        "phase": display_phase,
        "message": task.get("progress_message") or "",
        "kind": str(task.get("task_type") or ""),
        "unit_kind": (
            "translation_block" if task.get("task_type") == "translation"
            else "calibration_block"
        ),
        "unit_label": "块",
        "units": {
            "planned": total,
            "completed": completed,
            "repair_planned": total if task.get("phase") == "repair" else 0,
            "repair_completed": completed if task.get("phase") == "repair" else 0,
        },
        "problem_count": len(
            result.get("problem_block_ids") or result.get("problem_cue_ids") or []
        ),
    }
    task_kind = str(task.get("task_type") or "")
    if task_kind in {"translation", "calibration"}:
        projected_progress["kind"] = task_kind
        projected_progress.setdefault(
            "unit_kind",
            "translation_block" if task_kind == "translation" else "calibration_block",
        )
        projected_progress.setdefault(
            "unit_label", "块"
        )
        problem_block_ids = list(result.get("problem_block_ids") or [])
        problem_cue_ids = list(result.get("problem_cue_ids") or [])
        if problem_block_ids:
            projected_progress["problem_count"] = len(problem_block_ids)
            projected_progress["problem_unit_kind"] = "block"
        elif terminal and problem_cue_ids:
            # Older terminal results only persisted Cue ids. Keep that historic
            # count honest instead of relabelling individual Cues as blocks.
            projected_progress["problem_count"] = len(problem_cue_ids)
            projected_progress["problem_unit_kind"] = "cue"
    return {
        **dict(task),
        "kind": task["task_type"],
        "message": task.get("progress_message") or "",
        "display_error": str(error.get("message") or ""),
        "result_revision_id": result.get("result_revision_id"),
        "source_language_selection": frozen.get("source_language_selection"),
        "source_language": frozen.get("source_language"),
        "target_language": frozen.get("target_language"),
        "mapping_mode": result.get("mapping_mode") or frozen.get("mapping_mode"),
        "problem_cue_ids": list(result.get("problem_cue_ids") or []),
        "problem_block_ids": list(result.get("problem_block_ids") or []),
        "ai_progress": projected_progress,
    }


def _editor_ai_idempotency_key(kind: str, task_input: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(task_input), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"{kind}:{sha256(encoded).hexdigest()}"


def _retry_exact_failed_editor_task(
    service: Any,
    *,
    project_id: str,
    task_type: Literal["calibration", "translation"],
    task_input: Mapping[str, Any],
) -> dict[str, Any] | None:
    failed = service.list_tasks(
        project_id=project_id, task_type=task_type, states=["failed"], limit=1
    )
    if not failed:
        return None
    prior_input = service.get_task_input(failed[0]["task_id"])
    if prior_input != dict(task_input):
        return None
    retried = service.retry(failed[0]["task_id"])
    return _runtime_ai_task_projection(retried, prior_input)


@router.delete("/projects/{project_id}/ai-task")
def cancel_project_editor_ai_task(project_id: str, request: Request) -> dict[str, Any]:
    tasks = request.app.state.task_service.list_tasks(
        project_id=project_id,
        states=["queued", "running", "cancelling"],
        limit=20,
    )
    tasks = [task for task in tasks if task["task_type"] in {"calibration", "translation"}]
    if not tasks:
        raise HTTPException(status_code=409, detail={
            "code": "editor_ai_task_cancel_rejected", "message": "当前没有可取消的 AI 任务"
        })
    return request.app.state.task_service.request_cancel(tasks[0]["task_id"])


@router.get("/projects/{project_id}/task-info")
def get_project_task_info(project_id: str) -> dict[str, Any]:
    """Return the project's sole mutable task-information authority."""
    open_project_store(project_id)
    job_dir = project_job_path(project_id)
    try:
        info = load_task_info(job_dir, project_id)
        try:
            media, _ = _project_media_source(project_id)
            info.update(media_path=str(media.resolve()), media_missing=False)
        except HTTPException:
            manifest = json.loads((job_dir / "run_manifest.json").read_text(encoding="utf-8")) if (job_dir / "run_manifest.json").exists() else {}
            info.update(media_path=str(manifest.get("source_path", "")), media_missing=True)
            from substar_core.media_reference import read_reference
            reference=read_reference(job_dir)
            if reference: info['media_path']=reference['path']
        return info
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _project_llm_settings(project_id: str, *, include_secret: bool) -> dict[str, Any]:
    settings = load_settings(include_secret=False)
    info = load_task_info(project_job_path(project_id), project_id)
    provider_id = str(info.get("llm_provider_id") or "inherit")
    if provider_id == "inherit":
        provider_id = str(settings.get("active_model_provider") or "deepseek")
    return settings_for_model_provider(
        provider_id, include_secret=include_secret, base_settings=settings
    )


@router.get("/projects/{project_id}/llm-options")
def get_project_llm_options(project_id: str) -> dict[str, Any]:
    open_project_store(project_id)
    settings = load_settings(include_secret=False)
    info = load_task_info(project_job_path(project_id), project_id)
    profiles = settings.get("model_provider_profiles", {})
    key_set = settings.get("model_provider_key_set", {})
    options = []
    for definition in MODEL_PROVIDER_CATALOG:
        provider_id = str(definition["id"])
        profile = profiles.get(provider_id, {}) if isinstance(profiles, dict) else {}
        if not profile and provider_id == str(settings.get("active_model_provider") or ""):
            profile = {
                "model": settings.get("translation_api_model", ""),
                "base_url": settings.get("translation_api_base_url", ""),
            }
        if not isinstance(profile, dict) or not key_set.get(provider_id):
            continue
        model = str(profile.get("model") or "").strip()
        base_url = str(profile.get("base_url") or "").strip()
        if not model or not base_url:
            continue
        options.append({
            "provider_id": provider_id,
            "label": str(definition["label"]),
            "model": model,
        })
    selected = str(info.get("llm_provider_id") or "inherit")
    active = str(settings.get("active_model_provider") or "deepseek")
    return {
        "selected_provider_id": selected,
        "active_provider_id": active,
        "effective_provider_id": active if selected == "inherit" else selected,
        "options": options,
    }


@router.put("/projects/{project_id}/task-info")
def set_project_task_info(
    project_id: str, payload: ProjectTaskInfoRequest
) -> dict[str, Any]:
    """Atomically update metadata without mutating any existing subtitle content."""
    open_project_store(project_id)
    try:
        job_dir = project_job_path(project_id)
        selected = None
        if payload.media_selection_token:
            from substar_core.media_relink import resolve_selection
            selected = resolve_selection(project_id, payload.media_selection_token)
        info = save_task_info(job_dir, project_id, payload.model_dump())
        if selected:
            from substar_core.media_reference import write_reference
            write_reference(job_dir, selected)
            manifest_path = job_dir / "run_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
            manifest.update(source_path=selected, source_file=Path(selected).name, media_relinked=True)
            atomic_write_json(manifest_path, manifest)
        return get_project_task_info(project_id)
    except (OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


_AUDIO_MEDIA_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

@router.post("/media-select")
def select_import_media(request: Request) -> dict[str, Any]:
    if not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"} or request.headers.get('x-substar-media-select') != '1':
        raise HTTPException(status_code=403, detail="需要本机媒体选择操作")
    origin=request.headers.get('origin')
    if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(status_code=403, detail="不允许跨站媒体选择")
    from substar_core.media_relink import select_media
    try:
        result=select_media('__import__', None)
        if not result.get('cancelled'):
            path=Path(result['path'])
            result.update(name=path.name,size=path.stat().st_size)
        return result
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

@router.post("/projects/{project_id}/media-select")
def select_project_media(project_id: str, request: Request) -> dict[str, Any]:
    if not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(status_code=403, detail="只能在本机重新链接媒体")
    if request.headers.get("x-substar-media-select") != "1":
        raise HTTPException(status_code=403, detail="需要明确的媒体选择操作")
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="不允许跨站媒体选择")
    open_project_store(project_id)
    from substar_core.media_relink import select_media
    try:
        try:
            previous, _ = _project_media_source(project_id)
        except HTTPException:
            previous = None
        return select_media(project_id, previous)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
_VIDEO_MEDIA_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def _project_media_source(project_id: str) -> tuple[Path, dict[str, Any]]:
    job_dir = project_job_path(project_id)
    from substar_core.media_reference import resolve_reference
    try:
        referenced = resolve_reference(job_dir)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if referenced:
        return referenced, {}
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
    if manifest.get("media_relinked"):
        path = Path(explicit)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="文件不存在，请重新链接")
        return path, manifest
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
    with wave.open(str(audio), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        frame_count = pcm_wave_frame_count(audio, source)
        if width != 2:
            raise HTTPException(
                status_code=415,
                detail={"code": "waveform_format_unsupported", "message": "波形预览只支持 16-bit PCM"},
            )
        if cache.is_file() and cache.stat().st_mtime >= audio.stat().st_mtime:
            try:
                cached = json.loads(cache.read_text(encoding="utf-8"))
                if (
                    cached.get("schema_version") == "substar.waveform.v2"
                    and abs(float(cached.get("duration", -1)) - frame_count / rate) < 0.001
                ):
                    return cached
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
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
        "schema_version": "substar.waveform.v2",
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
        frame_count = pcm_wave_frame_count(audio, source)
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
    previous_cue = None
    previous_is_manual = False
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
                    "minimum_start": smart_snap_search_minimum(
                        previous_start=(previous_cue.start if previous_cue else None),
                        previous_end=(previous_cue.end if previous_cue else None),
                        current_start=cue.start,
                        previous_is_manual=previous_is_manual,
                    ),
                }
            )
        previous_cue = cue
        previous_is_manual = is_manual
    result = smart_forward_snap(
        audio,
        candidates,
        pre_roll_ms=payload.pre_roll_ms,
        sensitivity=payload.sensitivity,
    )
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


_SUBTITLE_EXPORT_LABELS = {
    SubtitleExportMode.SOURCE: "原文字幕",
    SubtitleExportMode.TARGET: "译文字幕",
    SubtitleExportMode.AB_SINGLE: "双语单行字幕",
    SubtitleExportMode.AB_DOUBLE: "双语双行字幕",
}


def _safe_export_name(value: Any) -> str:
    cleaned = "".join("_" if character in '\\/:*?\"<>|' or ord(character) < 32 else character for character in str(value or ""))
    return cleaned.rstrip(". ") or "未命名任务"


def _named_export(project_id: str, label: str, sequence: int, suffix: str) -> str:
    task_info = get_project_task_info(project_id)
    task_name = _safe_export_name(task_info.get("display_name") or project_id)
    return f"{task_name}_{label}_v{sequence:02d}.{suffix}"


@router.get("/projects/{project_id}/export/{mode}")
def export_project(
    project_id: str,
    mode: SubtitleExportMode,
    export_sequence: int = Query(default=1, ge=1, le=999999),
    revision_id: str | None = None,
) -> Response:
    store = open_project_store(project_id)
    revision = store.load_revision(revision_id) if revision_id else store.load_latest()
    if revision is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "empty_project", "message": "项目还没有文档版本"},
        )
    content = render_document_srt(revision.document, mode)
    filename = _named_export(project_id, _SUBTITLE_EXPORT_LABELS[mode], export_sequence, "srt")
    return Response(
        content=("\ufeff" + content).encode("utf-8"),
        media_type="application/x-subrip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-Substar-Artifact-Type": "subtitle",
        },
    )


@router.get("/projects/{project_id}/hotwords/export")
def export_project_hotwords(project_id: str) -> Response:
    glossary_id = _project_glossary_id(project_id)
    entries = _collect_generated_hotwords(project_id, glossary_id)
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


@router.post("/projects/{project_id}/restore-selection")
def restore_project_selection(project_id: str, payload: SelectiveRestoreRequest) -> dict[str, Any]:
    from substar_core.editor.application.selective_restore import restore_fields
    store = open_project_store(project_id)
    latest = store.load_latest()
    if latest is None or latest.revision_id != payload.expected_revision_id:
        raise HTTPException(409, detail="项目已更新，请刷新后重试")
    try:
        target = store.load_revision(payload.revision_id)
        document = restore_fields(latest.document, target.document, payload.cue_ids, payload.scope)
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    provenance = ChangeProvenance(kind=ChangeKind.MANUAL, operation="selective_restore", actor="editor",
        metadata={"restored_revision_id":target.revision_id, "scope":payload.scope, "cue_ids":payload.cue_ids})
    return _save_document(project_id, expected_revision_id=latest.revision_id, document=document,
                          operation="selective_restore", provenance=provenance)


@router.post("/projects/{project_id}/split-token")
def split_project_token(project_id: str, payload: SplitTokenRequest) -> dict[str, Any]:
    from substar_core.editor.application.selective_restore import split_token
    latest = open_project_store(project_id).load_latest()
    if latest is None or latest.revision_id != payload.expected_revision_id:
        raise HTTPException(409, detail="项目已更新，请刷新后重试")
    try:
        document = split_token(latest.document, payload.token_id, payload.offset)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    return _save_document(project_id, expected_revision_id=latest.revision_id, document=document,
                          operation="split_token", provenance=document.changes[-1])


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
    request: Request,
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
    token_by_id = {token.token_id: token for token in latest.document.display_tokens}
    active_tokens = [token_by_id[token_id] for cue in latest.document.cues if cue.state.value == "active"
                     for token_id in cue.display_token_ids if token_by_id[token_id].state.value == "active"]
    cue_by_token = {token_id: cue.cue_id for cue in latest.document.cues for token_id in cue.display_token_ids}
    units = [{"index": index, "text": token.text, "cue_id": cue_by_token[token.token_id]}
             for index, token in enumerate(active_tokens)]
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
        from substar_core.editor.application.reference import match_reference
        result = await match_reference(payload, file.filename or "reference.txt", units, source_language, request)
    except (ManuscriptMatchError, ValueError) as exc:
        raise HTTPException(status_code=422, detail={"code": "reference_match_failed", "message": str(exc)}) from exc
    from substar_core.segmentation.document_builder import apply_reference_report
    report = result["report"]
    if not any(report.get(key) for key in ("replacements", "merges", "insertions", "retained_source")) and not any(
        token.state.value == "deleted" and token.provenance.operation == "reference_manuscript_insert"
        for token in latest.document.display_tokens
    ):
        return {"revision": latest.to_dict(), "match": result, "applied": 0}
    document = apply_reference_report(
        latest.document, report, {index: token.token_id for index, token in enumerate(active_tokens)},
        operation_prefix=f"reference_{latest.revision_id}",
    )
    provenance = document.changes[-1]
    revision = _save_document(
        project_id, expected_revision_id=latest.revision_id, document=document,
        operation="reference_manuscript_apply", provenance=provenance,
    )
    return {"revision": revision, "match": result,
            "applied": len(report["replacements"]) + len(report["merges"])}





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






_CALIBRATION_PUNCTUATION = ".,?!;:，。？！；：、…-—–"







def _calibration_attach_mark(text: str, mark: str) -> str:
    base = str(text).rstrip(_CALIBRATION_PUNCTUATION)
    return base + mark






def _calibration_capitalize(text: str) -> str:
    chars = list(str(text))
    for index, char in enumerate(chars):
        if char.isalpha():
            chars[index] = char.upper()
            break
    return "".join(chars)




_CALIBRATION_ACTION_FIELDS = {
    "action_id", "kind", "token_ids", "before_text", "after_text",
    "confidence", "evidence", "disposition", "affects_translation",
}
_CALIBRATION_EVIDENCE_KINDS = {
    "glossary", "reference_document", "document_consistency", "context",
    "user_instruction",
}




@router.post("/projects/{project_id}/ai-calibrate", status_code=202)
def ai_calibrate_project(
    project_id: str, payload: AiCalibrationRequest, request: Request
) -> dict[str, Any]:
    """Apply model-authored punctuation, casing, terminology and ASR corrections."""
    latest = open_project_store(project_id).load_latest()
    if latest and any(cue.source_text is not None for cue in latest.document.cues):
        raise HTTPException(422, detail={"message": "无词元字幕项目不支持 AI 校准"})
    if latest is None:
        raise HTTPException(status_code=404, detail={
            "code": "empty_project", "message": "项目还没有文稿版本"
        })
    if payload.expected_revision_id != latest.revision_id:
        raise HTTPException(status_code=409, detail={
            "code": "revision_conflict", "message": "AI 校准基于旧版本，请刷新后重试"
        })
    service = request.app.state.task_service
    active = service.list_tasks(
        project_id=project_id,
        states=["queued", "running", "cancelling"],
        limit=20,
    )
    if any(task["task_type"] in {"translation", "calibration"} for task in active):
        raise HTTPException(status_code=423, detail={
            "code": "editor_ai_task_locked", "message": "当前项目已有 AI 任务正在运行"
        })
    try:
        settings = _project_llm_settings(project_id, include_secret=False)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={
            "code": "project_llm_unavailable", "message": str(exc)
        }) from exc
    try:
        settings.update(task_info_settings(load_task_info(project_job_path(project_id), project_id)))
    except (OSError, TypeError, ValueError):
        pass
    provider_id = str(settings.get("active_model_provider") or "").strip()
    frozen_settings = {
        key: value for key, value in settings.items()
        if not any(marker in key.lower() for marker in (
            "api_key", "secret", "password", "authorization", "access_token", "refresh_token", "bearer_token"
        )) and not key.startswith("_")
    }
    from dataclasses import asdict
    language = normalize_source_language(str(settings.get("language") or "Auto"))
    if language == "Auto":
        language = source_language_for_text(" ".join(t.text for t in latest.document.display_tokens if t.state.value == "active"))
    frozen_settings["language"] = language
    frozen_settings["prompt_snapshot"] = {
        key: asdict(render_prompt(key, variant=calibration_variant(language)))
        for key in ("calibration", "calibration_repair")
    }
    frozen_settings["glossary_snapshot"] = active_glossary(_project_glossary_id(project_id), stage="calibration")
    task_input = {
        "schema_version": CALIBRATION_INPUT_SCHEMA,
        "expected_revision_id": latest.revision_id,
        "instruction": payload.instruction,
        "provider_id": provider_id,
        "credential_ref": model_provider_credential_ref(provider_id),
        "settings": frozen_settings,
    }
    try:
        retried = _retry_exact_failed_editor_task(
            service,
            project_id=project_id,
            task_type="calibration",
            task_input=task_input,
        )
        if retried is not None:
            return retried
        task = service.create_task(
            "calibration",
            CALIBRATION_INPUT_SCHEMA,
            task_input,
            project_id=project_id,
            expected_revision_id=latest.revision_id,
            idempotency_key=_editor_ai_idempotency_key("calibration", task_input),
        )
        return _runtime_ai_task_projection(task, task_input)
    except Exception as exc:
        raise HTTPException(status_code=409, detail={
            "code": "calibration_start_rejected", "message": str(exc)
        }) from exc


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
        "glossary": active_glossary(_project_glossary_id(project_id)),
    }


@router.get("/projects/{project_id}/exchange/external-ai-prooftranslation")
def export_external_ai_prooftranslation(project_id: str, revision_id: str | None = None) -> FileResponse:
    store = open_project_store(project_id)
    revision = store.load_revision(revision_id) if revision_id else store.load_latest()
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
def export_external_ai_split(project_id: str, revision_id: str | None = None) -> FileResponse:
    store = open_project_store(project_id)
    revision = store.load_revision(revision_id) if revision_id else store.load_latest()
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
def export_external_ai_edit(project_id: str, revision_id: str | None = None) -> FileResponse:
    store = open_project_store(project_id)
    revision = store.load_revision(revision_id) if revision_id else store.load_latest()
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
def export_external_ai_generation(project_id: str, revision_id: str | None = None) -> FileResponse:
    store = open_project_store(project_id)
    revision = store.load_revision(revision_id) if revision_id else store.load_latest()
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


@router.get("/projects/{project_id}/exchange/external-calibration")
def export_external_calibration(project_id: str, revision_id: str, instruction: str = Query(default="", max_length=4000)):
    from substar_core.editor.calibration.exchange import export_calibration
    revision = open_project_store(project_id).load_revision(revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="项目版本不存在")
    info = get_project_task_info(project_id)
    try:
        text = export_calibration(project_id, revision, task_info_settings(info),
            active_glossary(_project_glossary_id(project_id), stage="calibration"), instruction)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"text": text, "revision_id": revision.revision_id}


@router.post("/projects/{project_id}/external-calibration")
async def import_external_calibration(project_id: str, request: Request, file: UploadFile = File(...), apply: bool = Form(default=False)):
    from substar_core.editor.calibration.exchange import inspect_calibration
    active = request.app.state.task_service.list_tasks(
        project_id=project_id, states=["queued", "running", "cancelling"], limit=20)
    if any(task["task_type"] in {"translation", "calibration"} for task in active):
        raise HTTPException(status_code=423, detail="当前项目已有 AI 任务正在运行，请等待完成后导入")
    revision = open_project_store(project_id).load_latest()
    if revision is None:
        raise HTTPException(status_code=404, detail="项目版本不存在")
    try:
        raw = await file.read(20 * 1024 * 1024 + 1)
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError("校准结果文件不能超过 20 MB")
        payload = json.loads(raw.decode("utf-8-sig"))
        document, provenance, result = inspect_calibration(project_id, revision, payload)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await file.close()
    if apply:
        result["revision"] = _save_document(project_id, expected_revision_id=revision.revision_id,
            document=document, operation="ai_calibration_apply", provenance=provenance)
    return result


@router.get("/projects/{project_id}/exchange/external-translation")
def export_external_translation(project_id: str, revision_id: str,
                                source_language: str = "Auto", target_language: str = "zh-CN",
                                mapping_mode: Literal["many_to_many", "one_to_one"] = "many_to_many"):
    from substar_core.project_exchange import external_translation_text
    revision = open_project_store(project_id).load_revision(revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="项目版本不存在")
    options = _exchange_prompt_options(project_id)
    info = get_project_task_info(project_id)
    limit_key = {"zh-CN":"chinese_hard_limit", "en":"english_hard_limit", "ja":"japanese_hard_limit", "ko":"korean_hard_limit"}.get(target_language)
    target_limit = int(info.get(limit_key) or options["target_hard_limit"])
    try:
        text = external_translation_text(revision, source_language=source_language,
            target_language=target_language, target_hard_limit=target_limit,
            mapping_mode=mapping_mode, glossary=options["glossary"])
    except (ProjectExchangeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"text": text, "revision_id": revision.revision_id}


@router.get("/projects/{project_id}/exchange/subtitle-project")
def export_subtitle_project_package(
    project_id: str,
    export_sequence: int = Query(default=1, ge=1, le=999999),
    revision_id: str | None = None,
) -> StreamingResponse:
    store = open_project_store(project_id)
    revision = store.load_revision(revision_id) if revision_id else store.load_latest()
    if revision is None:
        raise HTTPException(status_code=404, detail="项目还没有文档版本")
    get_project_task_info(project_id)
    filename = _named_export(project_id, "字幕工程", export_sequence, "zip")
    _project_media_source(project_id)  # Fail before starting a ZIP response.
    return StreamingResponse(
        stream_subtitle_project(
            project_id=project_id,
            job_dir=project_job_path(project_id),
            revision=revision,
        ),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-Substar-Artifact-Type": "subtitle-project",
        },
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


class SrtTranslationImportPayload(BaseModel):
    text: str = Field(max_length=5_000_000)
    format: Literal['auto', 'source', 'target', 'ab-single', 'ab-double'] = 'auto'
    expected_revision_id: str
    request_id: str = Field(min_length=1, max_length=100)
    apply: bool = False


@router.post('/projects/{project_id}/import-srt-translation')
def import_srt_translation(project_id: str, payload: SrtTranslationImportPayload):
    from substar_core.editor.application.srt_import import preview_srt, import_srt
    store = open_project_store(project_id)
    try:
        if payload.apply:
            revision = import_srt(store, text=payload.text, mode=payload.format,
                                  expected_revision_id=payload.expected_revision_id, request_id=payload.request_id)
            return {'revision': revision_payload(revision)}
        latest = store.load_latest()
        if latest is None or latest.revision_id != payload.expected_revision_id:
            raise ProjectConflictError('项目已变化，请重新预览')
        return preview_srt(latest.document, payload.text, payload.format)
    except ProjectConflictError as exc:
        raise HTTPException(status_code=409, detail={'message': str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={'message': str(exc)}) from exc








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
    project_id: str, payload: TranslationStartRequest, request: Request
) -> dict[str, Any]:
    job_dir = project_job_path(project_id)
    document_revision = open_project_store(project_id).load_latest()
    if document_revision and any(cue.source_text is not None for cue in document_revision.document.cues):
        raise HTTPException(422, detail={"message": "字幕导入模式不启用 AI 翻译，请直接编辑文本"})
    try:
        settings = _project_llm_settings(project_id, include_secret=False)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={
            "code": "project_llm_unavailable", "message": str(exc)
        }) from exc
    try:
        settings.update(task_info_settings(load_task_info(job_dir, project_id)))
    except (OSError, TypeError, ValueError):
        pass
    settings["target_language_mode"] = payload.target_language
    settings["translation_mapping_mode"] = payload.mapping_mode
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
    task_service = request.app.state.task_service
    active = task_service.list_tasks(
        project_id=project_id,
        states=["queued", "running", "cancelling"],
        limit=20,
    )
    if any(task["task_type"] in {"translation", "calibration"} for task in active):
        raise HTTPException(status_code=423, detail={
            "code": "editor_ai_task_locked", "message": "当前项目已有 AI 任务正在运行"
        })
    provider_id = str(settings.get("active_model_provider") or "").strip()
    frozen_settings = {
        key: value
        for key, value in settings.items()
        if not any(marker in key.lower() for marker in (
            "api_key", "secret", "password", "authorization", "access_token", "refresh_token", "bearer_token"
        ))
        and not key.startswith("_")
    }
    from dataclasses import asdict
    from substar_core.prompt_registry import translation_variant, opposite_language
    target = opposite_language(source_language) if payload.target_language == "auto_opposite" else payload.target_language
    variant = translation_variant(source_language, target)
    frozen_settings["prompt_snapshot"] = {
        key: asdict(render_prompt(key, variant=variant, mode=payload.mapping_mode))
        for key in ("contextual_translation", "contextual_translation_repair")
    }
    frozen_settings["glossary_snapshot"] = active_glossary(_project_glossary_id(project_id))
    task_input = {
        "schema_version": TRANSLATION_INPUT_SCHEMA,
        "expected_revision_id": latest.revision_id,
        "source_language_selection": payload.source_language,
        "source_language": settings["translation_source_language"],
        "target_language": payload.target_language,
        "mapping_mode": payload.mapping_mode,
        "provider_id": provider_id,
        "credential_ref": model_provider_credential_ref(provider_id),
        "settings": frozen_settings,
    }
    try:
        retried = _retry_exact_failed_editor_task(
            task_service,
            project_id=project_id,
            task_type="translation",
            task_input=task_input,
        )
        if retried is not None:
            return retried
        task = task_service.create_task(
            "translation",
            TRANSLATION_INPUT_SCHEMA,
            task_input,
            project_id=project_id,
            expected_revision_id=payload.expected_revision_id,
            idempotency_key=_editor_ai_idempotency_key("translation", task_input),
        )
        return _runtime_ai_task_projection(task, task_input)
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "translation_start_rejected", "message": str(exc)},
        ) from exc


@router.get("/projects/{project_id}/translation")
def get_project_translation(project_id: str, request: Request) -> dict[str, Any]:
    tasks = request.app.state.task_service.list_tasks(
        project_id=project_id, task_type="translation", limit=1
    )
    if not tasks:
        raise HTTPException(
            status_code=404,
            detail={"code": "translation_not_started", "message": "项目尚未启动翻译"},
        )
    task = tasks[0]
    task_input = request.app.state.task_service.get_task_input(task["task_id"])
    return _runtime_ai_task_projection(task, task_input)
