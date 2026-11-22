"""Generate a batch of media and subtitle files from one saved revision."""
from __future__ import annotations

from fractions import Fraction
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Literal
from uuid import uuid4
from zipfile import ZIP_STORED, ZipFile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from substar_core.cut_timeline import export_xml, frame_intervals, kept_intervals, projected_subtitles
from substar_core.export import SubtitleExportMode, render_document_srt, render_document_txt
from substar_core.media.video_encoding import EncodingError, encode_video, ffmpeg_tool, run_ffmpeg

from .cut_api import inspect_media, revision


router = APIRouter()
VideoRate = Literal["source", "24000/1001", "24", "25", "30000/1001", "30", "50", "60000/1001", "60"]
_SUBTITLE_LABELS = {
    SubtitleExportMode.SOURCE: "上行字幕",
    SubtitleExportMode.TARGET: "下行字幕",
    SubtitleExportMode.AB_SINGLE: "上下行单行字幕",
    SubtitleExportMode.AB_DOUBLE: "上下行双行字幕",
}
_RESOLUTIONS = {"720": (1280, 720), "1080": (1920, 1080), "1440": (2560, 1440), "2160": (3840, 2160)}
_AUDIO_CODECS = {"wav": "pcm_s24le", "mp3": "libmp3lame", "m4a": "aac", "flac": "flac"}
_OUTPUT_DIR = tempfile.TemporaryDirectory(prefix="substar-media-exports-")
_LOCK = threading.Lock()
_WORKER = threading.Semaphore(1)
_JOBS: dict[tuple[str, str], dict] = {}
_TERMINAL = {"ready", "error"}


class VideoOptions(BaseModel):
    resolution: Literal["source", "720", "1080", "1440", "2160"] = "source"
    format: Literal["mp4", "mov"] = "mp4"
    codec: Literal["h264", "hevc"] = "h264"
    fps: VideoRate = "source"
    bitrate: Literal["auto", "2M", "4M", "8M", "12M", "20M", "40M"] = "auto"


class AudioOptions(BaseModel):
    format: Literal["wav", "mp3", "m4a", "flac"] = "wav"
    sample_rate: Literal["source", "16000", "22050", "44100", "48000", "96000"] = "source"


class SubtitleOptions(BaseModel):
    formats: list[Literal["srt", "txt"]] = Field(min_length=1, max_length=2)
    modes: list[SubtitleExportMode] = Field(min_length=1, max_length=4)


class OutputRequest(BaseModel):
    revision_id: str
    export_sequence: int = Field(ge=1, le=999999)
    edited: bool = False
    plan_id: str | None = None
    video: VideoOptions | None = None
    audio: AudioOptions | None = None
    subtitles: SubtitleOptions | None = None
    title: str | None = Field(default=None, min_length=1, max_length=120)
    destination: str | None = Field(default=None, max_length=2048)
    project_files: list[Literal["subtitle-project", "cut-xml"]] = Field(default_factory=list, max_length=2)


def _snapshot(key):
    with _LOCK:
        job = _JOBS.get(key)
        if job is None:
            raise HTTPException(404, "导出任务已过期或不存在")
        return {name: value for name, value in job.items() if name not in {"directory", "workspace", "finished", "bundle_lock"}}


def _update(key, **fields):
    with _LOCK:
        _JOBS[key].update(fields)


def _directory(value):
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise HTTPException(422, "请选择一个已存在的导出目录")
    return path.resolve()


def _same_origin(request):
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "请在 Substar 编辑器中操作")


@router.get("/projects/{project_id}/output-destination")
def output_destination(project_id: str, request: Request, browse: bool = False, path: str | None = None):
    from .http_api import project_job_path

    _same_origin(request)
    project_dir = project_job_path(project_id)
    downloads = Path.home() / "Downloads"
    default = downloads if downloads.is_dir() else project_dir
    if not browse:
        return {"path": str(default)}
    directory = _directory(path) or default
    try:
        folders = sorted((entry for entry in directory.iterdir()
                          if entry.is_dir() and not entry.name.startswith(".")),
                         key=lambda entry: entry.name.casefold())
    except OSError as exc:
        raise HTTPException(422, "无法读取此目录，请选择其他位置") from exc
    roots = [directory.anchor]
    if os.name == "nt":
        from ctypes import windll
        mask = windll.kernel32.GetLogicalDrives()
        roots = [f"{chr(65 + bit)}:{os.sep}" for bit in range(26) if mask & (1 << bit)]
        if directory.anchor not in roots:
            roots.append(directory.anchor)
    return {
        "path": str(directory),
        "parent": str(directory.parent) if directory.parent != directory else None,
        "root": directory.anchor,
        "roots": roots,
        "folders": [{"name": entry.name, "path": str(entry)} for entry in folders],
    }


