from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from substar_core.ai_progress import ai_progress
from substar_core.credential_store import model_provider_credential_ref
from substar_core.artifacts import atomic_write_json
from substar_core.editor.translation.artifacts import TRANSLATION_INPUT_SCHEMA
from substar_core.process_command import python_script_command
from substar_core.runtime.model import InvalidTaskError
from substar_core.runtime.registry import TaskHandler, TaskWorkContext, WorkerLaunch
from substar_core.runtime.supervisor import WorkerCompletion
from substar_core.runtime.worker_protocol import WorkerMessage
from substar_core.storage import ProjectStore


_STEPS = {
    "translation.planning": "规划翻译单元",
    "translation.executing": "翻译处理中",
    "translation.repair": "修复未通过单元",
    "translation.validating": "验收翻译结果",
    "translation.materializing": "生成可编辑字幕",
    "translation.publishing": "交付翻译版本",
    "translation.completed": "翻译完成",
}


def validate_translation_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or payload.get("schema_version") != TRANSLATION_INPUT_SCHEMA:
        raise InvalidTaskError("unsupported translation task input")
    required = {
        "schema_version",
        "expected_revision_id",
        "source_language_selection",
        "source_language",
        "target_language",
        "mapping_mode",
        "provider_id",
        "credential_ref",
        "settings",
    }
    if set(payload) != required:
        raise InvalidTaskError("translation task input fields are invalid")
    if payload["mapping_mode"] not in {"one_to_one", "many_to_many"}:
        raise InvalidTaskError("translation mapping mode is invalid")
    if payload["source_language"] not in {"mixed", "zh-CN", "en", "ja", "ko"}:
        raise InvalidTaskError("translation source language is invalid")
    if payload["target_language"] not in {"auto_opposite", "zh-CN", "en", "ja", "ko"}:
        raise InvalidTaskError("translation target language is invalid")
    if payload["credential_ref"] != model_provider_credential_ref(str(payload["provider_id"])):
        raise InvalidTaskError("translation credential reference does not match provider")
    if not isinstance(payload["settings"], Mapping):
        raise InvalidTaskError("translation settings snapshot is invalid")
    return {**dict(payload), "settings": dict(payload["settings"])}


def build_translation_handler(projects_root: Path, application_root: Path) -> TaskHandler:
    projects_root = projects_root.resolve()
    application_root = application_root.resolve()

    def prepare(context: TaskWorkContext) -> WorkerLaunch:
        payload = validate_translation_input(context.input_payload)
        project_id = str(context.task.get("project_id") or "")
        project = (projects_root / project_id).resolve()
        if projects_root not in project.parents or not project.is_dir():
            raise InvalidTaskError("translation project does not exist")
        store = ProjectStore.open(project / "project")
        revision = store.load_latest()
        if revision is None or revision.revision_id != payload["expected_revision_id"]:
            raise InvalidTaskError("translation source revision changed")
        return WorkerLaunch(
            argv=tuple(python_script_command("scripts/run_translation_worker.py")),
            cwd=application_root,
            project_root=project,
            worker_input=payload,
            credential_refs=(str(payload["credential_ref"]),),
            timeout_seconds=float(payload["settings"].get("stage_timeout_seconds", 3600)),
        )

    def progress(_context: TaskWorkContext, message: WorkerMessage) -> Mapping[str, Any]:
        step = str(message.step or "")
        if step not in _STEPS:
            raise InvalidTaskError("translation progress step is invalid")
        phase = str(message.data.get("phase") or "primary")
        return {
            "progress": float(message.progress or 0.0),
            "message": _STEPS[step],
            "step": step,
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
        if not isinstance(result, Mapping) or result.get("schema_version") != "substar.translation-result.v2":
            raise InvalidTaskError("translation worker result is invalid")
        summary = result.get("summary")
        if not isinstance(summary, Mapping):
            raise InvalidTaskError("translation worker summary is invalid")
        project = Path(context.task.get("project_id") or "")
        project_root = (projects_root / project).resolve()
        revision = ProjectStore.open(project_root / "project").load_latest()
        if revision is None or revision.revision_id != summary.get("result_revision_id"):
            raise InvalidTaskError("translation result revision was not published")
        latest_path = project_root / "translation" / "latest.json"
        latest_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(latest_path, {
            "schema_version": "substar.translation-result.v2",
            "task_id": context.task["task_id"],
            **dict(summary),
        })
        problems = list(summary.get("problem_cue_ids") or [])
        problem_blocks = list(summary.get("problem_block_ids") or [])
        return {
            "result_revision_id": revision.revision_id,
            "problem_cue_ids": problems,
            "problem_block_ids": problem_blocks,
            "needs_attention": bool(problems),
            "mapping_mode": context.input_payload["mapping_mode"],
            "ai_progress": ai_progress(
                kind="translation",
                phase="completed",
                unit_label="块",
                unit_kind="translation_block",
                planned=int(summary.get("planned", 0) or 0),
                completed=int(summary.get("planned", 0) or 0),
                accepted=int(summary.get("planned", 0) or 0),
                failed=0,
                repair_planned=int(summary.get("repair_planned", 0) or 0),
                repair_completed=int(summary.get("repair_completed", 0) or 0),
                repair_accepted=int(summary.get("repair_accepted", 0) or 0),
                repair_failed=max(
                    0,
                    int(summary.get("repair_completed", 0) or 0)
                    - int(summary.get("repair_accepted", 0) or 0),
                ),
                problem_count=len(problem_blocks),
            ),
        }

    return TaskHandler(
        task_type="translation",
        validate_input=validate_translation_input,
        prepare=prepare,
        handle_worker_event=progress,
        finalize=finalize,
        # The worker performs provider work without holding a global write
        # lock. Publication is a short optimistic ProjectStore transaction,
        # so distinct projects can run concurrently without corrupting state.
        resources=("worker", "provider_io"),
    )
