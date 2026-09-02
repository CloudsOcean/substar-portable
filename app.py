from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
import wave
from array import array
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from substar_core.artifacts import atomic_write_json, atomic_write_text
from substar_core.config import (
    APP_DATA_DIR,
    INSTALL_ROOT,
    PROJECT_ROOT,
    DATA_ROOT,
    PROJECTS_ROOT,
    load_credentials,
    load_settings,
    save_settings,
    infer_model_provider,
    apply_declared_model_capabilities,
)
from substar_core.model_providers import canonical_provider_id, provider_catalog
from substar_core.runtime_instance import (
    APP_MARKER,
    TASK_SCHEDULER_SHUTDOWN_TIMEOUT,
    acquire_backend_mutex,
    build_id,
    clear_runtime_record,
    process_start_time_ns,
    probe_identity,
    release_backend_mutex,
    startup_port,
    write_runtime_record,
)
from substar_core.runtime.launch_surface import require_visible_backend
from substar_core.edition import capabilities as edition_capabilities, current_edition, is_slim
from substar_core.api_testing import (
    ApiTestError,
    probe_chat_thinking_modes,
    test_chat,
)
from substar_core.qwen_cloud_asr import QwenCloudAsrError, test_connection as test_qwen_cloud_connection
from substar_core.glossary import (
    active_glossary,
    glossary_collection_exists,
    load_glossary,
    load_glossary_library,
    save_glossary_library,
)
from substar_core.glossary_xlsx import XLSX_MEDIA_TYPE, glossary_xlsx_bytes, parse_glossary_xlsx
from substar_core.storage import (
    ProjectStore,
)
from substar_core.export import SubtitleExportMode, render_document_srt
from substar_core.model_catalog import ModelCatalogError, discover_models
from substar_core.reasoning_capabilities import reasoning_capabilities
from substar_core.qwen_enhancement import (
    QWEN_PROMPT_MAX_CHARACTERS,
    normalize_qwen_hotwords,
    prioritize_generated_qwen_hotwords,
    qwen_hotword_mapping,
)
from substar_core.model_gateway import ModelGatewayError, call_translation_model
from substar_core.prompt_registry import (
    PromptRegistryError,
    prompt_catalog,
    prompt_component,
    update_prompt_component,
)
from substar_core.filenames import safe_filename
from substar_core.manuscript_matching import (
    ManuscriptMatchError,
    extract_reference_text,
    normalize_break_symbols,
)
from substar_core.domain import EntityState
from substar_core.language_layout import layout_tokens
from substar_core.relay_profile import (
    RelayProfileError,
    validate_relay_profile,
)
from substar_core.subtitle_exports import BilingualBlock, render_track
from substar_core.punctuation import normalize_punctuation_rules, project_punctuation
from substar_core.web_routes import router as web_router
from substar_core.task_info import load_task_info, save_task_info, task_info_settings
from substar_core.editor.http_api import router as editor_api_router
from substar_core.runtime import (
    RuntimeStore,
    TaskNotFoundError,
    TaskRegistry,
    TaskScheduler,
    TaskService,
)
from substar_core.runtime.api import router as task_runtime_router
from substar_core.transcription import (
    build_transcription_handler,
    build_transcription_request,
)
from substar_core.segmentation import build_segmentation_handler
from substar_core.editor.translation import build_translation_handler
from substar_core.editor.calibration import build_calibration_handler
from substar_core.creation import (
    create_subtitle_creation_graph,
    freeze_prompt_snapshot,
    reference_document_snapshot,
    subtitle_creation_projection,
)
from substar_core.recognition.registry import (
    DEFAULT_RECOGNITION_PROFILE,
    get_recognition_profile,
    list_recognition_profiles,
    profile_settings,
)
from substar_core.credential_store import (
    ASR_GENERIC,
    ASR_QWEN,
    MODEL_PROVIDER_PREFIX,
    model_provider_credential_ref,
    resolve_model_provider_credential,
)
from substar_core.model_providers import MODEL_PROVIDER_IDS


_WORKER_CREDENTIAL_ROLES = frozenset(
    {ASR_QWEN, ASR_GENERIC}
    | {model_provider_credential_ref(provider) for provider in MODEL_PROVIDER_IDS}
)


def _resolve_worker_credentials(references: tuple[str, ...]) -> dict[str, str]:
    """Resolve only the capabilities explicitly granted to one worker."""

    unknown = set(references) - _WORKER_CREDENTIAL_ROLES
    if unknown:
        raise RuntimeError(
            "unsupported worker credential reference: " + ", ".join(sorted(unknown))
        )
    protected = load_credentials()
    resolved: dict[str, str] = {}
    for reference in references:
        if reference.startswith(MODEL_PROVIDER_PREFIX):
            resolved[reference] = resolve_model_provider_credential(
                protected, reference[len(MODEL_PROVIDER_PREFIX):]
            )
        else:
            resolved[reference] = str(protected.get(reference, ""))
    return resolved


WEB_DIR = PROJECT_ROOT / "web"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
ALLOWED_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
}
SEGMENTATION_STRATEGIES = {"semantic"}
MAIN_SPLIT_BRANCH = "A"


@asynccontextmanager
async def _application_lifespan(_: FastAPI):
    _claim_backend_instance()
    try:
        yield
    finally:
        _release_backend_instance()


app = FastAPI(
    title="Substar Workbench",
    version="2.0.4",
    lifespan=_application_lifespan,
)
APP_STARTED_AT = datetime.now(timezone.utc).isoformat()
APP_INSTANCE_ID = os.environ.get("SUBSTAR_INSTANCE_ID", uuid.uuid4().hex)
APP_BUILD_ID = build_id(PROJECT_ROOT)
_BACKEND_MUTEX_HANDLE: int | None = None
_OWNS_RUNTIME_RECORD = False
_UVICORN_SERVER: Any | None = None
app.mount("/assets", StaticFiles(directory=str(WEB_DIR)), name="assets")
app.include_router(web_router)
app.include_router(editor_api_router)
app.include_router(task_runtime_router)


def _claim_backend_instance() -> None:
    """Make every Uvicorn/FastAPI entry point obey the backend singleton."""

    global _BACKEND_MUTEX_HANDLE, _OWNS_RUNTIME_RECORD
    launch_surface = require_visible_backend()
    port = startup_port()
    existing = probe_identity(port)
    if existing and int(existing.get("pid", 0) or 0) != os.getpid():
        raise RuntimeError(
            f"Substar 后端已在运行（PID {existing.get('pid', 'unknown')}，端口 {port}）。"
        )
    handle = acquire_backend_mutex()
    if handle is None:
        raise RuntimeError(f"Substar 后端单例锁已被占用（端口 {port}）。")
    _BACKEND_MUTEX_HANDLE = handle
    try:
        existing = probe_identity(port)
        if existing and int(existing.get("pid", 0) or 0) != os.getpid():
            raise RuntimeError(
                f"Substar 后端已在运行（PID {existing.get('pid', 'unknown')}，端口 {port}）。"
            )
        if not os.environ.get("SUBSTAR_INSTANCE_ID"):
            backend_started = process_start_time_ns(os.getpid())
            if os.name == "nt" and backend_started <= 0:
                raise RuntimeError("failed to capture backend process creation time")
            write_runtime_record(
                {
                    "app": APP_MARKER,
                    "build_id": APP_BUILD_ID,
                    "instance_id": APP_INSTANCE_ID,
                    "pid": os.getpid(),
                    "backend_start_time_ns": backend_started,
                    "port": port,
                    "install_root": str(INSTALL_ROOT),
                    "data_root": str(DATA_ROOT),
                    "started_at": APP_STARTED_AT,
                    "launch_surface": launch_surface,
                }
            )
            _OWNS_RUNTIME_RECORD = True
        task_store = RuntimeStore(APP_DATA_DIR / "runtime-v2.sqlite3")
        task_service = TaskService(task_store, APP_INSTANCE_ID)
        task_service.reconcile_startup()
        task_registry = TaskRegistry()
        task_registry.register(
            build_transcription_handler(PROJECTS_ROOT, PROJECT_ROOT)
        )
        task_registry.register(
            build_segmentation_handler(PROJECTS_ROOT, PROJECT_ROOT)
        )
        task_registry.register(
            build_translation_handler(PROJECTS_ROOT, PROJECT_ROOT)
        )
        task_registry.register(
            build_calibration_handler(PROJECTS_ROOT, PROJECT_ROOT)
        )
        runtime_settings = load_settings()
        task_scheduler = TaskScheduler(
            task_service,
            task_registry,
            APP_DATA_DIR / "task-runtime",
            credential_resolver=_resolve_worker_credentials,
            resource_limits={
                "worker": int(runtime_settings["runtime_worker_concurrency"]),
                "local_gpu": int(runtime_settings["runtime_gpu_concurrency"]),
                "media_cpu": int(runtime_settings["runtime_media_concurrency"]),
                "provider_io": int(runtime_settings["runtime_cloud_concurrency"]),
                "project_write": 1,
                "download_io": int(runtime_settings["runtime_download_concurrency"]),
            },
        )
        app.state.task_store = task_store
        app.state.task_service = task_service
        app.state.task_registry = task_registry
        app.state.task_scheduler = task_scheduler
        app.state.shutdown_requested = False
        task_scheduler.start()
    except Exception:
        try:
            scheduler = getattr(app.state, "task_scheduler", None)
            if scheduler is not None:
                scheduler.shutdown(
                    grace_seconds=0.5,
                    timeout_seconds=TASK_SCHEDULER_SHUTDOWN_TIMEOUT,
                )
        finally:
            if _OWNS_RUNTIME_RECORD:
                clear_runtime_record(APP_INSTANCE_ID)
                _OWNS_RUNTIME_RECORD = False
            release_backend_mutex(_BACKEND_MUTEX_HANDLE)
            _BACKEND_MUTEX_HANDLE = None
        raise


def _release_backend_instance() -> None:
    global _BACKEND_MUTEX_HANDLE, _OWNS_RUNTIME_RECORD
    try:
        scheduler = getattr(app.state, "task_scheduler", None)
        if scheduler is not None:
            scheduler.shutdown(
                grace_seconds=5.0,
                timeout_seconds=TASK_SCHEDULER_SHUTDOWN_TIMEOUT,
            )
    finally:
        if _OWNS_RUNTIME_RECORD:
            clear_runtime_record(APP_INSTANCE_ID)
            _OWNS_RUNTIME_RECORD = False
        release_backend_mutex(_BACKEND_MUTEX_HANDLE)
        _BACKEND_MUTEX_HANDLE = None


