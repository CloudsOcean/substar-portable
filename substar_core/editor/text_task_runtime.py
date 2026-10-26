"""Durable Runtime adapters for the two narrow subtitle tasks."""
from __future__ import annotations

import hashlib
import os
import sys
import threading
from pathlib import Path

from substar_core.editor.text_tasks import INPUT_SCHEMA, RESULT_SCHEMA, compute
from substar_core.editor.application.publication import publish_candidate
from substar_core.process_command import python_script_command
from substar_core.runtime.model import InvalidTaskError
from substar_core.runtime.registry import TaskHandler, WorkerLaunch
from substar_core.runtime.worker_protocol import WorkerMessage, WorkerMessageType, WorkerControl, credential_environment_key, parse_command_line, utc_now
from substar_core.credential_store import model_provider_credential_ref
from substar_core.storage import ProjectStore


def validate_input(payload):
    required = {"schema_version","expected_revision_id","provider_id","credential_ref","settings","optimize_boundaries"}
    if not isinstance(payload, dict) or set(payload) != required or payload.get("schema_version") != INPUT_SCHEMA:
        raise InvalidTaskError("invalid subtitle text task input")
    if not isinstance(payload["settings"], dict) or not isinstance(payload["optimize_boundaries"], bool):
        raise InvalidTaskError("invalid subtitle text task settings")
    if payload["credential_ref"] != model_provider_credential_ref(payload["provider_id"]):
        raise InvalidTaskError("subtitle task credential mismatch")
    return dict(payload)


def build_text_task_handler(kind, projects_root, application_root):
    def prepare(context):
        payload = validate_input(dict(context.input_payload))
        project = (projects_root / str(context.task["project_id"])).resolve()
        if projects_root.resolve() not in project.parents:
            raise InvalidTaskError("invalid project")
        latest = ProjectStore.open(project / "project").load_latest()
        if latest is None or latest.revision_id != payload["expected_revision_id"]:
            raise InvalidTaskError("subtitle source revision changed")
        return WorkerLaunch(argv=tuple(python_script_command("scripts/run_text_task_worker.py")),cwd=application_root,
                            project_root=project,worker_input=payload,credential_refs=(payload["credential_ref"],),
                            timeout_seconds=float(payload["settings"].get("stage_timeout_seconds",3600)))
    def progress(context, message):
        return {"progress":float(message.progress or 0),"step":message.step,"message":message.data.get("message",""),
                "phase":"delivery" if message.data.get("phase") == "materializing" else "primary",
                "completed_units":int(message.data.get("completed",0)),"total_units":int(message.data.get("total",0)),
                "progress_payload":dict(message.data.get("ai_progress") or {})}
    def finalize(context, completion):
        value = completion.result
        if not isinstance(value, dict) or value.get("schema_version") != RESULT_SCHEMA:
            raise InvalidTaskError("invalid subtitle task result")
        summary = dict(value["summary"])
        project = projects_root / str(context.task["project_id"])
        publish_candidate(ProjectStore.open(project / "project"),context.artifact_directory,
                          task_id=context.task["task_id"],expected_revision_id=context.input_payload["expected_revision_id"],summary=summary)
        return summary
    return TaskHandler(task_type=kind,validate_input=validate_input,prepare=prepare,handle_worker_event=progress,
                       finalize=finalize,resources=("worker","provider_io"))


def main():
    for stream in (sys.stdin,sys.stdout,sys.stderr):
        if hasattr(stream,"reconfigure"):
            stream.reconfigure(encoding="utf-8",errors="replace")
    command = parse_command_line(sys.stdin.readline())
    kind = command.task_type.value
    if kind not in {"organize","punctuation"} or command.input_schema != INPUT_SCHEMA:
        return 90
    payload = validate_input(dict(command.input))
    credential = os.environ.pop(credential_environment_key(payload["credential_ref"]),"").strip()
    if not credential:
        raise ValueError("模型凭据不可用")
    payload["settings"]["translation_api_key"] = credential
    directory = Path(command.paths.artifact_directory)
    directory.mkdir(parents=True,exist_ok=True)
    sequence = 0
    lock = threading.Lock()
    def emit(message_type,data,**kwargs):
        nonlocal sequence
        with lock:
            sequence += 1
            sys.stdout.write(WorkerMessage(task_id=command.task_id,attempt=command.attempt,sequence=sequence,
                message_type=message_type,occurred_at=utc_now(),data=data,**kwargs).to_json_line())
            sys.stdout.flush()
    emit(WorkerMessageType.READY,{"worker":kind})
    cancel_event = threading.Event()
    from substar_core.model_concurrency import set_cancel_event
    set_cancel_event(cancel_event)
    def controls():
        for line in sys.stdin:
            try:
                control = WorkerControl.from_json_line(line)
                if control.task_id == command.task_id and control.attempt == command.attempt:
                    cancel_event.set()
            except ValueError:
                continue
    threading.Thread(target=controls,daemon=True).start()
    def progress(phase,done,total):
        labels={"planning":"优化请求边界" if payload["optimize_boundaries"] else "准备请求块",
                "executing":"AI整理中" if kind == "organize" else "AI标点中",
                "validating":"校验结果","materializing":"保存候选版本"}
        fraction = done / total if total else 0
        value = .03+.12*fraction if phase == "planning" else .15+.7*fraction if phase == "executing" else .9 if phase == "validating" else .96
        emit(WorkerMessageType.PROGRESS,{"phase":phase,"completed":done,"total":total,"message":labels[phase],
            "ai_progress":{"kind":kind,"phase":phase,"unit_label":"块","unit_kind":f"{kind}_block",
                           "units":{"planned":total,"completed":done}}},progress=value,step=f"{kind}.{phase}")
    try:
        summary = compute(kind,Path(command.paths.project_root),directory,payload,command.task_id,progress,cancel_event=cancel_event)
        rows=[]
        for name,artifact_type,schema in [("candidate.json","editor_candidate","substar.editor-candidate.v1"),("result.json","text_task_result",RESULT_SCHEMA)]:
            path=directory/name
            row={"artifact_type":artifact_type,"relative_path":name,"schema_version":schema,
                 "sha256":hashlib.sha256(path.read_bytes()).hexdigest(),"byte_size":path.stat().st_size}
            rows.append(row)
            emit(WorkerMessageType.ARTIFACT,row)
        emit(WorkerMessageType.RESULT,{"schema_version":RESULT_SCHEMA,"summary":summary,"artifacts":rows})
        return 0
    except InterruptedError:
        emit(WorkerMessageType.CANCELLED,{"reason":"user_requested"})
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}",file=sys.stderr,flush=True)
        emit(WorkerMessageType.ERROR,{"code":f"{kind}_worker_failed","public_message":str(exc)[:1600]})
        return 1
