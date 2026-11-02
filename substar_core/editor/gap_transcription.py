"""Durable range ASR with a single reversible, revision-checked publication."""
from __future__ import annotations

from datetime import datetime, timezone
import copy
import hashlib
import json
import math
import re
import subprocess
import threading
import time
import uuid
import wave
from dataclasses import replace
from pathlib import Path

from substar_core.artifacts import atomic_write_json
from substar_core.config import load_settings
from substar_core.document_operations import apply_document_operation
from substar_core.domain import ChangeKind, ChangeProvenance, EntityState
from substar_core.domain.editor_document import DisplayToken, SourceToken, TranslationTrack, stable_id
from substar_core.language_layout import layout_tokens
from substar_core.storage import ProjectStore
from substar_core.transcription.contracts import TRANSCRIPTION_INPUT_SCHEMA, build_transcription_request

_LOCK = threading.RLock()
_RUNNING = set()
_ID = re.compile(r"gap_[a-f0-9]{32}\Z")
TERMINAL = {"succeeded", "failed", "cancelled"}


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path, value):
    atomic_write_json(path, value)


def _identity(media):
    stat = media.stat()
    return [str(media.resolve()), stat.st_size, stat.st_mtime_ns]


def range_signature(document, start, end):
    """Include deleted rows and token contents so local concurrent edits cannot be lost."""
    cues = [cue for cue in document.cues if cue.mapping.get("subtitle_track") != "overlay"
            and cue.start < end - .01 and cue.end > start + .01]
    for cue in cues:
        if cue.state is EntityState.ACTIVE and (cue.start < start - .02 or cue.end > end + .02):
            raise ValueError("空隙边界穿过现有字幕，请先把边界调整到字幕之间再重新听写")
    ids = {tid for cue in cues for tid in cue.display_token_ids}
    value = {"cues":[cue.to_dict() for cue in cues],
             "tokens":[token.to_dict() for token in document.display_tokens if token.token_id in ids],
             "plan_id":document.properties.to_dict().get("timeline", {}).get("active_plan_id")}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _crop(items, start, end):
    """Subtract a half-open range, preserving all data outside it."""
    result = []
    for item in items:
        if item["end"] <= start or item["start"] >= end:
            result.append(item)
            continue
        if item["start"] < start:
            result.append({**item,"end":start})
        if item["end"] > end:
            right = {**item,"start":end}
            if "id" in right:
                right["id"] += "-right"
            result.append(right)
    return result


def restore_gap(timeline, start, end, job_id):
    timeline = copy.deepcopy(timeline or {"markers":[],"plans":[],"active_plan_id":None})
    plan = next((p for p in timeline.get("plans", []) if p["id"] == timeline.get("active_plan_id")), None)
    gap = (plan or {}).get("gap_remove", timeline.get("gap_remove"))
    if gap is not None:
        gap = copy.deepcopy(gap)
        a, b = round(start * 1000), round(end * 1000)
        gap["gaps"] = _crop(gap.get("gaps", []), a, b)
        gap["gaps"].append({"start":a,"end":b,"removed":False,"source":"manual","origins":["manual"]})
        gap["gaps"].sort(key=lambda g:g["start"])
        provenance = gap.setdefault("provenance", {"schema":"moy.asr.gap_provenance.v1","sources":{},"legacy":[]})
        provenance.setdefault("manual_overrides", []).append({"id":job_id,"start":a,"end":b,"removed":False,"source":"manual"})
        timeline["gap_remove"] = gap
        if plan is not None:
            plan["gap_remove"] = copy.deepcopy(gap)
    if plan is not None:
        # Export and playback use separate representations; restore both.
        enabled = [item for item in plan.get("cuts", []) if item.get("enabled", True)]
        disabled = [item for item in plan.get("cuts", []) if not item.get("enabled", True)]
        plan["cuts"] = _crop(enabled, start, end) + disabled
    return timeline


def recognition_segments(evidence, offset, end, limit=55):
    units = evidence.get("units") or []
    if not units:
        raise ValueError("片段未识别到文字，原字幕和空隙保持不变")
    segments, current = [], []
    def flush():
        nonlocal current
        if current:
            segments.append(current)
            current = []
    previous = -1.0
    for unit in units:
        text = str(unit.get("text", "")).strip()
        a, b = float(unit["start"]), float(unit["end"])
        if not text:
            continue
        if not all(math.isfinite(v) for v in (a,b)) or a < 0 or b < a or a < previous - .001:
            raise ValueError("听写词时间无效，未覆写字幕")
        previous = a
        a, b = max(offset, offset+a), min(end,offset+b)
        if a >= end or b <= offset:
            continue
        if b <= a:
            b = min(end,a+.01)
        if b <= a:
            continue
        item = {"text":text,"start":a,"end":b,"speaker_id":unit.get("speaker_id"),"timing_source":unit.get("timing_source","")}
        if current and (unit.get("sentence_start") or len(layout_tokens([v["text"] for v in current]+[text])) > limit):
            flush()
        current.append(item)
        if unit.get("sentence_end"):
            flush()
    flush()
    if not segments:
        raise ValueError("片段未识别到有效文字，原字幕和空隙保持不变")
    if len(segments) > 500:
        raise ValueError("片段听写结果过多，未覆写字幕")
    return segments