class SettingsPayload(BaseModel):
    workflow_mode: str = "subtitle_creation"
    segmentation_enabled: bool = True
    translation_enabled: bool = False
    calibration_enabled: bool = False
    project_name: str = "默认项目"
    recognition_profile_id: str = "qwen_cloud"
    transcript_source: str = "qwen_cloud"
    language: str = "Auto"
    context: str = ""
    qwen_cloud_region: str = "beijing"
    qwen_cloud_base_url: str = ""
    qwen_cloud_model: str = "qwen-audio-3.0-asr-flash-filetrans"
    qwen_cloud_request_timeout_seconds: int = 120
    qwen_cloud_task_timeout_seconds: int = 7200
    qwen_cloud_poll_interval_seconds: float = 3.0
    qwen_cloud_temporary_upload: bool = True
    output_dir: str
    api_key: str = ""
    clear_api_key: bool = False
    alignment_api_provider: str = "openai_chat"
    alignment_api_base_url: str = "https://api.deepseek.com"
    alignment_api_model: str = "deepseek-v4-flash"
    alignment_api_auth_mode: str = "bearer"
    alignment_api_timeout_seconds: int = 120
    alignment_api_key: str = ""
    clear_alignment_api_key: bool = False
    translation_api_provider: str = "openai_chat"
    translation_api_base_url: str = "https://api.deepseek.com"
    translation_api_model: str = "deepseek-v4-flash"
    active_model_provider: str = "deepseek"
    model_provider_profiles: dict[str, Any] = Field(default_factory=dict)
    model_reasoning_capabilities: dict[str, Any] = Field(default_factory=dict)
    stage_segmentation_model: str = ""
    stage_translation_model: str = ""
    stage_translation_repair_model: str = ""
    stage_calibration_model: str = ""
    stage_audit_repair_model: str = ""
    stage_segmentation_thinking_mode: str = "enabled"
    stage_segmentation_reasoning_effort: str = Field(default="low", pattern=r"^(low|medium|high|max|xhigh)$")
    stage_segmentation_max_tokens: int = 131072
    stage_segmentation_temperature: float = 0.0
    stage_segmentation_repair_model: str = ""
    stage_segmentation_repair_thinking_mode: str = "disabled"
    stage_segmentation_repair_reasoning_effort: str = Field(default="low", pattern=r"^(low|medium|high|max|xhigh)$")
    stage_segmentation_repair_max_tokens: int = 65536
    stage_segmentation_repair_temperature: float = 0.0
    stage_translation_thinking_mode: str = "enabled"
    stage_translation_reasoning_effort: str = Field(default="low", pattern=r"^(low|medium|high|max|xhigh)$")
    stage_translation_max_tokens: int = 131072
    stage_translation_temperature: float = 0.0
    stage_translation_repair_thinking_mode: str = "disabled"
    stage_translation_repair_reasoning_effort: str = Field(default="low", pattern=r"^(low|medium|high|max|xhigh)$")
    stage_translation_repair_max_tokens: int = 65536
    stage_translation_repair_temperature: float = 0.0
    stage_calibration_thinking_mode: str = "enabled"
    stage_calibration_reasoning_effort: str = Field(default="low", pattern=r"^(low|medium|high|max|xhigh)$")
    stage_calibration_max_tokens: int = 65536
    stage_calibration_temperature: float = 0.0
    stage_audit_repair_thinking_mode: str = "disabled"
    stage_audit_repair_reasoning_effort: str = Field(default="low", pattern=r"^(low|medium|high|max|xhigh)$")
    stage_audit_repair_max_tokens: int = 65536
    stage_audit_repair_temperature: float = 0.0
    startup_port: int = Field(default=8769, ge=1024, le=65535)
    translation_api_auth_mode: str = "bearer"
    translation_api_timeout_seconds: int = 300
    translation_thinking_mode: str = "enabled"
    translation_reasoning_effort: str = Field(
        default="low", pattern=r"^(low|medium|high|max|xhigh)$"
    )
    translation_style: str = "corporate_broadcast"
    target_language_mode: str = "auto_opposite"
    display_order: str = "en_zh"
    translation_api_key: str = ""
    clear_translation_api_key: bool = False
    top_raised_punctuation: str = "preserve"
    top_baseline_punctuation: str = "preserve"
    bottom_raised_punctuation: str = "preserve"
    bottom_baseline_punctuation: str = "normalize"
    english_hard_limit: int = 55
    english_count_spaces: bool = True
    english_count_punctuation: bool = True
    chinese_hard_limit: int = 25
    mixed_hard_limit: int = 25
    japanese_hard_limit: int = 25
    korean_hard_limit: int = 32
    target_visual_width_limit: int = 48
    minimum_cue_duration_ms: int = 400
    maximum_cue_duration_ms: int = 7000
    tail_padding_ms: int = 120
    snap_threshold_ms: int = 500
    maximum_cps_latin: float = 20.0
    maximum_cps_cjk: float = 12.0
    audio_denoise_mode: str = "off"
    text_cleanup_mode: str = "mark_conservative"
    segmentation_strategy: str = "semantic"
    split_workflow_mode: str = Field(default="one_step", pattern=r"^one_step$")
    translation_workflow_mode: str = Field(default="one_step", pattern=r"^one_step$")
    segmentation_chunk_seconds: int = 90
    segmentation_overlap_seconds: int = 40
    segmentation_batch_groups: int = 4
    segmentation_candidates: int = 1
    translation_workers: int = 64
    http_retry_attempts: int = 2
    stage_timeout_seconds: int = 3600
    shortcut_undo: str = "Ctrl+Z"
    shortcut_redo: str = "Ctrl+Y"
    shortcut_play_pause: str = "Space"
    shortcut_hide_cue: str = "Backspace"
    timeline_zoom_modifier: str = "Alt"


class GlossaryPayload(BaseModel):
    collections: list[dict[str, Any]] = Field(default_factory=list)
    entries: list[dict[str, Any]]


















class AutomaticTaskPayload(BaseModel):
    job_name: str
    settings_overrides: dict[str, Any] = Field(default_factory=dict)
    profile: dict[str, Any] | None = None


class JobRenamePayload(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)


class ModelDiscoveryPayload(BaseModel):
    base_url: str
    auth_mode: str = "bearer"
    provider_id: str = ""
    api_key: str = ""


class ReasoningCapabilitiesPayload(BaseModel):
    base_url: str
    model: str


class ReasoningProbePayload(ReasoningCapabilitiesPayload):
    auth_mode: str = "bearer"
    timeout_seconds: int = Field(default=60, ge=10, le=300)
    api_key: str = ""
    provider_id: str = ""


class PromptComponentUpdatePayload(BaseModel):
    text: str
    expected_sha256: str


class ApiConnectionTestPayload(BaseModel):
    role: str
    source: str = "api"
    provider: str = "openai_chat"
    provider_id: str = ""
    base_url: str = ""
    model: str = ""
    auth_mode: str = "bearer"
    timeout_seconds: int = 60
    api_key: str = ""
    thinking_mode: str = "disabled"
    reasoning_effort: str = Field(
        default="max", pattern=r"^(low|medium|high|max|xhigh)$"
    )


class QwenAssistPayload(BaseModel):
    source_language: str = Field(default="Auto", max_length=40)
    user_prompt: str = Field(min_length=1, max_length=4000)


PROJECT_CREATION_MODES = {"subtitle_creation"}


def _translation_export_state(job_dir: Path) -> dict[str, Any] | None:
    try:
        revision = ProjectStore.open(job_dir / "project").load_latest()
    except Exception:
        return None
    if revision is None:
        return None
    tokens = {token.token_id: token for token in revision.document.display_tokens}
    result = []
    for number, cue in enumerate(
        sorted(
            (item for item in revision.document.cues if item.state is EntityState.ACTIVE),
            key=lambda item: item.index,
        ),
        start=1,
    ):
        target = cue.target.target_text.strip() if cue.target is not None else ""
        if not target:
            return None
        source = layout_tokens(
            tokens[token_id].text
            for token_id in cue.display_token_ids
            if tokens[token_id].state is EntityState.ACTIVE
        )
        result.append({
            "number": number,
            "timing": f"{_srt_stamp(cue.start)} --> {_srt_stamp(cue.end)}",
            "source": source,
            "target": target,
        })
    if not result:
        return None
    return {
        "schema_version": "substar.translation-editor.v1",
        "source": "project",
        "revision_id": revision.revision_id,
        "cues": result,
    }


def _srt_stamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def _editor_punctuation_rules(job_dir: Path) -> dict[str, str]:
    path = job_dir / "manual_web_relay" / "slot_editor_draft.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        payload = value.get("payload", {})
        return normalize_punctuation_rules(payload.get("punctuation_rules", {}))
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return normalize_punctuation_rules({})


def _project_source_srt(text: str, rules: dict[str, str], line: str) -> str:
    pattern = re.compile(
        r"(?ms)^(\s*\d+\s*\n"
        r"\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2}:\d{2}:\d{2},\d{3}\s*\n)"
        r"(.*?)(?=\n{2,}|\Z)"
    )
    return pattern.sub(
        lambda match: match.group(1)
        + project_punctuation(match.group(2).strip(), rules, line),
        text.replace("\r\n", "\n"),
    )


def _workbench_export_availability(job: "Job") -> dict[str, dict[str, Any]]:
    eligible = (
        job.workflow_mode in PROJECT_CREATION_MODES
        and job.status in {"completed", "awaiting_edit"}
    )
    translated = eligible and _translation_export_state(job.job_dir) is not None
    base = f"/api/project-creations/{job.id}"
    unavailable = "切分任务尚未完成" if not eligible else "当前稿件尚无译文"
    result: dict[str, dict[str, Any]] = {
        "a": {
            "available": eligible,
            "url": f"{base}/subtitles/a",
            "reason": "" if eligible else unavailable,
        },
    }
    for mode in ("b", "ab_single", "ab_double"):
        result[mode] = {
            "available": bool(translated),
            "url": f"{base}/subtitles/{mode}",
            "reason": "" if translated else unavailable,
        }
    return result