@router.post("/projects/{project_id}/output-jobs", status_code=202)
def start_output(project_id: str, payload: OutputRequest, request: Request):
    from .http_api import _project_media_source

    _same_origin(request)
    destination = _directory(payload.destination)
    has_content = bool(payload.video or payload.audio or payload.subtitles)
    if not (has_content or payload.project_files):
        raise HTTPException(422, "请选择要导出的内容")
    if payload.audio and payload.audio.format == "mp3" and payload.audio.sample_rate == "96000":
        raise HTTPException(422, "MP3 采样率最高为 48 kHz")
    if payload.title is not None and not payload.title.strip():
        raise HTTPException(422, "请输入导出标题")
    saved = revision(project_id, payload.revision_id)
    plan = None
    cut_content = payload.edited and has_content
    if cut_content or "cut-xml" in payload.project_files:
        timeline = json.loads(saved.document.properties.timeline_json or "{}")
        plan_id = payload.plan_id or timeline.get("active_plan_id")
        plan = next((item for item in timeline.get("plans", []) if item["id"] == plan_id), None)
        if plan is None:
            raise HTTPException(422, "请先在剪辑中选择一个方案")
    source = _project_media_source(project_id)[0] if (plan is not None or payload.video or payload.audio or payload.project_files) else None
    key = (project_id, uuid4().hex)
    directory = Path(_OUTPUT_DIR.name) / key[1]
    stale = []
    with _LOCK:
        for old_key, job in list(_JOBS.items()):
            if job.get("finished", time.monotonic()) < time.monotonic() - 86400:
                stale.append(_JOBS.pop(old_key)["workspace"])
        if sum(job["state"] not in _TERMINAL for job in _JOBS.values()) >= 8:
            raise HTTPException(429, "导出队列已满，请稍后重试")
        _JOBS[key] = {"id": key[1], "directory": directory, "workspace": directory, "state": "queued", "progress": 0,
                      "files": [], "bundle_lock": threading.Lock()}
    for old in stale:
        shutil.rmtree(old, ignore_errors=True)
    threading.Thread(target=_generate, args=(key, payload, saved, plan, source, directory, destination, cut_content),
                     daemon=True, name="media-export").start()
    return _snapshot(key)


@router.get("/projects/{project_id}/output-jobs/{job_id}")
def output_status(project_id: str, job_id: str):
    return _snapshot((project_id, job_id))


@router.get("/projects/{project_id}/output-jobs/{job_id}/files/{file_index}")
def output_file(project_id: str, job_id: str, file_index: int):
    key = (project_id, job_id)
    with _LOCK:
        job = _JOBS.get(key)
        if not job or job["state"] != "ready" or not 0 <= file_index < len(job["files"]):
            raise HTTPException(404, "该导出文件不可用")
        filename = job["files"][file_index]["name"]
        path = job["directory"] / filename
    return FileResponse(path, filename=filename)


@router.get("/projects/{project_id}/output-jobs/{job_id}/bundle")
def output_bundle(project_id: str, job_id: str):
    key = (project_id, job_id)
    with _LOCK:
        job = _JOBS.get(key)
        if not job or job["state"] != "ready":
            raise HTTPException(404, "导出文件不可用")
        job["finished"] = time.monotonic()
        directory = job["directory"]
        workspace = job["workspace"]
        bundle_lock = job["bundle_lock"]
        files = [file["name"] for file in job["files"]]
        filename = job["bundle_name"]
    bundle = workspace / filename
    with bundle_lock:
        if not bundle.is_file():
            partial = workspace / "bundle.part"
            try:
                with ZipFile(partial, "w", compression=ZIP_STORED) as archive:
                    for file in files:
                        archive.write(directory / file, arcname=file)
                partial.replace(bundle)
            except OSError as exc:
                raise HTTPException(507, f"无法打包导出文件：{exc.strerror}") from exc
            finally:
                partial.unlink(missing_ok=True)
    return FileResponse(bundle, filename=filename, media_type="application/zip")