def replacement_document(document, segments, start, end, job_id):
    range_signature(document,start,end)
    affected = [cue for cue in document.cues if cue.mapping.get("subtitle_track") != "overlay"
                and cue.start >= start-.02 and cue.end <= end+.02 and cue.start < end-.01 and cue.end > start+.01]
    provenance = ChangeProvenance(kind=ChangeKind.MANUAL, operation="gap_transcription", actor="gap-asr",
                                 metadata={"job_id":job_id,"range_start":start,"range_end":end})
    # Range re-recognition replaces the current entities. Previous source words,
    # translations and rows remain in the parent revision, rather than tombstones.
    removed_cues = {cue.cue_id for cue in affected}
    remaining_cues = tuple(c for c in document.cues if c.cue_id not in removed_cues)
    retained_ids = {tid for c in remaining_cues for tid in c.display_token_ids}
    removed_ids = {tid for c in affected for tid in c.display_token_ids} - retained_ids
    removed_sources = {sid for t in document.display_tokens if t.token_id in removed_ids for sid in t.source_token_ids}
    remaining_groups = {c.group_id for c in remaining_cues}
    result = replace(document,
        source_tokens=tuple(replace(t,index=i) for i,t in enumerate(t for t in document.source_tokens if t.token_id not in removed_sources)),
        display_tokens=tuple(t for t in document.display_tokens if t.token_id not in removed_ids),
        cues=tuple(replace(c,index=i) for i,c in enumerate(remaining_cues)),
        groups=tuple(g for g in document.groups if g.group_id in remaining_groups))
    def operation(kind, payload, key):
        nonlocal result
        result = apply_document_operation(result, {"operation_id":f"{job_id}:{key}","type":kind,
            "payload":{**payload,"provenance":provenance.to_dict()}})
    timeline = restore_gap(document.properties.to_dict().get("timeline"),start,end,job_id)
    timings = timeline.setdefault("word_timings", {})
    for tid in removed_ids:
        timings.pop(tid,None)
    speaker_map = {}
    bilingual = any(c.target is not None for c in document.cues)
    target_language = next((c.target.language for c in affected if c.target is not None),
                           next((c.target.language for c in document.cues if c.target is not None),None))
    for index, words in enumerate(segments):
        a, b = words[0]["start"], max(word["end"] for word in words)
        # A neighboring original subtitle is the strongest local speaker anchor.
        anchor = min(affected or [c for c in document.cues if c.state is EntityState.ACTIVE and c.mapping.get("subtitle_track") != "overlay"],
                     key=lambda c:abs(c.start-a), default=None)
        recognized_speaker = words[0].get("speaker_id")
        if recognized_speaker not in speaker_map:
            speaker_map[recognized_speaker] = anchor.speaker if anchor else None
        speaker = speaker_map[recognized_speaker]
        operation("insert_cue",{"start":a,"end":b,"text":layout_tokens(word["text"] for word in words),"speaker":speaker},f"insert-{index}")
        cid = stable_id("cue",{"operation_id":f"{job_id}:insert-{index}"})
        inserted = next(c for c in result.cues if c.cue_id == cid)
        old_ids = set(inserted.display_token_ids)
        tokens, sources = [], []
        for word_index, word in enumerate(words):
            tid = stable_id("dsp",{"gap_asr":job_id,"cue":index,"word":word_index})
            sid = stable_id("src",{"gap_asr":job_id,"cue":index,"word":word_index})
            sources.append(SourceToken(token_id=sid,index=0,text=word["text"],start=word["start"],end=word["end"],speaker=speaker,
                timing={"kind":"native" if "native" in word.get("timing_source","") else "unknown",
                        "provider":word.get("timing_source") or "gap-transcription","job_id":job_id}))
            tokens.append(DisplayToken(token_id=tid,text=word["text"],original_text=word["text"],source_token_ids=(sid,),provenance=provenance))
            timings[tid] = {"start":round(word["start"]*1000),"end":round(word["end"]*1000)}
        blank_target = TranslationTrack(target_text="",provenance=provenance,language=target_language,
            translation_status="manual_required",issue_code="translation_unresolved") if bilingual else None
        result = replace(result,
            source_tokens=tuple(replace(t,index=i) for i,t in enumerate(sorted((*result.source_tokens,*sources),key=lambda t:t.start))),
            display_tokens=tuple(t for t in result.display_tokens if t.token_id not in old_ids)+tuple(tokens),
            cues=tuple(replace(c,display_token_ids=tuple(t.token_id for t in tokens),target=blank_target) if c.cue_id==cid else c for c in result.cues))
    expected_timeline = result.properties.to_dict().get("timeline")
    operation("set_timeline",{"timeline":timeline,"expected_timeline":expected_timeline},"restore-gap")
    result.validate()
    before_sources = {t.token_id:t for t in document.source_tokens if t.token_id not in removed_sources}
    for token in result.source_tokens:
        if token.token_id in before_sources and replace(token,index=before_sources[token.token_id].index) != before_sources[token.token_id]:
            raise ValueError("听写覆写改变了范围外的原始词记录")
    return result, provenance, len(affected)