def _job_stage_progress(job_dir: Path) -> dict[str, Any]:
    progress_path = job_dir / "stage_progress.json"
    if progress_path.is_file():
        try:
            value = json.loads(progress_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        except (OSError, json.JSONDecodeError):
            pass

    return {"schema_version": "substar.stage-progress.v1", "stages": {}}


@dataclass
class Job:
    id: str
    filename: str
    job_dir: Path
    input_path: Path
    display_name: str = ""
    workflow_mode: str = ""
    auxiliary_path: Path | None = None
    source_job_name: str = ""
    settings_overrides: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"
    message: str = "等待处理"
    progress: float = 0.0
    error: str = ""
    files: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    attempt: int = 1
    cancel_requested: bool = False
    transcription_task_id: str = ""
    segmentation_task_id: str = ""
    submission_key: str = ""
    submission_fingerprint: str = ""

    def public(self) -> dict[str, Any]:
        stage_progress = _job_stage_progress(self.job_dir)
        if self.workflow_mode == "subtitle_creation" and self.transcription_task_id:
            # Canonical tasks expose business progress through task events;
            # never project retired internal stage identifiers into new jobs.
            stage_progress = {
                "schema_version": "substar.stage-progress.v1",
                "stages": {},
            }
        public_setting_names = {
            "recognition_profile_id",
            "recognition_profile_label",
            "language",
            "translation_workers",
            "reference_script_mode",
            "reference_break_symbols",
        }
        public_settings = {
            key: self.settings_overrides[key]
            for key in public_setting_names
            if key in self.settings_overrides
        }
        public_message = _live_stage_message(
            self.workflow_mode,
            self.status,
            self.message,
            stage_progress,
            str(self.settings_overrides.get("segmentation_strategy", "")),
        )
        public_message = (
            public_message.replace("semantic grouping", "字幕切分")
            .replace("contextual translation", "字幕翻译")
            .replace("P1 直接选择安全接缝", "素材分组：选择安全接缝")
        )
        display_name = self.display_name or self.filename
        if (self.job_dir / "task_info.json").is_file():
            try:
                display_name = str(load_task_info(self.job_dir, self.id)["display_name"])
            except (OSError, TypeError, ValueError):
                pass
        tutorial_case_id = ""
        try:
            creation = json.loads(
                (self.job_dir / "project_creation.json").read_text(encoding="utf-8")
            )
            tutorial_case_id = str(creation.get("tutorial_case_id", ""))
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        if tutorial_case_id == "reference-script-v1":
            display_name = "初级教程"
        return {
            "id": self.id,
            "filename": self.filename,
            "display_name": display_name,
            "workflow_mode": self.workflow_mode,
            "source_job_name": self.source_job_name,
            "settings_overrides": public_settings,
            "status": self.status,
            "message": public_message,
            "progress": round(self.progress, 4),
            "error": self.error,
            "files": [
                {**item, "url": f'/api/project-creations/{self.id}/files/{item["name"]}'}
                for item in self.files
            ],
            "stage_progress": stage_progress,
            "created_at": self.created_at,
            "attempt": self.attempt,
            "transcription_task_id": self.transcription_task_id,
            "segmentation_task_id": self.segmentation_task_id,
            "runtime_log_url": f"/api/project-creations/{self.id}/logs",
            "export_availability": _workbench_export_availability(self),
            "tutorial_case_id": tutorial_case_id,
        }


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
SUBMISSION_LOCK = threading.Lock()
BATCH_SUBMISSION_LOCK = threading.Lock()
# This lock serializes the isolated local-ingest worker, not an entire job:
# cloud API, validation and translation stages remain free to overlap after
# the worker releases its model and audio memory.


def _has_durable_job_identity(job_dir: Path) -> bool:
    """Reject orphan state files left after a project directory was removed."""

    return (job_dir / "project_creation.json").is_file() or (
        job_dir / "tutorial_project.json"
    ).is_file()


def _prune_missing_jobs() -> None:
    """Drop in-memory task records whose durable directory is gone."""

    root = _relay_output_root().resolve()
    stale: list[str] = []
    with JOBS_LOCK:
        for job_id, job in JOBS.items():
            try:
                job_dir = job.job_dir.resolve()
            except OSError:
                stale.append(job_id)
                continue
            if (
                root not in job_dir.parents
                or not job_dir.is_dir()
                or not (job_dir / "creation_state.json").is_file()
                or not _has_durable_job_identity(job_dir)
            ):
                stale.append(job_id)
        for job_id in stale:
            JOBS.pop(job_id, None)


_RUNTIME_SECRET_FIELDS = ("api_key", "apikey", "token", "secret", "password", "authorization")


def _runtime_log_path(job: Job) -> Path:
    return job.job_dir / "runtime.log"


def _runtime_safe_text(value: str) -> str:
    """Replace lone UTF-16 surrogates before writing user-visible UTF-8."""

    return "".join(
        "\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char for char in value
    )


def _runtime_safe_value(value: Any, key: str = "") -> Any:
    """Return a JSON-safe, secret-free value suitable for the user-visible log."""
    lowered = key.lower()
    if any(marker in lowered for marker in _RUNTIME_SECRET_FIELDS):
        text = _runtime_safe_text(str(value or ""))
        return f"{text[:2]}****{text[-2:]}" if len(text) >= 6 else "[REDACTED]"
    if isinstance(value, dict):
        return {
            _runtime_safe_text(str(child_key)): _runtime_safe_value(
                child_value, str(child_key)
            )
            for child_key, child_value in value.items()
            if not str(child_key).startswith("_")
        }
    if isinstance(value, (list, tuple)):
        return [_runtime_safe_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        # A lone surrogate is invalid in a UTF-8 log file.  It can be produced
        # by a malformed subprocess/model response, so replace it at the log
        # boundary instead of masking the original task error with another
        # UnicodeEncodeError.
        return _runtime_safe_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return f"[{type(value).__name__}]"


def _append_runtime_log(job: Job, event: str, payload: Any | None = None) -> None:
    """Append one SmartSub-style timestamped event to the durable task log."""
    job.job_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H:%M:%S")
    lines = [f"{stamp} {_runtime_safe_text(str(event).strip())}"]
    if payload is not None:
        safe_payload = _runtime_safe_value(payload)
        if isinstance(safe_payload, str):
            rendered = safe_payload
        else:
            rendered = json.dumps(safe_payload, ensure_ascii=False, indent=2)
        if rendered:
            lines.extend(("", rendered))
    with _runtime_log_path(job).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def _live_stage_message(
    workflow_mode: str,
    status: str,
    fallback: str,
    progress: dict[str, Any],
    pipeline_mode: str = "",
) -> str:
    if status != "running" or workflow_mode != "subtitle_creation":
        return fallback
    stages = progress.get("stages", {})
    one_step = pipeline_mode == "semantic"
    labels = {
        "P1": "素材分组：选择安全接缝",
        "semantic grouping": "字幕切分：生成结构与最终切点",
    }
    if one_step:
        labels.update(
            {
                "P1": "分组：选择安全接缝",
                "semantic grouping": "字幕切分：结构与切点",
            }
        )
    stage_order = ("semantic grouping", "P1") if one_step else ()
    for name in stage_order:
        row = stages.get(name, {})
        if row.get("status") in {"running", "repairing"}:
            return labels[name]
    p1 = stages.get("P1", {})
    if p1.get("status") == "completed" and int(p1.get("planned", 0)) == 0:
        return "短片无需分组，准备字幕切分" if one_step else fallback
    return fallback


def _persist_job(job: Job) -> None:
    job.job_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        job.job_dir / "creation_state.json",
        {
            **job.public(),
            # The browser does not need these values, but a response-lost
            # retry must survive API restarts without creating a second
            # project or provider submission.
            "submission_key": job.submission_key,
            "submission_fingerprint": job.submission_fingerprint,
            # Cancellation is durable user intent.  A process restart must
            # never reinterpret it as a retryable interruption or completed
            # editor handoff.
            "cancel_requested": job.cancel_requested,
        },
    )


def _editor_ready(job_dir: Path) -> bool:
    """Prove the editor can open the project, media, and waveform authority."""

    try:
        latest = ProjectStore.open(job_dir / "project").load_latest()
        if latest is None:
            return False
        audio = job_dir / "audio_16k_mono.wav"
        if not audio.is_file() or audio.stat().st_size <= 44:
            return False
        input_dir = job_dir / "input"
        return any(
            path.is_file() and path.suffix.casefold() in ALLOWED_EXTENSIONS
            for path in input_dir.iterdir()
        )
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _register_completed_beginner_tutorial(job: Job) -> None:
    """Bind an accepted beginner walkthrough to the canonical editor contract."""

    creation_path = job.job_dir / "project_creation.json"
    tutorial_path = job.job_dir / "tutorial_project.json"
    try:
        creation = json.loads(creation_path.read_text(encoding="utf-8"))
        if creation.get("tutorial_case_id") != "reference-script-v1":
            return
        if tutorial_path.is_file():
            current = json.loads(tutorial_path.read_text(encoding="utf-8"))
            if current.get("case_id") == "reference-script-v1":
                return
            raise RuntimeError("初级教程项目绑定与现有教程清单冲突")
        latest = ProjectStore.open(job.job_dir / "project").load_latest()
        if latest is None:
            raise RuntimeError("初级教程没有可注册的编辑器版本")
        atomic_write_json(tutorial_path, {
            "schema_version": "substar.tutorial-project.v2",
            "case_id": "reference-script-v1",
            "level": "beginner",
            "display_name": "初级教程",
            "baseline_revision_id": latest.revision_id,
            "baseline_document_hash": latest.document_hash,
            "available_stages": [],
            "simulated": False,
        })
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"初级教程注册失败：{exc}") from exc


def _refresh_canonical_job_projection(job: Job) -> None:
    if (
        job.workflow_mode != "subtitle_creation"
        or not job.transcription_task_id
        or not job.segmentation_task_id
    ):
        return
    try:
        service = _task_service()
        transcription = service.get_task(job.transcription_task_id)
        segmentation = service.get_task(job.segmentation_task_id)
        if segmentation["state"] in {"cancelled", "failed", "interrupted"}:
            replacements = service.list_tasks(
                project_id=job.id,
                task_type="segmentation",
                parent_task_id=job.transcription_task_id,
                limit=20,
            )
            replacement = next(
                (
                    candidate
                    for candidate in replacements
                    if candidate["task_id"] != segmentation["task_id"]
                    and candidate["state"] != "cancelled"
                    and candidate["created_at"] > segmentation["created_at"]
                ),
                None,
            )
            if replacement is not None:
                segmentation = replacement
                job.segmentation_task_id = str(replacement["task_id"])
                _persist_job(job)
    except (RuntimeError, TaskNotFoundError):
        return
    projection = subtitle_creation_projection(
        transcription=transcription,
        segmentation=segmentation,
        editor_ready=_editor_ready(job.job_dir),
        cancel_requested=job.cancel_requested,
    )
    with JOBS_LOCK:
        changed = any(
            getattr(job, field) != projection[field]
            for field in ("status", "progress", "message", "error")
        )
        job.status = str(projection["status"])
        job.progress = float(projection["progress"])
        job.message = str(projection["message"])
        job.error = str(projection["error"])
        if job.status == "awaiting_edit":
            job.files = [
                {"name": path.name, "size": path.stat().st_size}
                for path in sorted(job.job_dir.iterdir())
                if path.is_file()
            ]
        if changed:
            _persist_job(job)
    if job.status == "awaiting_edit":
        _register_completed_beginner_tutorial(job)


def _submission_digest(path: Path | None) -> str:
    if path is None:
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _workbench_submission_fingerprint(
    *,
    mode: str,
    media_path: Path,
    srt_path: Path | None,
    reference_path: Path | None,
    settings: dict[str, Any],
) -> str:
    value = {
        "mode": mode,
        "media_sha256": _submission_digest(media_path),
        "srt_sha256": _submission_digest(srt_path),
        "reference_sha256": _submission_digest(reference_path),
        "settings": settings,
    }
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _normalized_idempotency_key(value: Any) -> str:
    key = str(value or "").strip()
    if key and (
        len(key) > 200 or re.fullmatch(r"[A-Za-z0-9._:-]+", key) is None
    ):
        raise HTTPException(status_code=400, detail="Idempotency-Key 格式无效")
    return key


def _batch_item_idempotency_key(batch_key: str, index: int) -> str:
    if not batch_key:
        return ""
    digest = hashlib.sha256(f"{batch_key}:{index}".encode("utf-8")).hexdigest()
    return f"batch-item:{digest}"


async def _upload_digest(upload: UploadFile) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    await upload.seek(0)
    return digest.hexdigest()


async def _close_uploads(uploads: list[UploadFile]) -> None:
    for upload in uploads:
        await upload.close()


async def _workbench_batch_submission_fingerprint(
    *,
    mode: str,
    media: list[UploadFile],
    references: list[UploadFile],
    settings: Mapping[str, Any],
) -> str:
    async def rows(uploads: list[UploadFile]) -> list[dict[str, str]]:
        return [
            {
                "name": Path(upload.filename or "").name,
                "sha256": await _upload_digest(upload),
            }
            for upload in uploads
        ]

    value = {
        "mode": mode,
        "media": await rows(media),
        "references": await rows(references),
        "settings": dict(settings),
    }
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _task_service() -> TaskService:
    service = getattr(app.state, "task_service", None)
    if not isinstance(service, TaskService):
        raise RuntimeError("任务运行时尚未初始化")
    return service


def _project_reference_path(job: Job) -> Path | None:
    snapshot_path = job.job_dir / "project_creation.json"
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    filename = Path(str(snapshot.get("reference_document", ""))).name
    if not filename:
        return None
    candidate = job.job_dir / "input" / filename
    return candidate if candidate.is_file() else None


def _workbench_transcription_request(
    job: Job, settings: dict[str, Any]
) -> dict[str, Any]:
    try:
        hotwords = qwen_hotword_mapping(settings.get("qwen_temporary_hotwords", []))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return build_transcription_request(
        media_path=job.input_path,
        project_directory=job.job_dir,
        profile_id=str(
            settings.get("recognition_profile_id", DEFAULT_RECOGNITION_PROFILE)
        ),
        language=str(settings.get("language", "Auto")),
        prompt=str(settings.get("context", "")),
        hotwords=hotwords,
        settings=settings,
    )


