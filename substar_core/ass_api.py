"""ASS style, export and rendering endpoints for the editor preview menu."""
import concurrent.futures
import json
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from substar_core.ass_subtitles import AssOptions, AssStyle, render_ass, render_media
from substar_core.artifacts import atomic_write_json
from substar_core.config import APP_DATA_DIR
from substar_core.ass_profiles import Profile, resolved_configuration, apply_profile, render_configuration, default_profile

router = APIRouter(prefix="/api")
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="ass-render")
_jobs = {}
_lock = threading.RLock()


def _font_files():
    """Serve installed fonts by opaque ID, never accept a filesystem path."""
    import hashlib
    import os
    result = {}
    roots = [Path(os.environ.get("WINDIR", "C:/Windows"))/"Fonts",
             Path(os.environ.get("LOCALAPPDATA", str(APP_DATA_DIR)))/"Microsoft/Windows/Fonts"]
    for root in roots:
        if root.is_dir():
            for path in root.iterdir():
                if path.suffix.lower() in {".ttf", ".otf", ".ttc"} and path.is_file():
                    result[hashlib.sha256(str(path).encode()).hexdigest()[:24]] = path
    return result


@router.get("/ass/fonts")
def fonts():
    files = _font_files()
    by_name = {path.name.lower(): key for key,path in files.items()}
    available = {}
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as key:
                    for i in range(winreg.QueryInfoKey(key)[1]):
                        name, filename, _ = winreg.EnumValue(key, i)
                        font_id = by_name.get(Path(str(filename)).name.lower())
                        if font_id:
                            for family in name.replace(" (TrueType)", "").replace(" (OpenType)", "").split(" & "):
                                available[family.lower()] = "/api/ass/fonts/"+font_id
            except OSError:
                continue
    except ImportError:
        pass
    fallback = by_name.get("msyh.ttc")
    return {"available":available, "fallback":("/api/ass/fonts/"+fallback) if fallback else None}


@router.get("/ass/fonts/{font_id}")
def font_file(font_id: str):
    path = _font_files().get(font_id)
    if path is None:
        raise HTTPException(404, "字体不存在")
    return FileResponse(path)


def busy():
    with _lock:
        return any(j["status"] in {"queued","running"} for j in _jobs.values())


