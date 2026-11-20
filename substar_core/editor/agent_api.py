"""Local agent harness. Editorial inference belongs to the caller."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
import io
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from typing import Any, Literal
from urllib.parse import urlencode
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from substar_core.artifacts import atomic_write_json
from substar_core.cut_timeline import kept_intervals, project_range, validate_timeline
from substar_core.document_operations import apply_document_batch
from substar_core.domain import ChangeKind, ChangeProvenance, EditorDocument, EntityState
from substar_core.language_layout import layout_tokens
from substar_core.storage import ProjectConflictError, ProjectStore, ProjectStoreError
from substar_core.translation_units import all_units, target_events
from .cut_api import executable, inspect_media

router = APIRouter(prefix="/agent", tags=["agent"])
_lock = threading.RLock()
FEATURES = ("cut", "review", "translation", "organize", "punctuation")
TERMINAL = {"succeeded", "failed", "cancelled"}
POLICY = {
    "cut": {"set_timeline"},
    "translation": {"set_target", "set_target_units", "set_target_unit"},
    "organize": {"delete", "split_cue", "merge_cues", "auto_merge_cues", "set_cue_time", "set_cue_times"},
    "punctuation": {"replace", "batch_replace", "retime_text"},
}


def api():
    from . import http_api
    return http_api


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def fail(status, code, message):
    raise HTTPException(status, detail={"code": code, "message": message})


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def latest(project_id, expected=None):
    revision = api().open_project_store(project_id).load_latest()
    if revision is None:
        fail(404, "empty_project", "项目没有文稿")
    if expected is not None and revision.revision_id != expected:
        fail(409, "revision_conflict", "项目已变化，请读取最新版本后重试")
    return revision


def editor_link(project_id, cue_id=None, number=None):
    query = {"project": project_id}
    if cue_id:
        query["cue_id"] = cue_id
    if number:
        query["cue"] = number
    return "/editor?" + urlencode(query)


def source_text(cue, tokens):
    return cue.source_text if cue.source_text is not None else layout_tokens(
        tokens[tid].text for tid in cue.display_token_ids
        if tokens[tid].state is EntityState.ACTIVE)


def media_data(project_id, document):
    try:
        path, _ = api()._project_media_source(project_id)
        duration, _, _ = inspect_media(path)
        return path, duration
    except (HTTPException, OSError, ValueError, subprocess.SubprocessError):
        duration = max((cue.end for cue in document.cues), default=0)
        return None, duration


def material(project_id, revision, scope):
    document = revision.document
    _, duration = media_data(project_id, document)
    timeline = json.loads(document.properties.timeline_json or "{}")
    plan = next((item for item in timeline.get("plans", []) if item["id"] == timeline.get("active_plan_id")), {})
    intervals = kept_intervals(plan, duration) if duration > 0 else []
    if scope == "all":
        intervals = [(0, duration)]
    visible_targets = {unit["unit_id"]: unit for unit in all_units(document)
                       if any(unit["end"] > a and unit["start"] < b for a, b in intervals)}
    tokens = {token.token_id: token for token in document.display_tokens}
    sources = {token.token_id: token for token in document.source_tokens}
    timings = timeline.get("word_timings", {})
    rows = []
    for cue in document.cues:
        if cue.state is not EntityState.ACTIVE:
            continue
        spans = [(max(cue.start, a), min(cue.end, b)) for a, b in intervals
                 if min(cue.end, b) > max(cue.start, a)]
        if not spans:
            continue
        active_tokens = []
        for tid in cue.display_token_ids:
            token = tokens[tid]
            if token.state is not EntityState.ACTIVE:
                continue
            lineage = [sources[sid] for sid in token.source_token_ids]
            start = min((item.start for item in lineage), default=cue.start)
            end = max((item.end for item in lineage), default=cue.end)
            if tid in timings:
                start, end = timings[tid]["start"] / 1000, timings[tid]["end"] / 1000
            if not any((end > a and start < b) or (start == end and a <= start < b) for a, b in spans):
                continue
            active_tokens.append(dict(token.to_dict(), start=start, end=end))
        text = cue.source_text if cue.source_text is not None else layout_tokens(item["text"] for item in active_tokens)
        target_ids = cue.mapping.get("target_unit_ids", [])
        target_text = ("\n".join(visible_targets[uid]["text"] for uid in target_ids if uid in visible_targets)
                       if target_ids else cue.target.target_text if cue.target else "")
        rows.append({
            "cue_id": cue.cue_id, "cue": cue.index + 1, "start": cue.start, "end": cue.end,
            "speaker": cue.speaker, "source": text, "target": target_text,
            "translation_status": cue.target.translation_status if cue.target else None,
            "partial": spans != [(cue.start, cue.end)],
            "retained_source_ranges": [{"start": a, "end": b} for a, b in spans],
            "output_ranges": [{"start": a, "end": b} for a, b in project_range(cue.start, cue.end, intervals)],
            "tokens": active_tokens, "mapping": {k: v for k, v in cue.mapping.items() if k != "target_units"},
            "editor_url": editor_link(project_id, cue.cue_id, cue.index + 1),
        })
    return rows, intervals


@router.get("")
def capabilities(request: Request):
    base = str(request.base_url).rstrip("/")
    return {
        "schema_version": "substar.agent-capabilities.v1",
        "model_execution": {"default": "caller", "program_model": "explicit_user_selection"},
        "features": {
            "cut": {"interaction": "chat", "commit_type": "set_timeline"},
            "review": {"interaction": "per_cue", "includes": ["audio_evidence", "calibration"]},
            "translation": {"interaction": "chat", "mapping": "many_to_many", "independent_target_ranges": True},
            "organize": {"interaction": "direct", "changes": ["delete_filler", "cue_boundaries"], "blue_tokens": False},
            "punctuation": {"interaction": "direct", "blue_tokens": "text_changes_only"},
        },
        "entry_points": {
            "openapi": base + "/api/agent/openapi.json",
            "guide": base + "/api/agent/guide",
            "projects": base + "/api/agent/projects",
            "registration": base + "/api/agent/projects",
            "context": "/api/agent/projects/{project_id}/context",
            "resolve_cue": "/api/agent/projects/{project_id}/cues/resolve",
            "apply": "/api/agent/projects/{project_id}/apply",
            "operation_reference": base + "/api/agent/operations",
            "runs": "/api/agent/projects/{project_id}/runs",
            "navigation": "/api/agent/projects/{project_id}/navigation",
            "navigation_poll": "/api/agent/projects/{project_id}/navigation/poll",
            "navigation_links": "/api/agent/projects/{project_id}/runs/{run_id}/navigation-links",
            "cut_plan": "/api/agent/projects/{project_id}/cut-plan",
            "translations": "/api/agent/projects/{project_id}/translation-units",
            "evidence": "/api/agent/projects/{project_id}/evidence",
            "undo": "/api/projects/{project_id}/restore-selection",
            "export": "/api/projects/{project_id}/output-jobs",
        },
        "program_model_routes": ["/api/projects/{project_id}/cut-ai", "/api/projects/{project_id}/translation",
                                 "/api/projects/{project_id}/ai-organize", "/api/projects/{project_id}/ai-punctuation",
                                 "/api/projects/{project_id}/ai-calibrate"],
        "rules": ["All times are original-media seconds.", "Use cue_id for stable links, cue for current numbering.",
                  "Read scope=kept before editorial review.", "Apply requires a revision and an idempotency request_id.",
                  "Keep separate before/after entries for adjacent cues."],
    }


@router.get("/operations")
def operation_reference():
    from substar_core.document_operations import _APPLIERS
    examples = {
        "replace": {"token_id": "DISPLAY_TOKEN_ID", "text": "Word,", "expected_text": "word"},
        "batch_replace": {"replacements": [{"token_id": "DISPLAY_TOKEN_ID", "text": "Word,", "expected_text": "word"}]},
        "retime_text": {"cue_id": "CUE_ID", "token_ids": ["DISPLAY_TOKEN_ID"], "text": "several words"},
        "delete": {"token_ids": ["FILLER_TOKEN_ID"]},
        "restore": {"token_ids": ["DISPLAY_TOKEN_ID"]},
        "split_cue": {"cue_id": "CUE_ID", "after_token_id": "DISPLAY_TOKEN_ID"},
        "merge_cues": {"cue_ids": ["LEFT_CUE_ID", "RIGHT_CUE_ID"]},
        "set_cue_time": {"cue_id": "CUE_ID", "start": 1.2, "end": 3.4},
        "set_cue_times": {"cues": [{"cue_id": "CUE_ID", "start": 1.2, "end": 3.4}]},
        "auto_merge_cues": {"gap_ms": 200, "short_count": 3, "absorb": True, "snap_direction": "previous", "absorb_direction": "previous"},
        "set_source_text": {"cue_id": "TOKEN_FREE_CUE_ID", "text": "Corrected imported text."},
        "set_target": {"cue_id": "CUE_ID", "target_text": "译文", "expected_target_text": "原译文"},
        "set_target_unit": {"unit_id": "TARGET_UNIT_ID", "text": "译文", "expected_text": "原译文", "start": 1.2, "end": 3.4},
        "set_target_units": {"replace_cue_ids": ["CUE_ID"], "units": [{
            "unit_id": "TARGET_UNIT_ID", "source_cue_ids": ["CUE_ID"], "start": 1.2, "end": 3.4, "text": "译文", "language": "zh-CN"}]},
        "set_cue_speaker": {"cue_ids": ["CUE_ID"], "speaker": "speaker_1"},
        "set_speaker_names": {"speaker_names": {"speaker_1": "姓名"}},
    }
    return {"schema_version": "substar.agent-operations.v1", "supported_types": sorted(_APPLIERS),
            "operations": [{"type": kind, "payload_example": payload,
                            "features": [feature for feature in FEATURES if feature == "review" or kind in POLICY.get(feature, set())]}
                           for kind, payload in examples.items()],
            "rules": ["Examples use original-media seconds. gap_ms is milliseconds.",
                      "delete/restore also accept cue_ids. Imported token-free text has no word timing.",
                      "retime_text token_ids must be contiguous in one cue; use it when word count changes.",
                      "Use /cut-plan for cutting. Raw set_timeline uses the existing editor timeline schema.",
                      "Caller-supplied provenance is replaced by run provenance."]}


@router.get("/openapi.json")
def agent_openapi(request: Request):
    source = request.app.openapi()
    include = ("/api/agent", "/api/projects/", "/api/tasks", "/api/project-creations", "/api/subtitle-projects", "/api/project-imports")
    return {**source, "paths": {path: value for path, value in source["paths"].items() if path.startswith(include)}}


@router.get("/guide", response_class=PlainTextResponse)
def guide():
    path = Path(__file__).resolve().parents[2] / "docs" / "agent-api.md"
    if not path.is_file():
        path = Path(__file__).resolve().parents[2] / "docs" / "architecture" / "agent-api.md"
    return path.read_text(encoding="utf-8")


@router.get("/projects")
def projects():
    return api().list_projects()


@router.get("/projects/{project_id}/head")
def head(project_id: str):
    summary = api().open_project_store(project_id).load_summary()
    if not summary:
        fail(404, "empty_project", "项目没有文稿")
    data = read_runs(project_id)
    return {"revision_id": summary["latest_revision_id"], "runs_version": data["version"],
            "runs": [{key: run.get(key) for key in ("run_id", "feature", "instruction", "state", "progress", "message", "updated_at")}
                     for run in reversed(data["runs"][-20:])]}


@router.get("/projects/{project_id}/context")
def context(project_id: str, scope: Literal["kept", "all"] = "kept",
            revision_id: str | None = None, cue_id: str | None = None, cue: int | None = Query(None, ge=1),
            before: int = Query(5, ge=0, le=50), after: int = Query(5, ge=0, le=50),
            offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)):
    revision = latest(project_id, revision_id)
    rows, intervals = material(project_id, revision, scope)
    total = len(rows)
    if cue_id is not None or cue is not None:
        selected = resolve(project_id, cue_id, cue, revision_id)
        index = next((i for i, row in enumerate(rows) if row["cue_id"] == selected["cue_id"]), None)
        if index is None:
            fail(404, "cue_outside_scope", "目标 Cue 已被剪除，可用 scope=all 检查原文")
        offset = max(0, index - before)
        rows = rows[offset:index + after + 1]
        for row in rows:
            row["role"] = "review" if row["cue_id"] == selected["cue_id"] else "context"
    else:
        rows = rows[offset:offset + limit]
    return {
        "project_id": project_id, "revision_id": revision.revision_id,
        "revision_number": revision.revision_number, "document_id": revision.document.document_id,
        "document_hash": revision.document.content_hash(), "scope": scope, "total": total,
        "offset": offset, "next_offset": offset + len(rows) if cue_id is None and cue is None and offset + len(rows) < total else None,
        "time_basis": "original_media_seconds", "rows": rows,
        "retained_ranges": [{"start": a, "end": b} for a, b in intervals],
        "target_units": [dict(unit, retained_source_ranges=[{"start": max(unit["start"], a), "end": min(unit["end"], b)}
                              for a, b in intervals if unit["end"] > a and unit["start"] < b])
                         for unit in target_events(revision.document)
                         if any(unit["end"] > a and unit["start"] < b for a, b in intervals)],
    }


@router.get("/projects/{project_id}/cues/resolve")
def resolve(project_id: str, cue_id: str | None = None, cue: int | None = Query(None, ge=1),
            revision_id: str | None = None):
    revision = latest(project_id, revision_id)
    if not cue_id and cue is None:
        fail(422, "missing_cue", "请提供 cue_id 或 cue 编号")
    # A stable ID remains authoritative after renumbering.
    found = next((item for item in revision.document.cues
                  if (item.cue_id == cue_id if cue_id else item.index + 1 == cue)), None)
    if found is None:
        fail(404, "cue_not_found", "此 Cue 已不存在，请重新读取项目")
    return {"cue_id": found.cue_id, "cue": found.index + 1, "start": found.start, "end": found.end,
            "state": found.state.value, "revision_id": revision.revision_id,
            "editor_url": editor_link(project_id, found.cue_id, found.index + 1)}


def run_path(project_id):
    return api().project_job_path(project_id) / "review" / "agent-runs.json"


def read_runs(project_id):
    path = run_path(project_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 0, "runs": []}


def update_runs(project_id, mutate):
    with _lock:
        data = read_runs(project_id)
        value = mutate(data)
        data["version"] += 1
        atomic_write_json(run_path(project_id), data)
        return deepcopy(value)


def find_run(data, run_id):
    run = next((run for run in data["runs"] if run["run_id"] == run_id), None)
    if run is None:
        fail(404, "run_not_found", "Agent 任务不存在")
    return run


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RunStart(StrictModel):
    request_id: str = Field(min_length=1, max_length=160)
    expected_revision_id: str = Field(min_length=1, max_length=160)
    feature: Literal["cut", "review", "translation", "organize", "punctuation"]
    instruction: str = Field(min_length=1, max_length=10000)
    actor: str = Field(default="agent", min_length=1, max_length=100)
    model: str = Field(default="", max_length=160)


@router.post("/projects/{project_id}/runs")
def start_run(project_id: str, payload: RunStart):
    key = digest(payload.model_dump(exclude_none=True))
    def mutate(data):
        existing = next((run for run in data["runs"] if run["request_id"] == payload.request_id), None)
        if existing:
            if existing["request_hash"] != key:
                fail(409, "request_reused", "request_id 已用于其他任务")
            return existing
        latest(project_id, payload.expected_revision_id)
        run = {**payload.model_dump(), "run_id": "agent_" + uuid.uuid4().hex,
               "request_hash": key, "model_execution": "caller", "state": "running", "version": 1,
               "progress": 0, "message": "", "created_at": now(), "updated_at": now(), "suggestions": []}
        data["runs"].append(run)
        return run
    return update_runs(project_id, mutate)


class RunUpdate(StrictModel):
    expected_version: int = Field(ge=1)
    state: Literal["running", "waiting", "succeeded", "failed", "cancelled"]
    progress: float = Field(ge=0, le=1)
    message: str = Field(default="", max_length=4000)


@router.patch("/projects/{project_id}/runs/{run_id}")
def update_run(project_id: str, run_id: str, payload: RunUpdate):
    def mutate(data):
        run = find_run(data, run_id)
        if run["version"] != payload.expected_version:
            fail(409, "run_conflict", "任务记录已变化，请刷新后重试")
        if run["state"] in TERMINAL and payload.state != run["state"]:
            fail(409, "run_terminal", "任务已经结束")
        run.update(state=payload.state, progress=payload.progress, message=payload.message,
                   updated_at=now(), version=run["version"] + 1)
        return run
    return update_runs(project_id, mutate)


def edit_records(project_id, run_id):
    store = api().open_project_store(project_id)
    entries = store.list_revision_metadata()
    return [{
        "revision_id": entry["revision_id"], "revision_number": entry["revision_number"],
        "parent_revision_id": entry["parent_revision_id"], "created_at": entry["created_at"],
        **entry["provenance"]["metadata"].get("agent_edit", {}),
    } for entry in entries
        if entry["provenance"].get("metadata", {}).get("agent_run_id") == run_id]


@router.get("/projects/{project_id}/runs")
def list_runs(project_id: str):
    latest(project_id)
    return read_runs(project_id)


@router.get("/projects/{project_id}/runs/{run_id}")
def get_run(project_id: str, run_id: str):
    latest(project_id)
    with _lock:
        run = deepcopy(find_run(read_runs(project_id), run_id))
    return {**run, "edits": edit_records(project_id, run_id)}


class AgentOperation(StrictModel):
    type: str = Field(min_length=1, max_length=80)
    payload: dict[str, Any]


class AgentApply(StrictModel):
    request_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)
    expected_revision_id: str = Field(min_length=1, max_length=160)
    operations: list[AgentOperation] = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=4000)
    dry_run: bool = False


@router.post("/projects/{project_id}/apply")
def apply(project_id: str, payload: AgentApply):
    store = api().open_project_store(project_id)
    key = digest(payload.model_dump(exclude={"dry_run"}))
    try:
        prior = store.find_receipt("agent:" + payload.request_id, key)
    except ProjectConflictError as error:
        fail(409, "request_reused", str(error))
    if prior:
        return {"applied": True, "replayed": True, "revision_id": prior.revision_id,
                "revision_number": prior.revision_number}
    with _lock:
        run = deepcopy(find_run(read_runs(project_id), payload.run_id))
    if run["state"] in TERMINAL:
        fail(409, "run_terminal", "任务已结束，请创建新任务")
    revision = latest(project_id, payload.expected_revision_id)
    feature = run["feature"]
    operations = []
    snapshots = []
    for index, operation in enumerate(payload.operations):
        if feature in POLICY and operation.type not in POLICY[feature]:
            fail(422, "feature_boundary", "此操作不属于当前功能的范围")
        raw = operation.model_dump()
        raw["operation_id"] = payload.request_id + ":" + str(index)
        raw["payload"] = {**raw["payload"], "provenance": {
            "kind": "normalization" if feature == "organize" else "ai",
            "operation": "agent_" + feature, "actor": run["actor"],
            "metadata": {"agent_run_id": payload.run_id, "request_id": payload.request_id,
                         "reason": payload.reason, "model_execution": "caller", "model": run["model"]},
        }}
        if feature == "punctuation":
            try:
                _check_punctuation(revision.document, operation)
            except (ValueError, KeyError, TypeError) as error:
                fail(422, "invalid_punctuation", str(error))
        operations.append(raw)
    try:
        result = apply_document_batch(revision.document, operations)
        before_tokens = {t.token_id: t for t in revision.document.display_tokens}
        for token in result.display_tokens:
            previous = before_tokens.get(token.token_id)
            if previous is None or (previous.text, previous.state) != (token.text, token.state):
                snapshots.append({"token_id": token.token_id, "before": previous.text if previous else None,
                                  "after": token.text, "state": token.state.value})
        before_cues = {c.cue_id: c for c in revision.document.cues}
        after_tokens = {t.token_id: t for t in result.display_tokens}
        for cue in result.cues:
            previous = before_cues.get(cue.cue_id)
            before_text = source_text(previous, before_tokens) if previous else ""
            after_text = source_text(cue, after_tokens)
            if previous != cue or before_text != after_text:
                snapshots.append({"cue_id": cue.cue_id, "cue": cue.index + 1,
                                  "before": previous.to_dict() if previous else None, "after": cue.to_dict(),
                                  "before_text": before_text, "after_text": after_text,
                                  "before_target": previous.target.target_text if previous and previous.target else "",
                                  "after_target": cue.target.target_text if cue.target else "",
                                  "editor_url": editor_link(project_id, cue.cue_id, cue.index + 1)})
        for removed in revision.document.cues:
            if not any(c.cue_id == removed.cue_id for c in result.cues):
                snapshots.append({"cue_id": removed.cue_id, "cue": removed.index + 1, "before": removed.to_dict(), "after": None,
                                  "before_text": source_text(removed, before_tokens), "after_text": "",
                                  "before_target": removed.target.target_text if removed.target else "", "after_target": ""})
        changes = {"feature": feature, "reason": payload.reason, "operations": [op.type for op in payload.operations],
                   "items": snapshots}
        if feature == "cut":
            changes["timeline_before"] = revision.document.properties.to_dict().get("timeline")
            changes["timeline_after"] = result.properties.to_dict().get("timeline")
        if payload.dry_run:
            return {"applied": False, "base_revision_id": revision.revision_id, "changes": changes}
        provenance = ChangeProvenance(ChangeKind.AI, "agent_" + feature, actor=run["actor"],
                                      metadata={"agent_run_id": payload.run_id, "request_id": payload.request_id,
                                                "agent_edit": changes})
        saved = store.save(result, provenance=provenance, expected_revision_id=revision.revision_id,
                           receipt={"id": "agent:" + payload.request_id, "hash": key, "result": {"run_id": payload.run_id}})
    except ProjectConflictError:
        fail(409, "revision_conflict", "工程已被修改，请重新读取")
    except (ValueError, KeyError, TypeError) as error:
        fail(422, "invalid_agent_edit", str(error))
    return {"applied": True, "replayed": False, "revision_id": saved.revision_id,
            "revision_number": saved.revision_number, "changes": changes}


def _check_punctuation(document, operation):
    tokens = {token.token_id: token for token in document.display_tokens}
    raw = operation.payload
    pairs = []
    if operation.type == "replace":
        if raw.get("token_id") not in tokens:
            fail(422, "unknown_token", "词元不存在")
        pairs.append((tokens[raw["token_id"]].text, raw.get("text", "")))
    elif operation.type == "batch_replace":
        pairs = [(tokens[item["token_id"]].text, item.get("text", "")) for item in raw.get("replacements", [])]
    elif operation.type == "retime_text":
        pairs.append((" ".join(tokens[tid].text for tid in raw.get("token_ids", []) if tokens[tid].state is EntityState.ACTIVE), raw.get("text", "")))
    for before, after in pairs:
        if "".join(c.casefold() for c in before if c.isalnum()) != "".join(c.casefold() for c in after if c.isalnum()):
            fail(422, "punctuation_changes_words", "标点操作只能修改标点和大小写")


class TranslationWrite(StrictModel):
    request_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)
    expected_revision_id: str = Field(min_length=1, max_length=160)
    replace_cue_ids: list[str] = Field(min_length=1, max_length=2000)
    units: list[dict[str, Any]] = Field(max_length=2000)
    reason: str = Field(default="", max_length=4000)
    dry_run: bool = False


@router.get("/projects/{project_id}/translation-units")
def read_units(project_id: str):
    revision = latest(project_id)
    return {"revision_id": revision.revision_id, "units": target_events(revision.document)}


@router.put("/projects/{project_id}/translation-units")
def write_units(project_id: str, payload: TranslationWrite):
    return apply(project_id, AgentApply(
        **payload.model_dump(exclude={"replace_cue_ids", "units"}),
        operations=[AgentOperation(type="set_target_units", payload={
            "replace_cue_ids": payload.replace_cue_ids, "units": payload.units,
        })]))


class CutWrite(StrictModel):
    request_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)
    expected_revision_id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=200)
    plan_id: str | None = None
    actions: list[dict[str, Any]] = Field(max_length=1000)
    summary: str = Field(default="", max_length=4000)
    dry_run: bool = False


@router.put("/projects/{project_id}/cut-plan")
def write_cut(project_id: str, payload: CutWrite):
    from substar_core.cut_timeline import ai_plan_from_ranges
    # Deterministic reconstruction lets apply() find its atomic receipt first.
    try:
        revision = api().open_project_store(project_id).load_revision(payload.expected_revision_id)
    except (ProjectStoreError, ValueError, KeyError):
        fail(409, "revision_conflict", "剪辑依据版本不存在，请重新读取")
    timeline = json.loads(revision.document.properties.timeline_json or "{}")
    _, duration = media_data(project_id, revision.document)
    existing = next((p for p in timeline.get("plans", []) if p["id"] == (payload.plan_id or timeline.get("active_plan_id"))), None)
    if payload.plan_id and existing is None:
        fail(422, "unknown_plan", "剪辑依据方案不存在")
    try:
        plan = ai_plan_from_ranges({"operations": payload.actions, "name": payload.name, "summary": payload.summary},
                                   payload.summary or payload.name, duration, existing)
        plan.update(name=payload.name, summary=payload.summary)
        plan["id"] = "agent_plan_" + digest([project_id, payload.request_id])[:24]
        for index, cut in enumerate(plan["cuts"]):
            cut["id"] = "agent_cut_" + digest([plan["id"], index])[:24]
        plan["ai_changes"] = [{"start": row["start"], "end": row["end"], "reason": row.get("reason", "")}
                              for row in payload.actions if row.get("action") == "remove"]
        # Preserve source-clock cuts while also materializing MAW's display gaps.
        gaps = [{"start": round(c["start"]*1000), "end": round(c["end"]*1000),
                 "removed": True, "source": "agent", "origins": ["ai_cleanup"]}
                for c in plan["cuts"] if c.get("enabled", True)]
        plan["gap_remove"] = {"schema": "moy.asr.gap_remove.v1", "gaps": gaps, "skip_playback": True}
        timeline = dict(timeline)
        timeline["plans"] = [*timeline.get("plans", []), plan]
        timeline["active_plan_id"] = plan["id"]
        timeline["gap_remove"] = plan["gap_remove"]
        timeline = validate_timeline(timeline)
    except (ValueError, KeyError, TypeError) as error:
        fail(422, "invalid_cut", str(error))
    return apply(project_id, AgentApply(
        request_id=payload.request_id, run_id=payload.run_id, expected_revision_id=payload.expected_revision_id,
        reason=payload.summary, dry_run=payload.dry_run,
        operations=[AgentOperation(type="set_timeline", payload={
            "timeline": timeline, "expected_timeline": revision.document.properties.to_dict().get("timeline"),
        })]))


@router.get("/projects/{project_id}/evidence")
def evidence(project_id: str, revision_id: str, kind: Literal["audio", "frame"],
             start: float = Query(ge=0, allow_inf_nan=False), end: float | None = Query(None, allow_inf_nan=False)):
    revision = latest(project_id, revision_id)
    source, duration = media_data(project_id, revision.document)
    if source is None:
        fail(404, "media_missing", "项目媒体不可用")
    if start >= duration or (kind == "audio" and (end is None or end <= start or end > duration or end-start > 120)):
        fail(422, "invalid_evidence_range", "音频范围须在原片内且不超过120秒")
    root = api().project_job_path(project_id) / "agent-evidence"
    root.mkdir(exist_ok=True)
    key = digest([str(source), source.stat().st_size, source.stat().st_mtime_ns, kind, start, end])
    output = root / (key + (".wav" if kind == "audio" else ".jpg"))
    if not output.exists():
        with tempfile.TemporaryDirectory(dir=root) as directory:
            pending = Path(directory) / output.name
            args = [executable("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", str(start), "-i", str(source)]
            if kind == "audio":
                args += ["-t", str(end-start), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]
            else:
                args += ["-frames:v", "1", "-vf", "scale=1280:-2"]
            try:
                subprocess.run([*args, str(pending)], capture_output=True, check=True, timeout=180,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                pending.replace(output)
            except (OSError, subprocess.SubprocessError) as error:
                fail(422, "evidence_failed", str(error))
    return FileResponse(output, media_type="audio/wav" if kind == "audio" else "image/jpeg",
                        headers={"X-Substar-Source-Start": str(start), "X-Substar-Revision": revision_id})

class Registration(StrictModel):
    request_id: str = Field(min_length=1, max_length=160)
    media_path: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=120)
    source_srt: str | None = Field(default=None, max_length=4_000_000)
    document: dict[str, Any] | None = None
    source_language: str = "en"
    target_language: str = "zh-CN"


@router.post("/projects", status_code=201)
def register_project(payload: Registration):
    from .application.subtitle_project_import import build_subtitle_document
    from .subtitle_api import make_preview
    from substar_core.media_reference import write_reference
    from substar_core.task_info import SOURCE_LANGUAGES, TARGET_LANGUAGES
    if bool(payload.source_srt) == bool(payload.document):
        fail(422, "registration_input", "请提供 source_srt 或完整 document")
    if payload.source_language not in SOURCE_LANGUAGES or payload.target_language not in TARGET_LANGUAGES:
        fail(422, "registration_language", "不支持的语言")
    source = Path(payload.media_path).expanduser()
    if not source.is_absolute() or not source.is_file():
        fail(422, "media_missing", "媒体路径必须是已存在的绝对文件路径")
    source = source.resolve()
    key = digest(payload.model_dump())
    project_id = "agent_" + digest(payload.request_id)[:24]
    root = api()._projects_root().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / project_id
    assert destination.parent == root
    with _lock:
        if destination.exists():
            receipt = destination / "agent-registration.json"
            if not receipt.exists() or json.loads(receipt.read_text(encoding="utf-8"))["request_hash"] != key:
                fail(409, "registration_reused", "request_id 已注册其他项目")
            revision = latest(project_id)
            return {"project_id": project_id, "revision_id": revision.revision_id,
                    "editor_url": editor_link(project_id), "replayed": True}
        try:
            duration, _, audio = inspect_media(source)
            if not audio:
                fail(422, "no_audio", "媒体没有音轨")
            if payload.document:
                document = EditorDocument.from_dict(payload.document)
                from substar_core.translation_units import validate_units
                validate_units(document, all_units(document))
            else:
                preview = make_preview(payload.source_srt, None, "single", "|", "source")
                if not preview["can_import"]:
                    fail(422, "invalid_subtitles", "字幕无法导入，请检查 SRT")
                document = build_subtitle_document(preview, project_id)
            if any(cue.end > duration + .12 for cue in document.cues):
                fail(422, "subtitle_duration", "字幕超出媒体时长")
            with tempfile.TemporaryDirectory(prefix=".agent-register-", dir=root) as directory:
                staging = Path(directory)
                write_reference(staging, source)
                (staging / "input").mkdir()
                if payload.source_srt:
                    (staging / "input" / "source.srt").write_text(payload.source_srt, encoding="utf-8")
                subprocess.run(
                    [executable("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
                     "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                     str(staging / "audio_16k_mono.wav")],
                    check=True, capture_output=True, timeout=600,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                atomic_write_json(staging / "run_manifest.json", {
                    "schema_version": "substar.run-manifest.v1", "input_mode": "subtitle",
                    "source_file": str(source), "media": {"duration_seconds": duration},
                })
                api().save_task_info(staging, project_id, {
                    "display_name": payload.name, "language": payload.source_language,
                    "target_language_mode": payload.target_language, "preserve_punctuation": True,
                    "source_hard_limit": 55, "target_hard_limit": 20,
                })
                atomic_write_json(staging / "agent-registration.json", {
                    "request_id": payload.request_id, "request_hash": key,
                })
                store = ProjectStore.create(staging / "project", project_id=project_id)
                revision = store.save(document, provenance=ChangeProvenance(ChangeKind.IMPORT, "agent_registration"))
                store.clear_memory_cache(staging / "project")
                staging.rename(destination)
        except HTTPException:
            raise
        except (ValueError, KeyError, OSError, subprocess.SubprocessError, ProjectStoreError) as error:
            fail(422, "registration_failed", str(error))
    return {"project_id": project_id, "revision_id": revision.revision_id,
            "editor_url": editor_link(project_id), "replayed": False}


class Suggestion(StrictModel):
    cue_id: str
    track: Literal["source", "target"] = "source"
    before: str
    after: str
    problem: str = Field(min_length=1, max_length=4000)
    advice: str = Field(default="", max_length=4000)
    group_id: str = Field(default="", max_length=160)


class SuggestionsWrite(StrictModel):
    request_id: str = Field(min_length=1, max_length=160)
    expected_revision_id: str = Field(min_length=1, max_length=160)
    expected_version: int = Field(ge=1)
    items: list[Suggestion] = Field(min_length=1, max_length=200)


@router.post("/projects/{project_id}/runs/{run_id}/suggestions")
def suggestions(project_id: str, run_id: str, payload: SuggestionsWrite):
    key = digest(payload.model_dump())
    def mutate(data):
        run = find_run(data, run_id)
        existing = [item for item in run["suggestions"] if item["request_id"] == payload.request_id]
        if existing:
            if existing[0]["request_hash"] != key:
                fail(409, "request_reused", "建议 request_id 已用于其他内容")
            return run
        if run["version"] != payload.expected_version or run["state"] in TERMINAL:
            fail(409, "run_conflict", "任务已变化，请刷新")
        revision = latest(project_id, payload.expected_revision_id)
        cues = {cue.cue_id: cue for cue in revision.document.cues if cue.state is EntityState.ACTIVE}
        tokens = {token.token_id: token for token in revision.document.display_tokens}
        for index, item in enumerate(payload.items):
            cue = cues.get(item.cue_id)
            if cue is None:
                fail(422, "suggestion_anchor", "建议必须对应有效 Cue")
            current = source_text(cue, tokens) if item.track == "source" else (cue.target.target_text if cue.target else "")
            if current != item.before:
                fail(409, "suggestion_source_changed", "建议原文与当前字幕不一致")
            if not item.after.strip():
                fail(422, "empty_suggestion", "建议文字不能为空")
            run["suggestions"].append({
                **item.model_dump(), "suggestion_id": "suggestion_" + digest([run_id, payload.request_id, index])[:24],
                "request_id": payload.request_id, "request_hash": key,
                "base_revision_id": revision.revision_id, "status": "pending", "comments": [],
                "cue": cue.index + 1, "editor_url": editor_link(project_id, cue.cue_id, cue.index + 1),
            })
        run.update(version=run["version"] + 1, updated_at=now(), state="waiting")
        return run
    return update_runs(project_id, mutate)


class SuggestionDecision(StrictModel):
    request_id: str = Field(min_length=1, max_length=160)
    expected_version: int = Field(ge=1)
    expected_revision_id: str = Field(min_length=1, max_length=160)
    action: Literal["accept", "reject", "comment"]
    comment: str = Field(default="", max_length=4000)


@router.post("/projects/{project_id}/runs/{run_id}/suggestions/{suggestion_id}/decision")
def decide(project_id: str, run_id: str, suggestion_id: str, payload: SuggestionDecision):
    with _lock:
        data = read_runs(project_id)
        run = find_run(data, run_id)
        item = next((item for item in run["suggestions"] if item["suggestion_id"] == suggestion_id), None)
        if item is None:
            fail(404, "suggestion_not_found", "建议不存在")
        decision_key = digest([suggestion_id, payload.model_dump()])
        prior = run.get("decision_receipts", {}).get(payload.request_id)
        if prior:
            if prior["hash"] != decision_key:
                fail(409, "decision_reused", "决定 request_id 已用于不同内容")
            return deepcopy(prior["result"])
        if item.get("decision_request_id") == payload.request_id:
            if item.get("decision_hash") != digest(payload.model_dump()):
                fail(409, "decision_reused", "决定 request_id 已用于不同内容")
            return {**item, "run_version": run["version"]}
        # A committed acceptance is authoritative even if the sidecar update was interrupted.
        key = digest([run_id, suggestion_id, payload.request_id])
        acceptance_id = "accept_" + key
        if run["version"] != payload.expected_version:
            fail(409, "run_conflict", "审阅记录已变化，请刷新")
        if run["state"] in TERMINAL:
            fail(409, "run_terminal", "任务已经结束")
        if item["status"] == "accepted" and payload.action != "comment":
            fail(409, "suggestion_decided", "建议已执行；撤销请使用版本恢复")
        saved = None
        if payload.action == "accept":
            # Reconstruct from immutable base so an interrupted sidecar write
            # can recover the already committed document acceptance receipt.
            try:
                revision = api().open_project_store(project_id).load_revision(payload.expected_revision_id)
            except (ProjectStoreError, ValueError, KeyError):
                fail(409, "revision_conflict", "建议依据版本不存在，请重新审阅")
            cue = next((cue for cue in revision.document.cues if cue.cue_id == item["cue_id"]
                        and cue.state is EntityState.ACTIVE), None)
            if cue is None:
                fail(409, "suggestion_anchor_changed", "原 Cue 已变化，请重新审阅")
            if item["track"] == "target":
                current = cue.target.target_text if cue.target else ""
                operation = AgentOperation(type="set_target", payload={
                    "cue_id": cue.cue_id, "expected_target_text": item["before"], "target_text": item["after"],
                })
            elif cue.source_text is not None:
                current = cue.source_text
                operation = AgentOperation(type="set_source_text", payload={"cue_id": cue.cue_id, "text": item["after"]})
            else:
                current = source_text(cue, {t.token_id:t for t in revision.document.display_tokens})
                operation = AgentOperation(type="retime_text", payload={
                    "cue_id": cue.cue_id, "token_ids": list(cue.display_token_ids), "text": item["after"],
                })
            if current != item["before"]:
                fail(409, "suggestion_source_changed", "此条字幕已被修改，请刷新建议")
            saved = apply(project_id, AgentApply(
                request_id=acceptance_id, run_id=run_id, expected_revision_id=revision.revision_id,
                operations=[operation], reason=item["problem"],
            ))
        if payload.action != "comment":
            item["status"] = {"accept":"accepted", "reject":"rejected"}[payload.action]
        item["decision_request_id"] = payload.request_id
        item["decision_hash"] = digest(payload.model_dump())
        if payload.comment:
            item["comments"].append({"text":payload.comment, "created_at":now()})
        if saved:
            item["result_revision_id"] = saved["revision_id"]
        run.update(version=run["version"]+1, updated_at=now())
        result = {**deepcopy(item), "run_version": run["version"]}
        run.setdefault("decision_receipts", {})[payload.request_id] = {"hash": decision_key, "result": result}
        data["version"] += 1
        atomic_write_json(run_path(project_id), data)
        return result


# The project URL is the identity. Navigation is ephemeral UI state, never a document edit.
_project_navigation: dict[str, dict] = {}
NAVIGATION_VIEWER_TTL = 90


def navigation_channel(project_id):
    channel = _project_navigation.setdefault(project_id, {"command": None, "viewers": {}})
    cutoff = time.monotonic() - NAVIGATION_VIEWER_TTL
    channel["viewers"] = {key: value for key, value in channel["viewers"].items() if value["seen"] >= cutoff}
    return channel


def navigation_projection(channel):
    command = channel["command"]
    current = command["navigation_id"] if command else None
    return {"navigation": deepcopy(command), "online_pages": len(channel["viewers"]),
            "located_pages": sum(bool(current and item.get("ack_id") == current and not item.get("ack_error"))
                                 for item in channel["viewers"].values()),
            "failed_pages": sum(bool(current and item.get("ack_id") == current and item.get("ack_error"))
                                for item in channel["viewers"].values())}


class NavigationPoll(StrictModel):
    viewer_id: str = Field(min_length=1, max_length=80)
    ack_id: str | None = Field(default=None, max_length=80)
    ack_error: str = Field(default="", max_length=500)
    closed: bool = False


@router.post("/projects/{project_id}/navigation/poll")
def poll_navigation(project_id: str, payload: NavigationPoll):
    if not api().open_project_store(project_id).load_summary():
        fail(404, "empty_project", "项目不存在或没有文稿")
    with _lock:
        channel = navigation_channel(project_id)
        if payload.closed:
            channel["viewers"].pop(payload.viewer_id, None)
        else:
            channel["viewers"][payload.viewer_id] = {
                "seen": time.monotonic(), "ack_id": payload.ack_id, "ack_error": payload.ack_error}
        return navigation_projection(channel)


@router.get("/projects/{project_id}/navigation")
def get_navigation(project_id: str):
    if not api().open_project_store(project_id).load_summary():
        fail(404, "empty_project", "项目不存在或没有文稿")
    with _lock:
        return navigation_projection(navigation_channel(project_id))


class NavigationLinks(StrictModel):
    cue_ids: list[str] = Field(min_length=1, max_length=100)


@router.post("/projects/{project_id}/runs/{run_id}/navigation-links")
def navigation_links(project_id: str, run_id: str, payload: NavigationLinks):
    with _lock:
        find_run(read_runs(project_id), run_id)
        cues = {cue.cue_id: cue for cue in latest(project_id).document.cues}
        if any(cid not in cues for cid in payload.cue_ids):
            fail(404, "cue_not_found", "Cue 已不存在，请读取最新版本")
        base = "/api/agent/projects/" + project_id
        return {"project_id": project_id, "links": [
            {"cue_id": cid, "cue": cues[cid].index + 1,
             "navigation_url": base + "/navigate?" + urlencode({"cue_id": cid}),
             "status_url": base + "/navigation"} for cid in dict.fromkeys(payload.cue_ids)]}


def navigation_handoff_page(command: dict) -> HTMLResponse:
    project = escape(command["project_id"], quote=True)
    navigation_id = escape(command["navigation_id"], quote=True)
    editor_url = escape(command["editor_url"], quote=True)
    cue = command["cue"]
    return HTMLResponse(
        f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Substar · 定位 Cue {cue}</title>
<main data-project="{project}" data-navigation-id="{navigation_id}">
  <p id="navigationStatus" role="status">正在定位 Cue {cue}…</p>
  <a id="openEditor" href="{editor_url}">打开 Substar 编辑器</a>
</main>
<script src="/assets/agent_navigation_handoff.js?v=1" defer></script>
</html>""",
        headers={"Cache-Control": "no-store", "Vary": "Accept, Sec-Fetch-Mode"},
    )