def _create_workbench_subtitle_tasks(
    job: Job, settings: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    service = _task_service()
    transcription_request = _workbench_transcription_request(job, settings)
    prompt_snapshot = freeze_prompt_snapshot(job.job_dir, PROJECT_ROOT)
    reference_snapshot = reference_document_snapshot(
        job.job_dir, _project_reference_path(job)
    )
    glossary_snapshot = active_glossary(str(settings.get("glossary_id") or ""))
    transcription, segmentation = create_subtitle_creation_graph(
        service=service,
        project_id=job.id,
        transcription_request=transcription_request,
        segmentation_enabled=bool(settings.get("segmentation_enabled", True)),
        language=str(settings.get("language", "Auto")),
        reference_document=reference_snapshot,
        prompt_snapshot=prompt_snapshot,
        glossary_snapshot=glossary_snapshot,
        settings=settings,
    )
    with JOBS_LOCK:
        job.transcription_task_id = str(transcription["task_id"])
        job.segmentation_task_id = str(segmentation["task_id"])
        _persist_job(job)
    return transcription, segmentation


def _create_workbench_transcription_task(
    job: Job, settings: dict[str, Any]
) -> dict[str, Any]:
    """Compatibility helper returning the first node of the durable graph."""

    transcription, _segmentation = _create_workbench_subtitle_tasks(job, settings)
    return transcription


def _relay_output_root() -> Path:
    root = Path(load_settings(include_secret=False)["output_dir"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root




def _automatic_settings_from_payload(
    payload: AutomaticTaskPayload,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        profile = validate_relay_profile(payload.profile)
    except RelayProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    raw = dict(payload.settings_overrides)
    saved = load_settings()
    stage_names = (
        "segmentation", "segmentation_repair",
        "translation", "translation_repair",
        "calibration", "audit_repair",
    )
    stage_keys = {
        f"stage_{stage}_{suffix}"
        for stage in stage_names
        for suffix in (
            "model",
            "thinking_mode",
            "reasoning_effort",
            "max_tokens",
            "temperature",
        )
    }
    allowed = {
        "active_model_provider",
        "translation_api_base_url",
        "translation_api_model",
        "translation_api_auth_mode",
        "stage_segmentation_model",
        "stage_translation_model",
        "stage_translation_repair_model",
        "stage_calibration_model",
        "stage_audit_repair_model",
        "translation_thinking_mode",
        "translation_reasoning_effort",
        "segmentation_strategy",
        "split_branch",
        "split_workflow_mode",
        "translation_workflow_mode",
        "segmentation_enabled",
        "reference_script_mode",
        "reference_break_symbols",
        "translation_enabled",
        "calibration_enabled",
        "target_language_mode",
        "segmentation_chunk_seconds",
        "translation_workers",
        "http_retry_attempts",
        "english_hard_limit",
        "chinese_hard_limit",
        "mixed_hard_limit",
        "japanese_hard_limit",
        "korean_hard_limit",
        "recognition_profile_id",
        "language",
        "qwen_cloud_region",
        "qwen_cloud_base_url",
        "qwen_cloud_model",
        "qwen_cloud_request_timeout_seconds",
        "qwen_cloud_task_timeout_seconds",
        "qwen_cloud_poll_interval_seconds",
        "qwen_cloud_temporary_upload",
        "context",
        "qwen_temporary_hotwords",
        "glossary_id",
    } | stage_keys
    # Settings snapshots can outlive the UI/backend build that created them.
    # Consume only the explicit task contract and ignore unrelated/newer
    # global settings instead of making project creation fail on version skew.
    raw = {key: value for key, value in raw.items() if key in allowed}

    base_url = str(raw.get("translation_api_base_url") or saved["translation_api_base_url"])
    model = str(raw.get("translation_api_model") or saved["translation_api_model"])
    provider_id = canonical_provider_id(
        raw.get("active_model_provider")
        or saved.get("active_model_provider")
        or infer_model_provider(base_url)
    )
    auth_mode = str(
        raw.get("translation_api_auth_mode")
        or saved.get("translation_api_auth_mode")
        or "bearer"
    ).strip().lower()
    if not base_url or len(base_url) > 500:
        raise HTTPException(status_code=400, detail="模型 Base URL 无效")
    if not model or len(model) > 200:
        raise HTTPException(status_code=400, detail="模型 ID 无效")
    if auth_mode not in {"bearer", "api-key"}:
        raise HTTPException(status_code=400, detail="模型认证方式无效")
    def bounded_int(key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(raw.get(key, default))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"{key} 必须是整数") from exc
        if not minimum <= value <= maximum:
            raise HTTPException(
                status_code=400,
                detail=f"{key} 必须在 {minimum}–{maximum} 之间",
            )
        return value

    stage_config: dict[str, Any] = {}
    endpoint_provider = provider_id
    for stage in stage_names:
        model_key = f"stage_{stage}_model"
        value = str(raw.get(model_key) or saved.get(model_key) or model)
        normalized_value = value.strip().lower()
        # Old portable snapshots can contain the former provider's default in
        # every Stage. Never send an unmistakable DeepSeek model to the GLM
        # endpoint (or vice versa); inherit the frozen connection model.
        if (
            endpoint_provider == "glm" and normalized_value.startswith("deepseek")
        ) or (
            endpoint_provider == "deepseek" and normalized_value.startswith("glm")
        ):
            value = model
        if len(value) > 200:
            raise HTTPException(status_code=400, detail=f"{model_key} 模型 ID 无效")
        stage_config[model_key] = value
        thinking_key = f"stage_{stage}_thinking_mode"
        effort_key = f"stage_{stage}_reasoning_effort"
        tokens_key = f"stage_{stage}_max_tokens"
        temperature_key = f"stage_{stage}_temperature"
        thinking = str(raw.get(thinking_key, saved[thinking_key]))
        effort = str(raw.get(effort_key, saved[effort_key]))
        if thinking not in {"enabled", "disabled"}:
            raise HTTPException(status_code=400, detail=f"{thinking_key} 无效")
        if effort not in {"low", "medium", "high", "max", "xhigh"}:
            raise HTTPException(status_code=400, detail=f"{effort_key} 无效")
        stage_config[thinking_key] = thinking
        stage_config[effort_key] = effort
        stage_config[tokens_key] = bounded_int(
            tokens_key, int(saved[tokens_key]), 256, 393216
        )
        try:
            temperature = float(raw.get(temperature_key, saved[temperature_key]))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"{temperature_key} 必须是数值") from exc
        if not 0 <= temperature <= 2:
            raise HTTPException(status_code=400, detail=f"{temperature_key} 必须在 0–2 之间")
        stage_config[temperature_key] = temperature
    split_workflow_mode = "one_step"
    requested_split_branch = str(raw.get("split_branch") or MAIN_SPLIT_BRANCH).strip().upper()
    if requested_split_branch != MAIN_SPLIT_BRANCH:
        raise HTTPException(status_code=400, detail="主入口不支持自定义字幕切分分支")
    pipeline_mode = "semantic"
    if pipeline_mode not in SEGMENTATION_STRATEGIES:
        raise HTTPException(status_code=400, detail="切分流水线无效")
    english_hard_limit = bounded_int(
        "english_hard_limit", int(saved["english_hard_limit"]), 1, 200
    )
    chinese_hard_limit = bounded_int(
        "chinese_hard_limit", int(saved["chinese_hard_limit"]), 1, 100
    )
    mixed_hard_limit = bounded_int(
        "mixed_hard_limit", int(saved.get("mixed_hard_limit", 25)), 1, 200
    )
    japanese_hard_limit = bounded_int(
        "japanese_hard_limit", int(saved["japanese_hard_limit"]), 1, 100
    )
    korean_hard_limit = bounded_int(
        "korean_hard_limit", int(saved["korean_hard_limit"]), 1, 120
    )
    reference_script_mode = bool(raw.get("reference_script_mode", False))
    glossary_id = str(raw.get("glossary_id") or "").strip()
    if not glossary_collection_exists(glossary_id):
        raise HTTPException(status_code=400, detail="所选项目词库不存在，请刷新后重试")
    try:
        reference_break_symbols = normalize_break_symbols(
            str(raw.get("reference_break_symbols") or "，。？")
        )
    except ManuscriptMatchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        temporary_hotwords = normalize_qwen_hotwords(
            raw.get("qwen_temporary_hotwords", [])
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    overrides = {
        "workflow_mode": "subtitle_creation",
        "active_model_provider": provider_id,
        "translation_api_base_url": base_url.rstrip("/"),
        "translation_api_model": model,
        "translation_api_auth_mode": auth_mode,
        "glossary_id": glossary_id,
        **{
            key: raw.get(key, saved.get(key))
            for key in (
                "recognition_profile_id",
                "language",
                "target_language_mode",
                "segmentation_enabled",
                "translation_enabled",
                "calibration_enabled",
            )
        },
        **stage_config,
        "segmentation_strategy": pipeline_mode,
        "split_branch": MAIN_SPLIT_BRANCH,
        "split_workflow_mode": split_workflow_mode,
        "translation_workflow_mode": "one_step",
        "segmentation_chunk_seconds": bounded_int(
            "segmentation_chunk_seconds", 90, 75, 100
        ),
        "translation_workers": bounded_int(
            "translation_workers", 64, 1, 256
        ),
        "http_retry_attempts": bounded_int(
            "http_retry_attempts", 2, 1, 3
        ),
        "translation_style": profile["translation_style"],
        "display_order": (
            "source_target"
            if profile["top_line_role"] == "source"
            else "target_source"
        ),
        "top_raised_punctuation": profile["top_raised_punctuation"],
        "top_baseline_punctuation": profile["top_baseline_punctuation"],
        "bottom_raised_punctuation": profile["bottom_raised_punctuation"],
        "bottom_baseline_punctuation": profile[
            "bottom_baseline_punctuation"
        ],
        "english_hard_limit": english_hard_limit,
        "english_count_spaces": profile["english_count_spaces"],
        "english_count_punctuation": profile[
            "english_count_punctuation"
        ],
        "chinese_hard_limit": chinese_hard_limit,
        "mixed_hard_limit": mixed_hard_limit,
        "japanese_hard_limit": japanese_hard_limit,
        "korean_hard_limit": korean_hard_limit,
        "reference_script_mode": reference_script_mode,
        "reference_break_symbols": reference_break_symbols,
        "minimum_cue_duration_ms": profile["minimum_cue_duration_ms"],
        "maximum_cue_duration_ms": profile["maximum_cue_duration_ms"],
        "maximum_cps_latin": profile["maximum_cps_latin"],
        "maximum_cps_cjk": profile["maximum_cps_cjk"],
        "audio_denoise_mode": profile["audio_denoise_mode"],
        "text_cleanup_mode": profile["text_cleanup_mode"],
        "context": str(raw.get("context", ""))[:QWEN_PROMPT_MAX_CHARACTERS],
        "qwen_temporary_hotwords": temporary_hotwords,
    }
    apply_declared_model_capabilities(overrides)
    return overrides, profile






















@app.get("/api/project-creations/{job_id}/logs")
def get_workbench_split_job_logs(job_id: str) -> PlainTextResponse:
    _restore_persisted_jobs(job_id)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None or job.workflow_mode != "subtitle_creation":
            raise HTTPException(status_code=404, detail="切分任务不存在")
        log_path = _runtime_log_path(job)
    if not log_path.is_file():
        return PlainTextResponse("暂无日志\n")
    # Keep the polling endpoint bounded even for long batch jobs. The durable
    # file itself remains complete; the UI follows the most recent 2 MiB.
    maximum = 2 * 1024 * 1024
    with log_path.open("rb") as handle:
        size = log_path.stat().st_size
        if size > maximum:
            handle.seek(-maximum, os.SEEK_END)
        payload = handle.read().decode("utf-8", errors="replace")
    if size > maximum:
        payload = "[日志较长，界面显示末尾 2 MiB]\n" + payload.split("\n", 1)[-1]
    return PlainTextResponse(payload, headers={"Cache-Control": "no-store"})


@app.delete("/api/project-creations/{job_id}/logs")
def clear_workbench_split_job_logs(job_id: str) -> dict[str, Any]:
    _restore_persisted_jobs(job_id)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None or job.workflow_mode != "subtitle_creation":
            raise HTTPException(status_code=404, detail="切分任务不存在")
        atomic_write_text(_runtime_log_path(job), "")
    return {"cleared": job_id}


@app.delete("/api/project-creations/{job_id}")
def delete_workbench_split_job(job_id: str) -> dict[str, Any]:
    _restore_persisted_jobs(job_id)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="切分任务不存在")
        if job.workflow_mode != "subtitle_creation":
            raise HTTPException(status_code=400, detail="该项目不是切分任务")
        if job.status in {"queued", "running"}:
            job.cancel_requested = True
            job.message = "正在安全取消任务"
            job.error = ""
            _persist_job(job)
            for task_id in (job.segmentation_task_id, job.transcription_task_id):
                if not task_id:
                    continue
                try:
                    task = _task_service().get_task(task_id)
                    if task["state"] in {"queued", "running"}:
                        _task_service().request_cancel(task_id)
                except (RuntimeError, TaskNotFoundError):
                    pass
            return {
                "cancel_requested": job_id,
                "pending": True,
                "message": "任务已请求取消；确认工作进程退出后会保留项目文件",
            }
        if job.status not in {"completed", "awaiting_edit", "failed", "interrupted", "cancelled"}:
            raise HTTPException(status_code=409, detail="当前任务状态不能删除")
        job_dir = job.job_dir.resolve()
        JOBS.pop(job_id, None)
    root = _relay_output_root().resolve()
    if root not in job_dir.parents or not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="切分任务目录不存在")
    trash_root = root / ".trash"
    trash_root.mkdir(parents=True, exist_ok=True)
    destination = trash_root / f"{job_id}_{int(time.time())}"
    shutil.move(str(job_dir), str(destination))
    return {"deleted": job_id, "recoverable_to": str(destination)}


@app.patch("/api/project-creations/{job_id}/name")
def rename_workbench_split_job(
    job_id: str, payload: JobRenamePayload
) -> dict[str, Any]:
    """Rename only the user-facing task label; immutable ids and files stay put."""
    _restore_persisted_jobs(job_id)
    name = payload.display_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="任务名称不能为空")
    if any(ord(char) < 32 for char in name) or re.search(r'[\\/:*?"<>|]', name):
        raise HTTPException(status_code=400, detail="任务名称包含不允许的字符")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None or job.workflow_mode != "subtitle_creation":
            raise HTTPException(status_code=404, detail="切分任务不存在")
        try:
            current = load_task_info(job.job_dir, job.id)
            save_task_info(job.job_dir, job.id, {**current, "display_name": name})
        except (OSError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        job.display_name = name
        return job.public()
















def _translation_top_line_role(job_dir: Path) -> str:
    try:
        revision = ProjectStore.open(job_dir / "project").load_latest()
        if revision is None:
            return "source"
        display_order = revision.document.presentation.display_order.value
    except (AttributeError, OSError, TypeError, ValueError):
        return "source"
    return "target" if display_order == "target_above_source" else "source"
























@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return load_settings(include_secret=False)


@app.get("/api/prompts")
def get_prompt_catalog() -> dict[str, Any]:
    try:
        return prompt_catalog()
    except PromptRegistryError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/prompts/content")
def get_prompt_component(path: str) -> dict[str, Any]:
    try:
        return prompt_component(path)
    except PromptRegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/prompts/content")
def put_prompt_component(
    path: str,
    payload: PromptComponentUpdatePayload,
) -> dict[str, Any]:
    try:
        return update_prompt_component(
            path,
            payload.text,
            expected_sha256=payload.expected_sha256,
        )
    except PromptRegistryError as exc:
        detail = str(exc)
        status = 409 if "其他位置修改" in detail else 422
        raise HTTPException(status_code=status, detail=detail) from exc


@app.get("/api/runtime/identity")
def runtime_identity() -> dict[str, Any]:
    launch_surface = require_visible_backend()
    return {
        "app": "substar-workbench",
        "api_version": 1,
        "build_id": APP_BUILD_ID,
        "instance_id": APP_INSTANCE_ID,
        "pid": os.getpid(),
        "host": "127.0.0.1",
        "port": startup_port(),
        "install_root": str(INSTALL_ROOT),
        "data_root": str(DATA_ROOT),
        "started_at": APP_STARTED_AT,
        "edition": current_edition(),
        "launch_surface": launch_surface,
        "user_visible": True,
    }


@app.get("/api/recognition/profiles")
def recognition_profiles() -> dict[str, Any]:
    caps = edition_capabilities()
    return {
        "default": "qwen_cloud" if is_slim() else DEFAULT_RECOGNITION_PROFILE,
        "profiles": list_recognition_profiles(),
        "edition": caps["edition"],
        "capabilities": caps,
    }


@app.post("/api/settings")
def put_settings(payload: SettingsPayload) -> dict[str, Any]:
    return save_settings(payload.model_dump())


@app.get("/api/glossary")
def get_glossary() -> dict[str, Any]:
    return load_glossary_library()


@app.put("/api/glossary")
def put_glossary(payload: GlossaryPayload) -> dict[str, Any]:
    try:
        return save_glossary_library(payload.collections, payload.entries)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/settings/test")
def test_api_connection(payload: ApiConnectionTestPayload) -> dict[str, Any]:
    if payload.role not in {"sentence", "alignment", "translation"}:
        raise HTTPException(status_code=400, detail="未知模型角色")
    saved = load_settings(include_secret=True)
    saved_key_name = {
        "sentence": "api_key",
        "alignment": "alignment_api_key",
        "translation": "translation_api_key",
    }[payload.role]
    api_key = payload.api_key.strip()
    if not api_key and payload.role in {"alignment", "translation"}:
        provider_id = canonical_provider_id(
            payload.provider_id.strip() or infer_model_provider(payload.base_url)
        )
        api_key = str(
            resolve_model_provider_credential(load_credentials(), provider_id)
        ).strip()
    if not api_key and not payload.provider_id.strip():
        api_key = str(saved.get(saved_key_name, "")).strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="请先输入 API Key，或保存已有密钥")
    if not payload.base_url.strip() or not payload.model.strip():
        raise HTTPException(status_code=400, detail="Base URL 和模型 ID 不能为空")
    try:
        if payload.provider == "qwen_cloud":
            return test_qwen_cloud_connection(
                {
                    "qwen_cloud_base_url": payload.base_url,
                    "qwen_cloud_model": payload.model,
                    "qwen_cloud_request_timeout_seconds": payload.timeout_seconds,
                },
                api_key,
            )
        return test_chat(
            base_url=payload.base_url,
            model=payload.model,
            api_key=api_key,
            auth_mode=payload.auth_mode,
            timeout=payload.timeout_seconds,
            thinking_mode=payload.thinking_mode,
            reasoning_effort=payload.reasoning_effort,
        )
    except (ApiTestError, QwenCloudAsrError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/qwen-assist")
def fill_qwen_transcription_fields(payload: QwenAssistPayload) -> dict[str, Any]:
    settings = load_settings(include_secret=True)
    provider_id = canonical_provider_id(
        settings.get("active_model_provider")
        or infer_model_provider(settings.get("translation_api_base_url"))
    )
    api_key = str(
        resolve_model_provider_credential(load_credentials(), provider_id)
        or settings.get("translation_api_key", "")
    ).strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="请先配置并测试当前 AI 服务")
    language_names = {
        "zh": "中文", "en": "英文", "ja": "日文", "ko": "韩文",
        "mixed": "中英混合", "Auto": "用户说明所使用的语言",
    }
    source_language = language_names.get(
        payload.source_language, payload.source_language or "用户说明所使用的语言"
    )
    qwen_model = str(settings.get("qwen_cloud_model", ""))
    supports_hotwords = not qwen_model.startswith("qwen3-asr-flash-filetrans")
    base_url = str(settings["translation_api_base_url"])
    # Qwen field generation follows the active text-model connection rather
    # than an imported or frozen segmentation Stage override.
    assist_model = str(settings["translation_api_model"])
    capability = reasoning_capabilities(base_url, assist_model)
    cache_key = "\n".join((base_url.strip().lower(), assist_model.strip().lower()))
    cached_capability = settings.get("model_reasoning_capabilities", {}).get(
        cache_key, {}
    )
    if isinstance(cached_capability, dict) and isinstance(
        cached_capability.get("supported_thinking_modes"), list
    ):
        supported_modes = cached_capability["supported_thinking_modes"]
    else:
        supported_modes = capability.get("supported_thinking_modes", [])
    normalized_modes = {
        str(mode) for mode in supported_modes if str(mode) in {"disabled", "enabled"}
    }
    if normalized_modes == {"enabled"}:
        assist_thinking_modes = ("enabled",)
    elif normalized_modes == {"disabled"}:
        assist_thinking_modes = ("disabled",)
    else:
        # Unknown or dual-mode models try the cheaper non-thinking request first.
        assist_thinking_modes = ("disabled", "enabled")
    system_prompt = f"""你负责为 Qwen 文件听写准备两个可编辑输入框。
字幕切分语言配置为：{source_language}。
根据用户说明输出且只输出 JSON 对象：
{{"prompt":"...","hotwords":[{{"text":"...","weight":50}}]}}
规则：
1. prompt 使用字幕切分语言组织，最多 400 个字符，用于描述节目领域、人物和主题。
2. hotwords 可以混合任意语言；人名、品牌、产品、缩写必须保留原始拼写，绝不翻译。
3. 用户直接点名的人名、品牌、产品和术语必须全部列入 hotwords，权重使用 50；根据节目内容推导出的相关专名权重使用 5，不得编造。
4. 每个热词应是真实词语；中文等非 ASCII 热词不超过 15 字，纯拉丁热词不超过 7 个单词。
5. {"当前 Qwen 模型不支持即时热词，hotwords 必须输出空数组。" if not supports_hotwords else "当前 Qwen 模型支持即时热词。"}
6. 不要输出解释、Markdown 或额外字段。"""
    try:
        result: dict[str, Any] | None = None
        last_stage_error: ModelGatewayError | None = None
        for thinking_mode in assist_thinking_modes:
            try:
                result, _metadata = call_translation_model(
                    base_url=base_url,
                    api_key=api_key,
                    auth_mode=str(settings.get("translation_api_auth_mode", "bearer")),
                    model=assist_model,
                    system_prompt=system_prompt,
                    groups=[{"user_prompt": payload.user_prompt.strip()}],
                    timeout=min(300, int(settings.get("translation_api_timeout_seconds", 300))),
                    thinking_mode=thinking_mode,
                    reasoning_effort="low",
                    request_attempts=max(1, int(settings.get("http_retry_attempts", 2)) + 1),
                    max_tokens=4096,
                    temperature=0.0,
                )
                break
            except ModelGatewayError as exc:
                last_stage_error = exc
        if result is None:
            assert last_stage_error is not None
            raise last_stage_error
        prompt = str(result.get("prompt", "")).strip()[:QWEN_PROMPT_MAX_CHARACTERS]
        if not prompt:
            raise ValueError("AI 没有生成 Qwen Prompt")
        hotwords = prioritize_generated_qwen_hotwords(
            result.get("hotwords", []),
            user_prompt=payload.user_prompt,
        )
        if not supports_hotwords:
            hotwords = []
    except (ModelGatewayError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"prompt": prompt, "hotwords": hotwords}


@app.get("/api/models/providers")
def list_model_providers() -> dict[str, Any]:
    return {"providers": provider_catalog()}


@app.post("/api/models/discover")
def discover_provider_models(payload: ModelDiscoveryPayload) -> dict[str, Any]:
    provider_id = canonical_provider_id(
        payload.provider_id.strip() or infer_model_provider(payload.base_url)
    )
    api_key = payload.api_key.strip() or str(
        resolve_model_provider_credential(load_credentials(), provider_id)
    ).strip()
    try:
        return discover_models(
            base_url=payload.base_url,
            api_key=api_key,
            auth_mode=payload.auth_mode,
        )
    except ModelCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/glossary/export-xlsx")
def export_glossary_xlsx() -> Response:
    content = glossary_xlsx_bytes(load_glossary())
    return Response(
        content=content,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="substar_glossary.xlsx"'},
    )


@app.post("/api/glossary/import-xlsx")
async def import_glossary_xlsx(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="热词表文件不能超过 10 MB")
    try:
        entries = parse_glossary_xlsx(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"entries": entries, "format": "substar-glossary-xlsx-v1"}


@app.post("/api/models/reasoning-capabilities")
def get_reasoning_capabilities(payload: ReasoningCapabilitiesPayload) -> dict[str, Any]:
    if not payload.base_url.strip() or not payload.model.strip():
        raise HTTPException(status_code=400, detail="Base URL 和模型 ID 不能为空")
    capability = reasoning_capabilities(payload.base_url, payload.model)
    cache_key = "\n".join((payload.base_url.strip().lower(), payload.model.strip().lower()))
    cached = load_settings().get("model_reasoning_capabilities", {}).get(cache_key, {})
    if isinstance(cached, dict) and isinstance(cached.get("supported_thinking_modes"), list):
        capability = {
            **capability,
            "supported_thinking_modes": list(
                cached.get("supported_thinking_modes", capability.get("supported_thinking_modes", []))
            ),
            "verified": True,
            "source": "persisted-live-probe",
        }
    return capability


@app.post("/api/models/reasoning-probe")
def probe_reasoning_capabilities(payload: ReasoningProbePayload) -> dict[str, Any]:
    saved = load_settings(include_secret=True)
    provider_id = canonical_provider_id(
        payload.provider_id.strip() or infer_model_provider(payload.base_url)
    )
    api_key = payload.api_key.strip() or str(
        resolve_model_provider_credential(load_credentials(), provider_id)
    ).strip()
    if not api_key and provider_id == canonical_provider_id(
        saved.get("active_model_provider")
        or infer_model_provider(saved.get("translation_api_base_url"))
    ):
        api_key = str(saved.get("translation_api_key", "")).strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="请先输入 API Key，或保存已有密钥")
    if not payload.base_url.strip() or not payload.model.strip():
        raise HTTPException(status_code=400, detail="Base URL 和模型 ID 不能为空")
    capability = reasoning_capabilities(payload.base_url, payload.model)
    try:
        probe = probe_chat_thinking_modes(
            base_url=payload.base_url,
            model=payload.model,
            api_key=api_key,
            auth_mode=payload.auth_mode,
            timeout=payload.timeout_seconds,
            reasoning_effort="high",
        )
    except ApiTestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    accepted_thinking_modes = probe.get("accepted_thinking_modes") or []
    if accepted_thinking_modes:
        capability = {
            **capability,
            "verified": True,
            "source": "live-probe",
            "supported_thinking_modes": accepted_thinking_modes,
            "note": "连通性测试已验证接口接受的思考模式；五档推理强度由服务商映射。",
        }
        cache_key = "\n".join((payload.base_url.strip().lower(), payload.model.strip().lower()))
        saved = load_settings()
        cache = dict(saved.get("model_reasoning_capabilities", {}))
        cache[cache_key] = {
            "base_url": payload.base_url.strip(),
            "model": payload.model.strip(),
            "supported_thinking_modes": list(accepted_thinking_modes),
            "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        save_settings({"model_reasoning_capabilities": cache})
    return {**capability, "probe": probe}


@app.get("/api/system")
def system_status() -> dict[str, Any]:
    return {
        "ffmpeg_installed": bool(shutil.which("ffmpeg")),
        "recognition_provider": "qwen_cloud",
        "segmentation_provider": "deepseek",
    }


@app.post("/api/project-creations")
async def create_workbench_split_job(
    mode: str = Form(...),
    media: UploadFile = File(...),
    reference_document: UploadFile | None = File(default=None),
    settings_json: str = Form(default="{}"),
    tutorial_case_id: str = Form(default=""),
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
) -> JSONResponse:
    """Create one durable job for ingest, API cutting, and editor handoff."""
    if mode != "asr":
        raise HTTPException(status_code=400, detail="新版本只接受 ASR 媒体输入")
    submission_key = _normalized_idempotency_key(idempotency_key)
    media_name = safe_filename(media.filename or "media.mp4")
    media_suffix = Path(media_name).suffix.lower()
    if media_suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型：{media_suffix}")

    try:
        raw_overrides = json.loads(settings_json or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="任务设置不是有效 JSON") from exc
    if not isinstance(raw_overrides, dict):
        raise HTTPException(status_code=400, detail="任务设置必须是 JSON 对象")
    tutorial_case_id = tutorial_case_id.strip() if isinstance(tutorial_case_id, str) else ""
    if tutorial_case_id not in {"", "reference-script-v1"}:
        raise HTTPException(status_code=400, detail="未知的教程案例")
    if tutorial_case_id:
        raw_overrides["tutorial_case_id"] = tutorial_case_id
    if is_slim():
        raw_overrides.update(
            {
                "recognition_profile_id": "qwen_cloud",
                "split_workflow_mode": "one_step",
                "translation_workflow_mode": "one_step",
            }
        )
    if (
        raw_overrides.get("translation_enabled")
        and not raw_overrides.get("segmentation_enabled", True)
        and not raw_overrides.get("reference_script_mode", False)
    ):
        raise HTTPException(status_code=422, detail="翻译必须先启用切分并选择切分方案")
    overrides, profile = _automatic_settings_from_payload(
        AutomaticTaskPayload(
            job_name="workbench",
            settings_overrides=raw_overrides,
        )
    )
    requested_profile_id = str(
        overrides.get("recognition_profile_id", DEFAULT_RECOGNITION_PROFILE)
    )
    if bool(overrides.get("reference_script_mode")) and reference_document is None:
        raise HTTPException(status_code=422, detail="参考稿模式必须上传参考文稿")
    try:
        recognition_profile = get_recognition_profile(requested_profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if recognition_profile.requires_srt:
        raise HTTPException(status_code=400, detail="当前识别方案不接受媒体听写输入")
    overrides = profile_settings(
        {**overrides, "recognition_profile_id": recognition_profile.id}
    )
    # Resolve the configured root before comparing it with the resolved job
    # directory.  On Windows the temporary directory may be returned through
    # an 8.3/case-normalized alias, which otherwise makes the containment check
    # reject a directory that is actually inside the configured root.
    output_root = _relay_output_root().resolve()
    job_id = time.strftime("%Y%m%d_%H%M%S") + "_split_" + uuid.uuid4().hex[:6]
    job_dir = (output_root / job_id).resolve()
    if output_root not in job_dir.parents:
        raise HTTPException(status_code=400, detail="输出目录无效")
    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=False)
    media_path = input_dir / media_name
    srt_path: Path | None = None
    reference_path: Path | None = None
    reference_name = ""

    async def save_upload(
        source: UploadFile,
        target: Path,
        maximum_bytes: int,
    ) -> None:
        size = 0
        with target.open("wb") as handle:
            while chunk := await source.read(1024 * 1024):
                size += len(chunk)
                if size > maximum_bytes:
                    raise HTTPException(
                        status_code=413, detail=f"{target.name} 文件过大"
                    )
                handle.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail=f"{target.name} 为空")

    try:
        await save_upload(media, media_path, 20 * 1024 * 1024 * 1024)
        if reference_document is not None:
            reference_name = safe_filename(
                reference_document.filename or "reference.txt"
            )
            if Path(reference_name).suffix.lower() not in {".txt", ".docx", ".srt"}:
                raise HTTPException(
                    status_code=400,
                    detail="参考文稿只支持 TXT、DOCX 或 SRT",
                )
            reference_path = input_dir / reference_name
            await save_upload(reference_document, reference_path, 50 * 1024 * 1024)
            # Parse now so a bad document fails before a durable task is queued.
            extract_reference_text(reference_path.read_bytes(), reference_name)
        atomic_write_json(
            job_dir / "project_creation.json",
            {
                "schema_version": "substar.project-creation.v2",
                "input_mode": mode,
                "source_file": media_name,
                "reference_document": reference_name,
                "settings_overrides": overrides,
                "profile": profile,
                "recognition_profile": recognition_profile.public(),
                "created_at": time.time(),
                "tutorial_case_id": tutorial_case_id,
            },
        )
        source_language = str(overrides.get("language") or "Auto")
        target_language = str(overrides.get("target_language_mode") or "zh-CN")
        source_limit_key = {
            "en": "english_hard_limit", "zh": "chinese_hard_limit",
            "zh-CN": "chinese_hard_limit", "ja": "japanese_hard_limit",
            "ko": "korean_hard_limit", "mixed": "mixed_hard_limit",
        }.get(source_language, "mixed_hard_limit")
        target_limit_key = {
            "en": "english_hard_limit", "zh-CN": "chinese_hard_limit",
            "ja": "japanese_hard_limit", "ko": "korean_hard_limit",
        }.get(target_language, "chinese_hard_limit")
        save_task_info(job_dir, job_id, {
            "display_name": (
                "初级教程" if tutorial_case_id == "reference-script-v1"
                else f"{Path(media_name).stem} · {recognition_profile.short_label}"
            ),
            "language": source_language,
            "target_language_mode": target_language,
            "glossary_id": str(overrides.get("glossary_id") or ""),
            "source_hard_limit": int(overrides.get(source_limit_key, 25)),
            "target_hard_limit": int(overrides.get(target_limit_key, 25)),
        })
    except ManuscriptMatchError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        await media.close()
        if reference_document is not None:
            await reference_document.close()

    workflow_mode = "subtitle_creation"
    submission_fingerprint = _workbench_submission_fingerprint(
        mode=mode,
        media_path=media_path,
        srt_path=srt_path,
        reference_path=reference_path,
        settings=raw_overrides,
    )
    job = Job(
        id=job_id,
        filename=media_name,
        job_dir=job_dir,
        input_path=media_path,
        workflow_mode=workflow_mode,
        auxiliary_path=srt_path,
        display_name=(
            "初级教程"
            if tutorial_case_id == "reference-script-v1"
            else f"{Path(media_name).stem} · {recognition_profile.short_label}"
        ),
        settings_overrides=overrides,
        message="等待生成字幕草稿",
        submission_key=submission_key,
        submission_fingerprint=submission_fingerprint,
    )
    _append_runtime_log(job, "handleTask start")
    _append_runtime_log(
        job,
        "formData:",
        {
            "inputMode": mode,
            "sourceFile": media_name,
            "referenceDocument": reference_name,
            "settings": overrides,
        },
    )
    _append_runtime_log(job, f"queued {media_name} for subtitle draft generation")
    with SUBMISSION_LOCK:
        if submission_key:
            _restore_persisted_jobs()
            with JOBS_LOCK:
                existing = next(
                    (
                        item
                        for item in JOBS.values()
                        if item.submission_key == submission_key
                    ),
                    None,
                )
            if existing is not None:
                shutil.rmtree(job_dir, ignore_errors=True)
                if existing.submission_fingerprint != submission_fingerprint:
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency-Key 已用于不同的创建请求",
                    )
                return JSONResponse(existing.public(), status_code=202)
        with JOBS_LOCK:
            JOBS[job_id] = job
            _persist_job(job)
        try:
            non_secret_settings = {
                **load_settings(include_secret=False),
                **job.settings_overrides,
            }
            _create_workbench_subtitle_tasks(
                job, profile_settings(non_secret_settings)
            )
        except Exception:
            with JOBS_LOCK:
                JOBS.pop(job_id, None)
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
    return JSONResponse(job.public(), status_code=202)