def _clip(project, media, destination, start, end):
    # Use the original media, rather than a possibly stale previously linked PCM.
    from substar_core.environment_doctor import _find_tool
    command = [_find_tool("ffmpeg") or "ffmpeg","-nostdin","-hide_banner","-loglevel","error","-y",
               "-ss",str(start),"-i",str(media),"-t",str(end-start),"-vn","-ac","1","-ar","16000",str(destination)]
    result = subprocess.run(command,capture_output=True,timeout=120,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    if result.returncode:
        raise ValueError("片段音频提取失败，请检查原媒体是否可读取")
    with wave.open(str(destination)) as audio:
        duration = audio.getnframes()/audio.getframerate()
    if duration < .05 or duration < end-start-.1:
        raise ValueError("片段超出媒体时长或没有可用音频")


def status(project, job_id, service):
    if not _ID.fullmatch(job_id):
        raise ValueError("无效的片段听写任务")
    record = _read(project/"gap-transcriptions"/job_id/"request.json")
    result = {key:record[key] for key in ("job_id","state","start","end","message") if key in record}
    result.update({key:record[key] for key in ("result_revision_id","cue_count","replaced_cue_count") if key in record})
    if record.get("task_id") and record["state"] not in TERMINAL and record.get("phase") != "publishing":
        task = service.get_task(record["task_id"])
        result["progress"] = task.get("progress",0)
        result["message"] = task.get("progress_message") or "正在听写片段…"
    return result


def start(project, media, start, end, expected_revision_id, service):
    if not all(math.isfinite(v) for v in (start,end)) or start < 0 or end-start < .05 or end-start > 300:
        raise ValueError("重新听写范围需为0.05秒至5分钟")
    store = ProjectStore.open(project/"project")
    latest = store.load_latest()
    if latest.revision_id != expected_revision_id:
        raise ValueError("字幕已变化，请刷新后重新听写")
    signature = range_signature(latest.document,start,end)
    with _LOCK:
        for path in (project/"gap-transcriptions").glob("gap_*/request.json"):
            if _read(path)["state"] not in TERMINAL:
                raise ValueError("此项目已有片段正在听写，请等待完成或取消")
        jid = "gap_"+uuid.uuid4().hex
        folder = project/"gap-transcriptions"/jid
        folder.mkdir(parents=True)
        _write(folder/"before-revision.json",latest.to_dict())
        record = {"created_at":datetime.now(timezone.utc).isoformat(),"job_id":jid,"state":"preparing","start":start,"end":end,"signature":signature,
                  "message":"正在提取片段音频…","media_identity":_identity(media),"media_path":str(media)}
        _write(folder/"request.json",record)
        _launch(project,folder,service)
    return status(project,jid,service)


def cancel(project, job_id, service):
    if not _ID.fullmatch(job_id):
        raise ValueError("无效的片段听写任务")
    path = project/"gap-transcriptions"/job_id/"request.json"
    with _LOCK:
        record = _read(path)
        if record["state"] not in TERMINAL:
            record.update(state="cancelled",message="已取消，字幕未覆写")
            _write(path,record)
            if record.get("task_id"):
                service.request_cancel(record["task_id"])
    return status(project,job_id,service)


def _launch(project, folder, service):
    key = str(folder.resolve())
    with _LOCK:
        if key in _RUNNING:
            return
        _RUNNING.add(key)
    threading.Thread(target=_run,args=(project,folder,service,key),daemon=True,name="gap-transcription").start()


def _run(project, folder, service, key):
    path = folder/"request.json"
    try:
        record = _read(path)
        if record["state"] in TERMINAL:
            return
        jid, a, b = record["job_id"],record["start"],record["end"]
        store = ProjectStore.open(project/"project")
        # Reconcile a crash between atomic publication and status persistence.
        receipt = store.find_receipt("gap-asr:"+jid,jid)
        if receipt is not None:
            record.update(state="succeeded",result_revision_id=receipt.revision_id,message="片段听写已覆写，可撤销")
            _write(path,record)
            return
        scratch = project.parent/("gap-asr-"+jid[4:])
        if not record.get("task_id"):
            (scratch/"input").mkdir(parents=True,exist_ok=True)
            clip = scratch/"input/clip.wav"
            _clip(project,Path(record["media_path"]),clip,a,b)
            original = _read(project/"recognition_request.json") if (project/"recognition_request.json").exists() else {}
            settings = {**load_settings(include_secret=False),**original.get("options",{})}
            profile = original.get("profile_id",settings.get("recognition_profile_id","qwen_cloud"))
            request = build_transcription_request(media_path=clip,project_directory=scratch,profile_id=profile,
                language=original.get("language",settings.get("language","Auto")),prompt=original.get("prompt",""),
                hotwords={h["text"]:h["weight"] for h in original.get("hotwords",[])},settings=settings)
            with _LOCK:
                if _read(path)["state"] == "cancelled":
                    return
                task = service.create_task(task_type="transcription",input_schema=TRANSCRIPTION_INPUT_SCHEMA,
                    input_payload=request,project_id=scratch.name,idempotency_key="gap-asr:"+jid)
                record.update(state="running",task_id=task["task_id"],message="正在听写片段…")
                _write(path,record)
        while True:
            with _LOCK:
                record = _read(path)
                if record["state"] == "cancelled":
                    return
            task = service.get_task(record["task_id"])
            if task["state"] in {"succeeded","succeeded_with_issues"}:
                break
            if task["state"] == "interrupted":
                service.retry(task["task_id"])
                continue
            if task["state"] in {"failed","cancelled"}:
                raise ValueError((task.get("error") or {}).get("message") or "片段听写未完成，原字幕未改动；可重新尝试")
            time.sleep(.5)
        evidence = _read(scratch/"recognition_evidence.json")
        with _LOCK:
            current = _read(path)
            if current["state"] == "cancelled":
                return
            current.update(message="正在生成字幕并保存…", phase="publishing")
            _write(path,current)
        segments = recognition_segments(evidence,a,b)
        with _LOCK:
            record = _read(path)
            if record["state"] == "cancelled":
                return
            if _identity(Path(record["media_path"])) != record["media_identity"]:
                raise ValueError("原媒体已变化，未覆写字幕")
            latest = store.load_latest()
            if range_signature(latest.document,a,b) != record["signature"]:
                raise ValueError("听写期间此范围内的字幕或剪辑方案已变化，未覆写；请重新听写")
            result,provenance,replaced = replacement_document(latest.document,segments,a,b,jid)
            summary = {"job_id":jid,"cue_count":len(segments),"replaced_cue_count":replaced}
            saved = store.save(result,provenance=provenance,expected_revision_id=latest.revision_id,
                               receipt={"id":"gap-asr:"+jid,"hash":jid,"result":summary})
            record.update(state="succeeded",result_revision_id=saved.revision_id,cue_count=len(segments),
                          replaced_cue_count=replaced,message=f"已覆写片段，生成{len(segments)}条字幕，可撤销")
            _write(path,record)
    except Exception as exc:
        with _LOCK:
            record = _read(path)
            if record["state"] != "cancelled":
                record.update(state="failed",message=str(exc)[:500])
                _write(path,record)
    finally:
        with _LOCK:
            _RUNNING.discard(key)


def recover_pending(service, projects_root):
    for path in projects_root.glob("*/gap-transcriptions/gap_*/request.json"):
        try:
            if _read(path)["state"] not in TERMINAL:
                _launch(path.parents[2],path.parent,service)
        except (OSError,ValueError,KeyError):
            continue


def task_projection(project, service):
    paths = sorted((project / "gap-transcriptions").glob("gap_*/request.json"), key=lambda p:p.stat().st_mtime, reverse=True)
    if not paths:
        return None
    record = _read(paths[0])
    row = status(project, record["job_id"], service)
    return {**row, "task_id":row["job_id"], "kind":"gap_transcription",
            "state":"running" if row["state"] == "preparing" else row["state"],
            "created_at":record.get("created_at", ""),
            "progress":1 if row["state"] == "succeeded" else min(.95, float(row.get("progress", 0))*.9),
            "display_error":row["message"] if row["state"] == "failed" else None}