def _publish(outputs, destination):
    # Publish only complete files and keep existing files at the destination.
    targets = []
    try:
        for source in outputs:
            target = destination / source.name
            if os.name == "nt":
                source.rename(target)
            else:
                os.link(source, target)
            targets.append(target)
    except OSError:
        for target in targets:
            target.unlink(missing_ok=True)
        raise
    return targets


def _generate(key, payload, saved, plan, source, workspace, destination, cut_content):
    from substar_core.project_exchange import stream_subtitle_project

    from .http_api import _named_export, _safe_export_name, project_job_path

    document = saved.document
    scope = "剪辑" if cut_content else "全片"

    def filename(label, extension):
        if payload.title:
            title = _safe_export_name(payload.title.strip())
            return f"{title}_{label}_v{payload.export_sequence:02d}.{extension}"
        return _named_export(key[0], label, payload.export_sequence, extension)

    def update(progress, encoder=None, reason=""):
        _update(key, progress=min(.99, progress), encoder=encoder, fallback_reason=reason)

    staging = None
    try:
        with _WORKER:
            _update(key, state="running")
            workspace.mkdir()
            if destination is not None:
                candidate = destination / f".substar-export-{uuid4().hex}"
                candidate.mkdir()
                staging = candidate
            directory = staging or workspace
            duration, video, audio = inspect_media(source) if (plan or payload.video or payload.audio) else (0, None, None)
            if payload.video and video is None:
                raise ValueError("源媒体没有视频流")
            if payload.audio and audio is None:
                raise ValueError("源媒体没有音频流")
            rate = _source_rate(video)
            intervals = None
            if plan is not None:
                intervals = kept_intervals(plan, duration)
                if video:
                    intervals = frame_intervals(intervals, rate)
                if not intervals:
                    raise ValueError("当前剪辑方案没有保留片段")
            if cut_content:
                document = projected_subtitles(document, intervals)
                duration = sum(end - start for start, end in intervals)

            outputs = []
            if payload.subtitles:
                for suffix in dict.fromkeys(payload.subtitles.formats):
                    render = render_document_srt if suffix == "srt" else render_document_txt
                    for mode in dict.fromkeys(payload.subtitles.modes):
                        output = directory / filename(f"{scope}_{_SUBTITLE_LABELS[mode]}", suffix)
                        output.write_text(render(document, mode), encoding="utf-8", newline="\n")
                        outputs.append(output)
            video_output = directory / filename(f"{scope}_视频", payload.video.format) if payload.video else None
            audio_output = directory / filename(f"{scope}_音频", payload.audio.format) if payload.audio else None
            if video_output or audio_output:
                _render_media(payload, source, intervals if cut_content else None, rate, video, audio, duration,
                              video_output, audio_output, update)
                outputs.extend(output for output in (video_output, audio_output) if output)
            if "subtitle-project" in payload.project_files:
                _update(key, progress=.99 if outputs else 0)
                package = directory / filename("字幕工程", "zip")
                with package.open("wb") as file:
                    for chunk in stream_subtitle_project(project_id=key[0], job_dir=project_job_path(key[0]), revision=saved):
                        file.write(chunk)
                outputs.append(package)
            if "cut-xml" in payload.project_files:
                xml = directory / filename("剪辑方案", "xml")
                xml.write_text(export_xml(source, intervals, rate,
                                           video["width"] if video else 1920, video["height"] if video else 1080,
                                           video is not None, audio is not None), encoding="utf-8")
                outputs.append(xml)
            if destination is not None:
                outputs = _publish(outputs, destination)
            files = [{"name": path.name, "bytes": path.stat().st_size} for path in outputs]
            _update(key, state="ready", progress=1, files=files, bundle_name=filename("导出", "zip"),
                    directory=destination or workspace, destination=str(destination) if destination else None,
                    finished=time.monotonic())
    except (ValueError, EncodingError, OSError, subprocess.SubprocessError, KeyError) as exc:
        shutil.rmtree(workspace, ignore_errors=True)
        fault = (f"导出目录已有同名文件：{Path(exc.filename2 or exc.filename).name}" if isinstance(exc, FileExistsError)
                 and (exc.filename2 or exc.filename) else getattr(exc, "stderr", None) or str(exc))
        if isinstance(fault, bytes):
            fault = fault.decode("utf-8", "replace")
        _update(key, state="error", message=fault[-2000:], finished=time.monotonic())
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        _update(key, state="error", message="导出时遇到异常", finished=time.monotonic())
        raise
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)