def _split_batch_root() -> Path:
    root = _relay_output_root() / ".project_batches"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _split_batch_path(batch_id: str) -> Path:
    if Path(batch_id).name != batch_id or not re.fullmatch(
        r"[0-9]{8}_[0-9]{6}_batch_[0-9a-f]{6}", batch_id
    ):
        raise HTTPException(status_code=400, detail="批次编号无效")
    return _split_batch_root() / f"{batch_id}.json"


def _find_split_batch_submission(submission_key: str) -> dict[str, Any] | None:
    if not submission_key:
        return None
    matches: list[dict[str, Any]] = []
    for path in _split_batch_root().glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("submission_key") == submission_key:
            matches.append(value)
    if len(matches) > 1:
        raise HTTPException(
            status_code=500,
            detail="批量提交幂等记录不唯一，请检查本地运行时数据",
        )
    return matches[0] if matches else None


def _load_split_batch(batch_id: str) -> dict[str, Any]:
    path = _split_batch_path(batch_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="切分批次不存在")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="切分批次记录损坏") from exc
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise HTTPException(status_code=500, detail="切分批次记录无效")
    return value


def _public_split_batch(value: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    completed = failed = running = queued = 0
    progress_total = 0.0
    for frozen in value.get("items", []):
        item = {
            "id": str(frozen.get("id", "")),
            "media_name": Path(str(frozen.get("media_name", ""))).name,
            "job_id": str(frozen.get("job_id", "")),
            "status": str(frozen.get("status", "failed")),
            "progress": float(frozen.get("progress", 0.0)),
            "message": str(frozen.get("message", "")),
            "error": str(frozen.get("error", "")),
        }
        job_id = item["job_id"]
        if job_id:
            _restore_persisted_jobs(job_id)
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                current = job.public() if job else None
            if current:
                item.update(
                    status=current["status"],
                    progress=float(current.get("progress", 0.0)),
                    message=str(current.get("message", "")),
                    error=str(current.get("error", "")),
                )
        if item["status"] in {"completed", "awaiting_edit"}:
            completed += 1
            item["progress"] = 1.0
            item["editor_url"] = f"/editor?project={job_id}"
        elif item["status"] in {"failed", "interrupted", "cancelled"}:
            failed += 1
            item["progress"] = 1.0
        elif item["status"] == "queued":
            queued += 1
        else:
            running += 1
        progress_total += float(item["progress"])
        items.append(item)
    total = len(items)
    if total and completed + failed == total:
        status = (
            "failed"
            if failed == total
            else ("completed_with_failures" if failed else "completed")
        )
    elif running:
        status = "running"
    else:
        status = "queued"
    return {
        "schema_version": "substar.project-batch.v1",
        "id": str(value.get("id", "")),
        "mode": str(value.get("mode", "")),
        "status": status,
        "created_at": float(value.get("created_at", 0.0)),
        "total": total,
        "completed": completed,
        "failed": failed,
        "running": running,
        "queued": queued,
        "progress": round(progress_total / total, 4) if total else 0.0,
        "items": items,
    }


@app.post("/api/project-batches")
async def create_workbench_split_batch(
    mode: str = Form(...),
    media: list[UploadFile] = File(...),
    reference_documents: list[UploadFile] | None = File(default=None),
    settings_json: str = Form(default="{}"),
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
) -> JSONResponse:
    batch_submission_key = _normalized_idempotency_key(idempotency_key)
    if mode != "asr":
        raise HTTPException(status_code=400, detail="新版本只接受 ASR 媒体输入")
    if not media:
        raise HTTPException(status_code=400, detail="批处理至少需要一个媒体文件")
    reference_files = list(reference_documents or [])
    all_uploads = [*media, *reference_files]
    try:
        parsed_settings = json.loads(settings_json)
    except json.JSONDecodeError as exc:
        await _close_uploads(all_uploads)
        raise HTTPException(status_code=400, detail="设置 JSON 无效") from exc
    if not isinstance(parsed_settings, dict):
        await _close_uploads(all_uploads)
        raise HTTPException(status_code=400, detail="设置必须是 JSON 对象")
    batch_fingerprint = await _workbench_batch_submission_fingerprint(
        mode=mode,
        media=media,
        references=reference_files,
        settings=parsed_settings,
    )

    replay: dict[str, Any] | None = None
    conflict = ""
    with BATCH_SUBMISSION_LOCK:
        existing = _find_split_batch_submission(batch_submission_key)
        if existing is not None:
            if existing.get("submission_fingerprint") != batch_fingerprint:
                conflict = "Idempotency-Key 已用于不同的批量提交"
            elif existing.get("creation_state") == "complete":
                replay = existing
            elif existing.get("owner_instance_id") == APP_INSTANCE_ID:
                conflict = "相同批量提交仍在创建中，请稍后重试"
            batch_id = str(existing["id"])
            created_at = float(existing.get("created_at", time.time()))
        else:
            batch_id = (
                time.strftime("%Y%m%d_%H%M%S")
                + "_batch_"
                + uuid.uuid4().hex[:6]
            )
            created_at = time.time()
        frozen: dict[str, Any] = {
            "schema_version": "substar.project-batch.v1",
            "id": batch_id,
            "mode": mode,
            "created_at": created_at,
            "items": [],
            "submission_key": batch_submission_key,
            "submission_fingerprint": batch_fingerprint,
            "creation_state": "creating",
            "owner_instance_id": APP_INSTANCE_ID,
        }
        if replay is None and not conflict:
            atomic_write_json(_split_batch_path(batch_id), frozen)
    if replay is not None:
        await _close_uploads(all_uploads)
        return JSONResponse(_public_split_batch(replay), status_code=202)
    if conflict:
        await _close_uploads(all_uploads)
        raise HTTPException(status_code=409, detail=conflict)
    references_by_stem: dict[str, list[UploadFile]] = {}
    for reference in reference_files:
        key = Path(reference.filename or "").stem.casefold()
        references_by_stem.setdefault(key, []).append(reference)
    used_references: set[int] = set()

    for index, media_upload in enumerate(media, start=1):
        media_name = safe_filename(media_upload.filename or f"media-{index}")
        reference_upload: UploadFile | None = None
        pair_error = ""
        reference_matches = references_by_stem.get(
            Path(media_name).stem.casefold(), []
        )
        if len(reference_matches) > 1:
            pair_error = pair_error or "存在多个同名参考文稿，无法确定配对"
        elif reference_matches:
            reference_upload = reference_matches[0]
            if id(reference_upload) in used_references:
                pair_error = pair_error or "同名参考文稿已被另一媒体占用"
            else:
                used_references.add(id(reference_upload))
        base_item = {
            "id": f"item_{index:04d}",
            "media_name": media_name,
            "job_id": "",
            "status": "failed" if pair_error else "queued",
            "progress": 0.0,
            "message": "配对失败" if pair_error else "等待处理",
            "error": pair_error,
        }
        if pair_error:
            await media_upload.close()
            if reference_upload is not None:
                await reference_upload.close()
            frozen["items"].append(base_item)
            continue
        try:
            response = await create_workbench_split_job(
                mode=mode,
                media=media_upload,
                reference_document=reference_upload,
                settings_json=settings_json,
                tutorial_case_id="",
                idempotency_key=_batch_item_idempotency_key(
                    batch_submission_key, index
                ),
            )
            job_payload = json.loads(response.body.decode("utf-8"))
            base_item.update(
                job_id=str(job_payload["id"]),
                status=str(job_payload["status"]),
                progress=float(job_payload.get("progress", 0.0)),
                message=str(job_payload.get("message", "等待处理")),
            )
        except HTTPException as exc:
            await media_upload.close()
            if reference_upload is not None:
                await reference_upload.close()
            base_item.update(
                status="failed",
                message="任务创建失败",
                error=str(exc.detail),
            )
        except Exception as exc:
            await media_upload.close()
            if reference_upload is not None:
                await reference_upload.close()
            base_item.update(
                status="failed",
                message="任务创建失败",
                error=str(exc),
            )
        frozen["items"].append(base_item)

    for reference in reference_files:
        if id(reference) not in used_references:
            await reference.close()
    frozen["creation_state"] = "complete"
    frozen["owner_instance_id"] = ""
    atomic_write_json(_split_batch_path(batch_id), frozen)
    return JSONResponse(_public_split_batch(frozen), status_code=202)


@app.get("/api/project-batches/{batch_id}")
def get_workbench_split_batch(batch_id: str) -> dict[str, Any]:
    return _public_split_batch(_load_split_batch(batch_id))


def _workbench_retry_inputs(job: Job) -> tuple[Path, Path | None]:
    settings_path = job.job_dir / "project_creation.json"
    try:
        frozen = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=409, detail="任务缺少可重试的设置快照") from exc
    source_name = Path(str(frozen.get("source_file", job.filename))).name
    input_dir = job.job_dir / "input"
    media_path = input_dir / source_name
    if not media_path.is_file():
        raise HTTPException(status_code=409, detail="服务端已保存的源媒体不存在")
    overrides = frozen.get("settings_overrides")
    if not isinstance(overrides, dict):
        raise HTTPException(status_code=409, detail="任务设置快照损坏")
    job.settings_overrides = {
        **overrides,
        "workflow_mode": "subtitle_creation",
        "segmentation_strategy": "semantic",
        "split_branch": MAIN_SPLIT_BRANCH,
    }
    return media_path, None


