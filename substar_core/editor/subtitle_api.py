"""Non-AI subtitle project creation and ordinary source text editing."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel, Field
from substar_core.artifacts import atomic_write_json
from substar_core.domain import ChangeKind, ChangeProvenance
from substar_core.storage import ProjectStore, ProjectConflictError
from .application.subtitle_project_import import preview_subtitle_project, build_subtitle_document

router = APIRouter(prefix="/api", tags=["subtitle-projects"])


def read_subtitle(file):
    if file is None:
        return None
    data = file.file.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("字幕文件不能超过 4 MB")
    if not str(file.filename).lower().endswith(".srt"):
        raise ValueError("请选择 SRT 字幕文件")
    try:
        return data.decode("utf-16" if data.startswith((b'\xff\xfe', b'\xfe\xff')) else "utf-8-sig")
    except UnicodeError as exc:
        raise ValueError("字幕须使用 UTF-8 或 UTF-16 编码") from exc


def make_preview(primary, secondary, mode, separator, first_track):
    preview = preview_subtitle_project(primary, secondary=secondary, mode=mode,
                                       separator=separator, first_track=first_track)
    preview["confirmation"] = hashlib.sha256(json.dumps(preview, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return preview


@router.post("/subtitle-projects/preview")
def preview_import(subtitle: UploadFile = File(...), secondary: UploadFile | None = File(None),
                   mode: str = Form("single"), separator: str = Form("|"), first_track: str = Form("source")):
    try:
        return make_preview(read_subtitle(subtitle), read_subtitle(secondary), mode, separator, first_track)
    except ValueError as exc:
        raise HTTPException(422, detail={"message": str(exc)}) from exc


@router.post("/subtitle-projects")
def create_import(subtitle: UploadFile = File(...), secondary: UploadFile | None = File(None),
                  media: UploadFile | None = File(None), media_reference_token: str = Form(""),
                  mode: str = Form("single"), separator: str = Form("|"), first_track: str = Form("source"),
                  confirmation: str = Form(...), source_language: str = Form("Auto"), target_language: str = Form("zh-CN"),
                  source_hard_limit: int = Form(55, ge=1, le=500), target_hard_limit: int = Form(24, ge=1, le=500)):
    from .http_api import _projects_root, save_task_info
    from substar_core.environment_doctor import _find_tool
    from substar_core.media_relink import resolve_selection
    from substar_core.media_reference import write_reference
    from substar_core.task_info import SOURCE_LANGUAGES, TARGET_LANGUAGES
    try:
        if source_language not in SOURCE_LANGUAGES or target_language not in TARGET_LANGUAGES:
            raise ValueError("请选择有效的原文和译文语言")
        primary, second = read_subtitle(subtitle), read_subtitle(secondary)
        preview = make_preview(primary, second, mode, separator, first_track)
        if not preview["can_import"] or confirmation != preview["confirmation"]:
            raise ValueError("字幕内容或格式已变化，请重新预览并确认")
        source = Path(resolve_selection("__import__", media_reference_token)) if media_reference_token else None
        if source is None and media is None:
            raise ValueError("请选择视频或音频")
        name = source.name if source else Path(str(media.filename).replace('\\', '/')).name
        if Path(name).suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mp3", ".wav", ".m4a"}:
            raise ValueError("不支持的媒体类型")
        ffmpeg = _find_tool("ffmpeg")
        if not ffmpeg:
            raise ValueError("FFmpeg 未就绪，不能生成波形")
    except (ValueError, OSError) as exc:
        raise HTTPException(422, detail={"message": str(exc)}) from exc
    project_id = datetime.now().strftime("%Y%m%d_%H%M%S_subtitle_") + uuid.uuid4().hex[:6]
    root = _projects_root() / project_id
    root.mkdir(parents=True, exist_ok=False)
    (root / "input").mkdir()
    (root / "input" / "source.srt").write_text(primary, encoding="utf-8")
    if second is not None:
        (root / "input" / "secondary.srt").write_text(second, encoding="utf-8")
    if source:
        write_reference(root, source)
    else:
        source = root / "input" / ("media" + Path(name).suffix.lower())
        with source.open("wb") as output:
            shutil.copyfileobj(media.file, output, length=1024 * 1024)
    try:
        subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(root / "audio_16k_mono.wav")],
                       check=True, capture_output=True, timeout=600,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError) as exc:
        atomic_write_json(root / "import_failure.json", {"message": "媒体解码失败", "source": name})
        raise HTTPException(422, detail={"message": "媒体解码失败，未创建可编辑工程；请检查音轨或 FFmpeg"}) from exc
    document = build_subtitle_document(preview, project_id)
    atomic_write_json(root / "run_manifest.json", {"schema_version": "substar.run-manifest.v1", "source_file": "input/" + source.name,
                                                   "input_mode": "subtitle", "media": {"duration_seconds": None}})
    atomic_write_json(root / "subtitle_import.json", preview)
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', Path(name).stem)[:120].strip() or "字幕导入"
    save_task_info(root, project_id, {"display_name": title, "language": source_language, "target_language_mode": target_language,
                                     "source_hard_limit": source_hard_limit, "target_hard_limit": target_hard_limit})
    store = ProjectStore.create(root / "project", project_id=project_id)
    store.save(document, provenance=ChangeProvenance(ChangeKind.IMPORT, "subtitle_import"))
    return {"project_id": project_id, "editor_url": f"/editor?project={project_id}"}


class SourceTextRequest(BaseModel):
    expected_revision_id: str
    text: str = Field(min_length=1, max_length=20000)


@router.put("/projects/{project_id}/cues/{cue_id}/source-text")
def set_source_text(project_id: str, cue_id: str, payload: SourceTextRequest):
    from .http_api import open_project_store, revision_payload
    store = open_project_store(project_id)
    latest = store.load_latest()
    if latest is None:
        raise HTTPException(404)
    cue = next((c for c in latest.document.cues if c.cue_id == cue_id), None)
    if cue is None or cue.source_text is None or cue.state.value != "active":
        raise HTTPException(422, detail={"message": "此字幕不支持普通原文编辑"})
    if not payload.text.strip():
        raise HTTPException(422, detail={"message": "原文不能为空"})
    updated = replace(latest.document, cues=tuple(replace(c, source_text=payload.text) if c.cue_id == cue_id else c for c in latest.document.cues))
    try:
        saved = store.save(updated, provenance=ChangeProvenance(ChangeKind.MANUAL, "set_source_text"), expected_revision_id=payload.expected_revision_id)
    except ProjectConflictError as exc:
        raise HTTPException(409, detail={"message": "字幕版本已变化，请刷新后重试"}) from exc
    return revision_payload(saved)