@router.get(
    "/projects/{project_id}/navigate",
    status_code=204,
    response_class=Response,
    responses={200: {"description": "Browser navigation handoff", "content": {"text/html": {}}}},
)
def navigate_project(project_id: str, request: Request, cue_id: str = Query(min_length=1, max_length=160)):
    purpose = request.headers.get("sec-purpose", "") + request.headers.get("purpose", "")
    if "prefetch" in purpose.lower() or "preview" in purpose.lower():
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    cue = next((c for c in latest(project_id).document.cues if c.cue_id == cue_id), None)
    if cue is None:
        fail(404, "cue_not_found", "Cue 已不存在，请读取最新版本")
    command = {
        "navigation_id": uuid.uuid4().hex, "project_id": project_id, "cue_id": cue.cue_id,
        "cue": cue.index + 1, "editor_url": editor_link(project_id, cue.cue_id, cue.index + 1),
        "created_at": now(),
    }
    with _lock:
        navigation_channel(project_id)["command"] = command
    fetch_mode = request.headers.get("sec-fetch-mode")
    if fetch_mode in (None, "navigate") and "text/html" in request.headers.get("accept", ""):
        return navigation_handoff_page(command)
    return Response(status_code=204, headers={"Cache-Control": "no-store", "Vary": "Accept, Sec-Fetch-Mode"})