class StylePreset(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    style: AssStyle


class RenderRequest(BaseModel):
    expected_revision_id: str
    options: AssOptions = Field(default_factory=AssOptions)
    preview_seconds: float | None = Field(default=None, ge=0)
    profile: Profile | None = None
    cue_ids: list[str] | None = None
    layer_id: str | None = None
    layer_ids: list[str] | None = Field(default=None, max_length=16)
    use_configuration: bool = False


class ApplyRequest(BaseModel):
    expected_revision_id: str
    profile: Profile | None = None
    cue_ids: list[str] | None = None
    layer_id: str | None = None
    layer_ids: list[str] | None = Field(default=None, max_length=16)
    reset: bool = False


def _apply_layers(document, payload):
    if payload.layer_ids is not None and not payload.layer_ids:
        raise ValueError("请选择要应用的字幕层")
    for layer_id in payload.layer_ids or [payload.layer_id]:
        document = apply_profile(document, payload.profile, payload.cue_ids,
            getattr(payload, "reset", False), layer_id)
    return document


@router.get("/projects/{project_id}/ass/configuration")
def get_configuration(project_id: str):
    from substar_core.editor.http_api import open_project_store
    latest = open_project_store(project_id).load_latest()
    if latest is None:
        raise HTTPException(404, "项目不存在")
    return resolved_configuration(latest.document).model_dump()


@router.post("/projects/{project_id}/ass/configuration")
def save_configuration(project_id: str, payload: ApplyRequest):
    from substar_core.editor.http_api import open_project_store, _save_document
    latest = open_project_store(project_id).load_latest()
    if latest is None or latest.revision_id != payload.expected_revision_id:
        raise HTTPException(409, "项目已更新，请重试")
    try:
        document = _apply_layers(latest.document, payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _save_document(project_id, expected_revision_id=latest.revision_id, document=document,
        operation="ass_configuration", provenance=document.changes[-1])


@router.get("/ass/profiles")
def profiles():
    path = APP_DATA_DIR / "ass-profiles.json"
    values = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {"默认双语":default_profile().model_dump(), **values}


@router.put("/ass/profiles/{name}")
def save_profile(name: str, payload: Profile):
    if name == "默认双语" or not name.strip() or len(name) > 80:
        raise HTTPException(422, "请为自定义方案输入新名称")
    with _lock:
        values = profiles()
        values.pop("默认双语", None)
        if len(values) >= 64 and name not in values:
            raise HTTPException(422, "方案数量已达上限")
        values[name] = payload.model_copy(update={"name":name}).model_dump()
        atomic_write_json(APP_DATA_DIR / "ass-profiles.json", values)
    return profiles()


@router.delete("/ass/profiles/{name}")
def delete_profile(name: str):
    if name == "默认双语":
        raise HTTPException(422, "默认方案不能删除")
    with _lock:
        values = profiles()
        values.pop("默认双语", None)
        values.pop(name, None)
        atomic_write_json(APP_DATA_DIR / "ass-profiles.json", values)
    return profiles()


@router.get("/ass/styles")
def styles():
    path = APP_DATA_DIR / "ass-styles.json"
    if not path.exists():
        return {"默认":AssStyle().model_dump()}
    return json.loads(path.read_text(encoding="utf-8"))


@router.put("/ass/styles")
def save_style(payload: StylePreset):
    with _lock:
        values = styles()
        if len(values) >= 200 and payload.name not in values:
            raise HTTPException(422, "样式数量已达上限")
        values[payload.name] = payload.style.model_dump()
        atomic_write_json(APP_DATA_DIR / "ass-styles.json", values)
    return values


def material(project_id, payload):
    from substar_core.editor.http_api import open_project_store
    latest = open_project_store(project_id).load_latest()
    if latest is None or latest.revision_id != payload.expected_revision_id:
        raise HTTPException(409, "项目已更新，请重新生成")
    try:
        if payload.use_configuration:
            document = latest.document
            if payload.profile is not None:
                document = _apply_layers(document, payload)
            return render_configuration(document, payload.options.width, payload.options.height)
        return render_ass(latest.document, payload.options)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/projects/{project_id}/ass/export")
def export_ass(project_id: str, payload: RenderRequest):
    return Response(material(project_id,payload), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition":'attachment; filename="captions.ass"'})


@router.post("/projects/{project_id}/ass/render", status_code=202)
def start_render(project_id: str, payload: RenderRequest):
    from substar_core.editor.http_api import _project_media_source
    ass = material(project_id,payload)
    media, _ = _project_media_source(project_id)
    with _lock:
        if sum(j["status"] in {"queued","running"} for j in _jobs.values()) >= 3:
            raise HTTPException(409, "请等待当前字幕渲染任务完成")
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {"job_id":job_id,"status":"queued","project_id":project_id}
    def work():
        with _lock: _jobs[job_id]["status"] = "running"
        try:
            output = render_media(media,ass,APP_DATA_DIR / "ass-renders" / job_id,
                                  preview_seconds=payload.preview_seconds)
            with _lock: _jobs[job_id].update(status="succeeded",output=str(output))
        except Exception:
            with _lock: _jobs[job_id].update(status="failed",error="字幕渲染失败，请检查媒体、字体及 FFmpeg 的 libass 支持")
    _executor.submit(work)
    return dict(_jobs[job_id])


@router.get("/ass/renders/{job_id}")
def render_status(job_id: str):
    with _lock:
        if job_id not in _jobs: raise HTTPException(404,"渲染任务不存在")
        return {k:v for k,v in _jobs[job_id].items() if k != "output"}


@router.get("/ass/renders/{job_id}/file")
def rendered_file(job_id: str):
    with _lock:
        job = _jobs.get(job_id,{})
        if job.get("status") != "succeeded": raise HTTPException(409,"渲染文件尚未就绪")
        path = Path(job["output"])
    return FileResponse(path, filename=path.name)