def _source_rate(video):
    for value in ((video or {}).get("avg_frame_rate"), (video or {}).get("r_frame_rate")):
        try:
            rate = Fraction(value)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if 0 < rate <= 240:
            return str(rate)
    return "25"


def _cut_filters(intervals, rate, video, audio):
    filters = []
    if video:
        frame_rate = Fraction(rate)
        terms = [
            f"between(n,{round(start * frame_rate)},{round(end * frame_rate) - 1})"
            for start, end in intervals
        ]
        filters.append(f"[0:{video['index']}]fps={rate}:start_time=0,"
                       f"select='{'+'.join(terms)}',setpts=N[v]")
    if audio:
        count = len(intervals)
        for i, (start, end) in enumerate(intervals):
            filters.append(f"[0:{audio['index']}]atrim=start={start:.9f}:end={end:.9f},asetpts=PTS-STARTPTS[a{i}]")
        filters.append("".join(f"[a{i}]" for i in range(count)) + f"concat=n={count}:v=0:a=1[a]")
    return filters


def _render_media(payload, source, intervals, rate, video, audio, duration, video_output, audio_output, update):
    video_map = f"0:{video['index']}" if video_output else ""
    audio_map = f"0:{audio['index']}" if audio else ""
    filters = []
    if intervals is not None:
        filters = _cut_filters(intervals, rate, video if video_output else None, audio)
        video_map = "[v]" if video_output else ""
        audio_map = "[a]" if audio else ""

    if video_output:
        transforms = []
        if payload.video.resolution != "source":
            width, height = _RESOLUTIONS[payload.video.resolution]
            transforms.append(f"scale='if(gte(dar,1),{width},{height})':'if(gte(dar,1),{height},{width})':"
                              "force_original_aspect_ratio=decrease:force_divisible_by=2:reset_sar=1")
        else:
            transforms.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
        if payload.video.fps != "source":
            transforms.append(f"fps={payload.video.fps}")
        filters.append(f"[{video_map.strip('[]')}]" + ",".join(transforms) + "[video_out]")
        video_map = "[video_out]"
    video_audio_map = audio_map
    if intervals is not None and audio and video_output and audio_output:
        filters.append("[a]asplit=2[video_audio][separate_audio]")
        video_audio_map, audio_map = "[video_audio]", "[separate_audio]"

    script = (video_output or audio_output).parent / "filters.txt"
    command = []
    if intervals is not None:
        command += ["-t", f"{max(end for _, end in intervals) + 2 / Fraction(rate):.9f}"]
    command += ["-i", str(source)]
    if filters:
        script.write_text(";\n".join(filters), encoding="utf-8")
        command += ["-/filter_complex", str(script)]

    audio_args = []
    if audio_output:
        options = payload.audio
        audio_args = ["-map", audio_map, "-c:a", _AUDIO_CODECS[options.format]]
        if options.format in {"m4a", "mp3"}:
            audio_args += ["-b:a", "192k"]
        if options.sample_rate != "source":
            audio_args += ["-ar", options.sample_rate]

    if not video_output:
        run_ffmpeg(ffmpeg_tool(), command + audio_args, audio_output, duration, update)
        return

    def args(options):
        result = [*command]
        if audio_output:
            result += [*audio_args, str(audio_output)]
        result += ["-map", video_map, *options]
        if video_audio_map:
            result += ["-map", video_audio_map, "-c:a", "aac", "-b:a", "192k"]
        if payload.video.codec == "hevc":
            result += ["-tag:v", "hvc1"]
        return result + ["-movflags", "+faststart"]

    bitrate = payload.video.bitrate
    encode_video(args, video_output, codec=payload.video.codec, duration=duration,
                 bitrate=None if bitrate == "auto" else bitrate, update=update)
