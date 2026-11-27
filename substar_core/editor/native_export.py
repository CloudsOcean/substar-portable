"""Short-lived handoff between the local editor and the Windows save dialog."""
from __future__ import annotations

import os
import re
import threading
import time
from uuid import uuid4

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from .output_api import _same_origin, _snapshot, router

_LOCK = threading.Lock()
_TICKETS = {}
_LAST_BRIDGE_POLL = float('-inf')
_TTL = 5 * 60 * 60
_TERMINAL = {"saved", "cancelled", "error"}


class NativeExportTitle(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class NativeExportUpdate(BaseModel):
    action: str
    job_id: str = ""
    folder: str = Field(default="", max_length=32767)
    message: str = Field(default="", max_length=1000)


def _local(request: Request):
    _same_origin(request)
    if (os.name != "nt" or request.url.hostname not in {"127.0.0.1", "localhost"}
            or not request.client or request.client.host not in {"127.0.0.1", "::1"}):
        raise HTTPException(404, "Windows 原生导出仅在本机使用")


def _ticket(token: str):
    item = _TICKETS.get(token)
    if item is None or time.monotonic() - item["created"] > _TTL:
        raise HTTPException(404, "原生导出请求已失效")
    return item


def _public(item):
    return {key: item[key] for key in ("state", "title", "project_id", "job_id", "folder", "message")}


@router.post("/projects/{project_id}/native-output", status_code=201)
def start_native_output(project_id: str, body: NativeExportTitle, request: Request):
    _local(request)
    from .http_api import _safe_export_name, project_job_path

    project_job_path(project_id)
    title = _safe_export_name(body.title.strip())
    with _LOCK:
        now = time.monotonic()
        for token, entry in list(_TICKETS.items()):
            if now - entry["created"] > (60 if entry["state"] == "choosing" else _TTL):
                _TICKETS.pop(token)
        if sum(entry["state"] not in _TERMINAL for entry in _TICKETS.values()) >= 8:
            raise HTTPException(429, "原生导出请求过多，请稍后重试")
        token = uuid4().hex
        _TICKETS[token] = dict(created=now, state="choosing", title=title, project_id=project_id,
                               job_id="", folder="", message="")
    return {"token": token}


@router.get("/native-output/availability")
def native_output_availability(request: Request):
    _local(request)
    with _LOCK:
        active = any(item["state"] in {"dialog", "selected", "rendering"}
                     for item in _TICKETS.values())
        return {"available": active or time.monotonic() - _LAST_BRIDGE_POLL < 5}


@router.post("/native-output/claim")
def claim_native_output(request: Request):
    global _LAST_BRIDGE_POLL
    _local(request)
    instance = os.environ.get("SUBSTAR_INSTANCE_ID")
    if not instance or request.headers.get("x-substar-instance-id") != instance:
        raise HTTPException(403, "Windows 导出组件不属于当前 Substar 实例")
    with _LOCK:
        now = time.monotonic()
        _LAST_BRIDGE_POLL = now
        for token, item in _TICKETS.items():
            if item["state"] == "choosing" and now - item["created"] <= 60:
                item["state"] = "dialog"
                return {"token": token, **_public(item)}
    return {"token": ""}


@router.get("/native-output/{token}")
def native_output_status(token: str, request: Request):
    _local(request)
    with _LOCK:
        return _public(_ticket(token))


@router.post("/native-output/{token}")
def update_native_output(token: str, body: NativeExportUpdate, request: Request):
    _local(request)
    with _LOCK:
        item = _ticket(token)
        state = item["state"]
        match body.action, state:
            case "selected", "dialog":
                item["state"] = "selected"
            case "render", "selected":
                if not re.fullmatch(r"[0-9a-f]{32}", body.job_id):
                    raise HTTPException(422, "导出任务编号无效")
                _snapshot((item["project_id"], body.job_id))
                item.update(state="rendering", job_id=body.job_id)
            case "saved", "rendering":
                item.update(state="saved", folder=body.folder)
            case "cancelled", ("choosing" | "dialog" | "selected" | "cancelled"):
                item["state"] = "cancelled"
            case "cancelled" | "error", "rendering" if _snapshot((item["project_id"], item["job_id"]))["state"] == "cancelled":
                item["state"] = "cancelled"
            case "error", "cancelled":
                pass
            case "error", ("choosing" | "dialog" | "selected" | "rendering"):
                item.update(state="error", message=body.message)
            case _:
                raise HTTPException(409, "原生导出状态已变化")
        return _public(item)