@app.get("/api/runtime/health")
def runtime_health() -> dict[str, Any]:
    """Return live readiness without trusting the discovery record on disk."""

    return {
        "status": "ready",
        "instance_id": APP_INSTANCE_ID,
        "build_id": APP_BUILD_ID,
        "launch_surface": require_visible_backend(),
        "user_visible": True,
        "task_runtime": bool(getattr(app.state, "task_service", None)),
        "task_scheduler": (
            getattr(app.state, "task_scheduler", None).snapshot()
            if getattr(app.state, "task_scheduler", None) is not None
            else None
        ),
    }


@app.post("/api/runtime/shutdown", status_code=202)
def request_runtime_shutdown(request: Request) -> dict[str, Any]:
    """Ask this exact local backend instance to stop gracefully."""

    supplied_identity = request.headers.get("x-substar-instance-id", "").strip()
    if not supplied_identity or supplied_identity != APP_INSTANCE_ID:
        raise HTTPException(status_code=403, detail="runtime instance identity mismatch")
    if _UVICORN_SERVER is None:
        raise HTTPException(
            status_code=503,
            detail="runtime shutdown is unavailable for this server host",
        )
    app.state.shutdown_requested = True
    server = _UVICORN_SERVER
    if server is not None:
        server.should_exit = True
    return {
        "accepted": True,
        "instance_id": APP_INSTANCE_ID,
        "status": "shutting_down",
    }


