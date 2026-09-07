from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from substar_core.credential_store import model_provider_credential_ref
from substar_core.process_command import python_script_command
from substar_core.runtime.model import InvalidTaskError
from substar_core.runtime.registry import TaskHandler, TaskWorkContext, WorkerLaunch
from substar_core.runtime.supervisor import WorkerCompletion
from substar_core.runtime.worker_protocol import WorkerMessage
from substar_core.storage import ProjectStore
from .contracts import CALIBRATION_RESULT_SCHEMA


CALIBRATION_INPUT_SCHEMA = "substar.calibration-input.v2"


def validate_calibration_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version", "expected_revision_id", "instruction",
        "provider_id", "credential_ref", "settings",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise InvalidTaskError("calibration task input fields are invalid")
    if payload.get("schema_version") != CALIBRATION_INPUT_SCHEMA:
        raise InvalidTaskError("unsupported calibration input schema")
    if payload["credential_ref"] != model_provider_credential_ref(str(payload["provider_id"])):
        raise InvalidTaskError("calibration credential reference does not match provider")
    if not isinstance(payload["settings"], Mapping):
        raise InvalidTaskError("calibration settings snapshot is invalid")
    return {**dict(payload), "settings": dict(payload["settings"])}


def build_calibration_handler(projects_root: Path, application_root: Path) -> TaskHandler:
    projects_root = projects_root.resolve()
    application_root = application_root.resolve()

    def prepare(context: TaskWorkContext) -> WorkerLaunch:
        payload = validate_calibration_input(context.input_payload)
        project_id = str(context.task.get("project_id") or "")
        project = (projects_root / project_id).resolve()
        if projects_root not in project.parents or not project.is_dir():
            raise InvalidTaskError("calibration project does not exist")
        revision = ProjectStore.open(project / "project").load_latest()
        if revision is None or revision.revision_id != payload["expected_revision_id"]:
            raise InvalidTaskError("calibration source revision changed")
        return WorkerLaunch(
            argv=tuple(python_script_command("scripts/run_calibration_worker.py")),
            cwd=application_root,
            project_root=project,
            worker_input=payload,
            credential_refs=(str(payload["credential_ref"]),),
            timeout_seconds=float(payload["settings"].get("stage_timeout_seconds", 3600)),
        )

    def progress(_context: TaskWorkContext, message: WorkerMessage) -> Mapping[str, Any]:
        phase = str(message.data.get("phase") or "executing")
        labels = {
            "executing": "校准处理中",
            "repair": "修复未通过校准块",
            "validating": "验收校准结果",
            "materializing": "生成可编辑校准版本",
            "publishing": "交付校准版本",
            "completed": "校准完成",
        }
        return {
            "progress": float(message.progress or 0.0),
            "message": labels.get(phase, "校准处理中"),
            "step": str(message.step or f"calibration.{phase}"),
            "wait_reason": None,
            "phase": "repair" if phase == "repair" else (
                "delivery" if phase in {"materializing", "publishing", "completed"} else
                "validation" if phase == "validating" else "primary"
            ),
            "completed_units": int(message.data.get("completed", 0) or 0),
            "total_units": int(message.data.get("total", 0) or 0),
            "progress_payload": dict(message.data.get("ai_progress") or {}),
        }

    def finalize(context: TaskWorkContext, completion: WorkerCompletion) -> Mapping[str, Any]:
        result = completion.result
        if not isinstance(result, Mapping) or result.get("schema_version") != CALIBRATION_RESULT_SCHEMA:
            raise InvalidTaskError("calibration worker result is invalid")
        summary = result.get("summary")
        if not isinstance(summary, Mapping):
            raise InvalidTaskError("calibration worker summary is invalid")
        project = (projects_root / str(context.task["project_id"])).resolve()
        from substar_core.editor.application.publication import publish_candidate
        from substar_core.artifacts import atomic_write_json
        import json
        # All required IO precedes the authoritative transaction.
        for name in ("latest.json", "audit.json"):
            value = json.loads((context.artifact_directory / name).read_text(encoding="utf-8"))
            atomic_write_json(project / "calibration" / name, value)
        problems = list(summary.get("problem_cue_ids") or [])
        problem_blocks = list(summary.get("problem_block_ids") or [])
        failures = list(summary.get("failed_blocks") or [])
        final_result = {
            "result_revision_id": str(summary["result_revision_id"]),
            "problem_cue_ids": problems,
            "problem_block_ids": problem_blocks,
            "failed_blocks": failures,
            "needs_attention": bool(problems or failures),
            "ai_progress": dict(summary.get("ai_progress") or {}),
        }

        revision = publish_candidate(ProjectStore.open(project / "project"), context.artifact_directory,
            task_id=str(context.task["task_id"]),
            expected_revision_id=str(context.input_payload["expected_revision_id"]), summary=final_result)
        return final_result

    return TaskHandler(
        task_type="calibration",
        validate_input=validate_calibration_input,
        prepare=prepare,
        handle_worker_event=progress,
        finalize=finalize,
        # ProjectStore serializes the short final publication per project;
        # cloud inference across distinct projects must remain concurrent.
        resources=("worker", "provider_io"),
    )