@app.post("/api/project-creations/{job_id}/retry")
def retry_workbench_split_job(job_id: str) -> JSONResponse:
    _restore_persisted_jobs(job_id)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        if job.workflow_mode != "subtitle_creation":
            raise HTTPException(status_code=400, detail="该任务不是工作台切分任务")
        if job.status not in {"failed", "interrupted"}:
            raise HTTPException(status_code=409, detail="只有失败或中断任务可以重试")
        canonical_retry_ids: tuple[str, ...] = ()
        if job.workflow_mode == "subtitle_creation":
            retryable: list[str] = []
            for task_id in (job.transcription_task_id, job.segmentation_task_id):
                if not task_id:
                    continue
                try:
                    task = _task_service().get_task(task_id)
                except TaskNotFoundError:
                    continue
                if task["state"] in {"failed", "interrupted"}:
                    retryable.append(task_id)
            if not retryable:
                raise HTTPException(
                    status_code=409,
                    detail="规范任务已经终结，当前项目损坏不能通过重复调用供应商修复",
                )
            canonical_retry_ids = tuple(retryable)
        media_path, auxiliary_path = _workbench_retry_inputs(job)
        history_path = job.job_dir / "creation_attempts.json"
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            history = {"schema_version": "substar.creation-attempts.v1", "attempts": []}
        attempts = history.get("attempts")
        if not isinstance(attempts, list):
            attempts = []
        attempts.append(
            {
                "attempt": job.attempt,
                "status": job.status,
                "message": job.message,
                "error": job.error,
                "finished_at": time.time(),
            }
        )
        atomic_write_json(history_path, {**history, "attempts": attempts})
        job.input_path = media_path
        job.auxiliary_path = auxiliary_path
        job.attempt += 1
        job.status = "queued"
        job.message = f"第 {job.attempt} 次尝试等待处理"
        job.progress = 0.0
        job.error = ""
        _append_runtime_log(job, f"retry requested: attempt {job.attempt}")
        _persist_job(job)
        result = job.public()
    if canonical_retry_ids:
        for task_id in canonical_retry_ids:
            _task_service().retry(task_id)
        return JSONResponse(result, status_code=202)
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return JSONResponse(result, status_code=202)


def _workbench_job_for_export(job_id: str) -> Job:
    _restore_persisted_jobs(job_id)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status not in {"completed", "awaiting_edit"}:
        raise HTTPException(status_code=409, detail="切分任务尚未完成")
    if job.workflow_mode not in PROJECT_CREATION_MODES:
        raise HTTPException(status_code=400, detail="该任务不是可导出的切分稿")
    return job


def _workbench_media_path(job: Job) -> Path | None:
    if job.input_path.is_file() and job.input_path.suffix.lower() != ".md":
        return job.input_path
    input_dir = job.job_dir / "input"
    return next(
        (
            path
            for path in input_dir.iterdir()
            if path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS
        ),
        None,
    ) if input_dir.is_dir() else None


def _source_only_srt(text: str) -> str:
    rendered: list[str] = []
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    for position, block in enumerate(blocks, start=1):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        rendered.append(f"{position}\n{lines[1]}\n{lines[2]}")
    if not rendered:
        raise HTTPException(status_code=409, detail="当前切分稿无法生成 A 字幕")
    return "\n\n".join(rendered) + "\n"


def _materialize_latest_source_srt(job: Job) -> Path:
    revision = ProjectStore.open(job.job_dir / "project").load_latest()
    if revision is None:
        raise OSError("当前项目还没有可导出的编辑器文档")
    temp_root = Path(tempfile.mkdtemp(prefix="substar-source-export-"))
    target = temp_root / "source.srt"
    try:
        target.write_text(
            render_document_srt(revision.document, SubtitleExportMode.SOURCE),
            encoding="utf-8-sig",
        )
        return target
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def _temporary_srt_response(text: str, filename: str) -> FileResponse:
    descriptor, raw_path = tempfile.mkstemp(prefix="substar-export-", suffix=".srt")
    os.close(descriptor)
    path = Path(raw_path)
    path.write_text(text, encoding="utf-8-sig")
    return FileResponse(
        path,
        media_type="application/x-subrip",
        filename=filename,
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@app.get("/api/project-creations/{job_id}/subtitles/{mode}")
def export_workbench_subtitles(job_id: str, mode: str) -> FileResponse:
    aliases = {
        "a": "a",
        "b": "b",
        "ab_single": "ab_inline",
        "ab_double": "ab_two_line",
    }
    if mode not in aliases:
        raise HTTPException(status_code=404, detail="字幕导出模式不存在")
    job = _workbench_job_for_export(job_id)
    punctuation_rules = _editor_punctuation_rules(job.job_dir)
    top_role = _translation_top_line_role(job.job_dir)
    source_line = "top" if top_role == "source" else "bottom"
    target_line = "bottom" if top_role == "source" else "top"
    stem = re.sub(r"[^0-9A-Za-z_.-]+", "_", Path(job.filename).stem)[:80] or "substar"
    if mode == "a":
        try:
            source_path = _materialize_latest_source_srt(job)
        except OSError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "source_export_unavailable",
                    "message": str(exc) or "当前切分稿无法导出",
                },
            ) from exc
        try:
            text = _project_source_srt(
                _source_only_srt(source_path.read_text(encoding="utf-8-sig")),
                punctuation_rules,
                source_line,
            )
        finally:
            if source_path.parent.name.startswith("substar-source-export-"):
                shutil.rmtree(source_path.parent, ignore_errors=True)
        return _temporary_srt_response(text, f"{stem}_A.srt")
    state = _translation_export_state(job.job_dir)
    if state is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "translation_unavailable", "message": "当前稿件尚无译文"},
        )
    blocks = [
        BilingualBlock(
            number=int(cue["number"]),
            timing=str(cue["timing"]),
            source=project_punctuation(
                str(cue["source"]), punctuation_rules, source_line
            ),
            target=project_punctuation(
                str(cue["target"]), punctuation_rules, target_line
            ),
        )
        for cue in state["cues"]
    ]
    names = {
        "b": f"{stem}_B.srt",
        "ab_single": f"{stem}_AB_inline.srt",
        "ab_double": f"{stem}_AB_two_line.srt",
    }
    return _temporary_srt_response(
        render_track(blocks, mode=aliases[mode]),
        names[mode],
    )




def _restore_persisted_jobs(wanted_id: str = "") -> None:
    _prune_missing_jobs()
    root = _relay_output_root()
    if wanted_id:
        job_dirs = [root / wanted_id]
    else:
        job_dirs = sorted({
            path.parent
            for pattern in ("*/creation_state.json", "*/tutorial_project.json")
            for path in root.glob(pattern)
        })
    restored: list[Job] = []
    for job_dir in job_dirs:
        status_path = job_dir / "creation_state.json"
        tutorial_path = job_dir / "tutorial_project.json"
        if not _has_durable_job_identity(job_dir):
            continue
        register_packaged_tutorial = not status_path.is_file() and tutorial_path.is_file()
        if not status_path.is_file() and not tutorial_path.is_file():
            continue
        job_id = job_dir.name
        with JOBS_LOCK:
            if job_id in JOBS:
                continue
        try:
            if status_path.is_file():
                value = json.loads(status_path.read_text(encoding="utf-8"))
            else:
                tutorial = json.loads(tutorial_path.read_text(encoding="utf-8"))
                case_id = str(tutorial.get("case_id", ""))
                if case_id not in {"reference-script-v1", "advanced-ai-v1"}:
                    continue
                info = load_task_info(job_dir, job_id)
                creation = json.loads(
                    (job_dir / "project_creation.json").read_text(encoding="utf-8")
                )
                value = {
                    "filename": str(creation.get("source_file") or "tutorial.wav"),
                    "display_name": str(info["display_name"]),
                    "workflow_mode": "subtitle_creation",
                    "settings_overrides": task_info_settings(info),
                    "status": "awaiting_edit",
                    "message": "教程已准备完成",
                    "progress": 1.0,
                    "created_at": tutorial_path.stat().st_mtime,
                }
            files = [
                {"name": str(item["name"]), "size": int(item.get("size", 0))}
                for item in value.get("files", [])
                if isinstance(item, dict) and item.get("name")
            ]
            restored_job = Job(
                    id=job_id,
                    filename=str(value.get("filename", job_id)),
                    display_name=str(value.get("display_name", "")),
                    job_dir=job_dir.resolve(),
                    input_path=(job_dir / "segmentation_material.json").resolve(),
                    workflow_mode=str(value.get("workflow_mode", "")),
                    source_job_name=str(value.get("source_job_name", "")),
                    settings_overrides=dict(value.get("settings_overrides", {})),
                    status=str(value.get("status", "completed")),
                    message=str(value.get("message", "")),
                    progress=float(value.get("progress", 0)),
                    error=str(value.get("error", "")),
                    files=files,
                    created_at=float(value.get(
                        "created_at",
                        (status_path if status_path.is_file() else tutorial_path).stat().st_mtime,
                    )),
                    attempt=max(1, int(value.get("attempt", 1))),
                    cancel_requested=bool(value.get("cancel_requested", False)),
                    transcription_task_id=str(value.get("transcription_task_id", "")),
                    segmentation_task_id=str(value.get("segmentation_task_id", "")),
                    submission_key=str(value.get("submission_key", "")),
                    submission_fingerprint=str(
                        value.get("submission_fingerprint", "")
                    ),
                )
            if register_packaged_tutorial:
                _persist_job(restored_job)
            if restored_job.workflow_mode == "subtitle_creation":
                try:
                    (
                        restored_job.input_path,
                        restored_job.auxiliary_path,
                    ) = _workbench_retry_inputs(restored_job)
                except HTTPException:
                    pass
            editor_ready = (
                restored_job.workflow_mode == "subtitle_creation"
                and _editor_ready(job_dir)
            )
            if restored_job.cancel_requested:
                # Startup reconciliation has already removed the previous
                # process owner.  Preserve the user's cancellation even if a
                # revision happened to commit immediately before the crash.
                for task_id in (
                    restored_job.segmentation_task_id,
                    restored_job.transcription_task_id,
                ):
                    if not task_id:
                        continue
                    try:
                        task = _task_service().get_task(task_id)
                        if task["state"] in {"queued", "running"}:
                            _task_service().request_cancel(task_id)
                    except (RuntimeError, TaskNotFoundError):
                        pass
                restored_job.status = "cancelled"
                restored_job.message = "任务已取消，项目文件已保留"
                restored_job.error = ""
                _persist_job(restored_job)
            elif editor_ready:
                # The API process can restart while the detached Stage child
                # keeps running. Reconcile its durable completion artifacts
                # instead of leaving a successfully generated editor project
                # in the misleading "interrupted" state.
                restored_job.status = "awaiting_edit"
                restored_job.message = "项目已创建，可以进入编辑模式"
                restored_job.progress = 1.0
                restored_job.error = ""
                restored_job.files = [
                    {"name": path.name, "size": path.stat().st_size}
                    for path in sorted(status_path.parent.iterdir())
                    if path.is_file()
                ]
                _persist_job(restored_job)
                _register_completed_beginner_tutorial(restored_job)
            elif (
                restored_job.workflow_mode == "subtitle_creation"
                and restored_job.transcription_task_id
                and restored_job.segmentation_task_id
            ):
                _refresh_canonical_job_projection(restored_job)
            elif restored_job.status in {"queued", "running"}:
                restored_job.status = "interrupted"
                restored_job.message = "服务重启中断了任务，可从有效阶段产物重新创建"
                restored_job.error = ""
                _persist_job(restored_job)
            restored.append(restored_job)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    if restored:
        with JOBS_LOCK:
            for job in restored:
                JOBS.setdefault(job.id, job)


@app.get("/api/project-creations")
def list_jobs() -> list[dict[str, Any]]:
    _restore_persisted_jobs()
    _prune_missing_jobs()
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    for job in jobs:
        _refresh_canonical_job_projection(job)
    with JOBS_LOCK:
        return [
            job.public()
            for job in sorted(jobs, key=lambda x: x.created_at, reverse=True)
        ]


@app.get("/api/project-creations/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    _restore_persisted_jobs(job_id)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
    _refresh_canonical_job_projection(job)
    with JOBS_LOCK:
        return job.public()


@app.post("/api/project-creations/{job_id}/resume")
def resume_job(job_id: str) -> JSONResponse:
    _restore_persisted_jobs(job_id)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        if job.status != "interrupted":
            raise HTTPException(status_code=409, detail="只有中断任务可以恢复")
        job.status = "queued"
        job.message = "正在从已有检查点恢复"
        job.error = ""
        _persist_job(job)
        result = job.public()
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return JSONResponse(result, status_code=202)


@app.get("/api/project-creations/{job_id}/files/{filename}")
def download_file(job_id: str, filename: str) -> FileResponse:
    _restore_persisted_jobs(job_id)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    safe_name = Path(filename).name
    path = (job.job_dir / safe_name).resolve()
    if job.job_dir.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path, filename=safe_name)


def main() -> None:
    import logging
    import uvicorn

    global _UVICORN_SERVER

    host = "127.0.0.1"
    port = startup_port()
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)
    if os.environ.get("SUBSTAR_OPEN_BROWSER", "1") != "0":
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="critical",
        access_log=False,
    )
    server = uvicorn.Server(config)
    _UVICORN_SERVER = server
    try:
        server.run()
    finally:
        _UVICORN_SERVER = None


if __name__ == "__main__":
    main()
