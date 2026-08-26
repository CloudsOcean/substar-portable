from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ...artifacts import atomic_write_json
from ...process_command import python_script_command
from ...storage import ProjectStore
from ..tasks.contracts import EditorAiTaskState
from ..tasks.repository import finish_task as finish_editor_ai_task
from .artifacts import (
    TRANSLATION_PROGRESS_FILENAME,
    TRANSLATION_REVISION_FILENAME,
    TRANSLATION_SUBTITLE_FILENAME,
)


TRANSLATION_TASK_SCHEMA = "substar.translation-task.v1"
TRANSLATION_RESULT_SCHEMA = "substar.translation-result.v1"
_ACTIVE = {"queued", "running", "cancelling"}
PROJECT_ROOT = Path(__file__).resolve().parents[3]
_OWNED_TRANSLATION_TASK_IDS: set[str] = set()
_ACTIVE_PROCESSES_LOCK = threading.Lock()
_ACTIVE_PROCESSES: dict[str, subprocess.Popen[str]] = {}
_CANCELLED_TASK_IDS: set[str] = set()


class TranslationTaskError(RuntimeError):
    pass


def cancel_translation_task(job_dir: Path, task_id: str) -> dict[str, Any] | None:
    state = load_translation_status(job_dir)
    if state is None or str(state.get("task_id")) != task_id:
        return state
    if state.get("state") not in _ACTIVE:
        return state
    state.update(state="cancelling", message="正在取消翻译")
    atomic_write_json(translation_status_path(job_dir), state)
    with _ACTIVE_PROCESSES_LOCK:
        _CANCELLED_TASK_IDS.add(task_id)
        process = _ACTIVE_PROCESSES.get(task_id)
        if process is not None and process.poll() is None:
            process.terminate()
    return state


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def translation_status_path(job_dir: Path) -> Path:
    return job_dir / "translation" / "status.json"


def _source_rows(document: Any) -> list[dict[str, str]]:
    tokens = {token.token_id: token for token in document.display_tokens}
    rows: list[dict[str, str]] = []
    for cue in document.cues:
        if cue.state.value != "active":
            continue
        source_text = " ".join(
            tokens[token_id].text
            for token_id in cue.display_token_ids
            if token_id in tokens and tokens[token_id].state.value == "active"
        ).strip()
        rows.append({
            "cue_id": cue.cue_id,
            "source_hash": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        })
    return rows


def _translated_text_by_source_cue(document: Any) -> dict[str, str]:
    """Resolve translated presentation Cues through their source-Cue lineage."""
    parts: dict[str, list[str]] = {}
    for cue in document.cues:
        if cue.state.value != "active" or cue.target is None:
            continue
        target_text = cue.target.target_text.strip()
        if not target_text:
            continue
        mapping = cue.mapping if isinstance(cue.mapping, Mapping) else {}
        source_cue_ids = mapping.get("source_cue_ids", [])
        if not isinstance(source_cue_ids, (list, tuple)) or not source_cue_ids:
            raise TranslationTaskError(
                f"translated Cue {cue.cue_id} is missing source-Cue lineage"
            )
        for source_cue_id in source_cue_ids:
            bucket = parts.setdefault(str(source_cue_id), [])
            if not bucket or bucket[-1] != target_text:
                bucket.append(target_text)
    return {
        cue_id: "\n".join(values)
        for cue_id, values in parts.items()
        if values
    }


def _source_hashes_by_lineage(document: Any) -> dict[str, set[str]]:
    """Index current source text by the Cue ids it was translated from."""
    active_cues = (cue for cue in document.cues if cue.state.value == "active")
    current: dict[str, set[str]] = {}
    for row, cue in zip(_source_rows(document), active_cues):
        mapping = cue.mapping if isinstance(cue.mapping, Mapping) else {}
        source_cue_ids = mapping.get("source_cue_ids", [])
        lineage = (
            [str(value) for value in source_cue_ids]
            if isinstance(source_cue_ids, (list, tuple)) and source_cue_ids
            else [str(row["cue_id"])]
        )
        for source_cue_id in lineage:
            current.setdefault(source_cue_id, set()).add(row["source_hash"])
    return current


def _accepted_translation_rows(
    source_rows: list[dict[str, str]], translated_text: Mapping[str, str]
) -> tuple[list[dict[str, str]], list[str]]:
    accepted: list[dict[str, str]] = []
    problems: list[str] = []
    for row in source_rows:
        target_text = str(translated_text.get(row["cue_id"], "")).strip()
        if not target_text:
            problems.append(row["cue_id"])
        else:
            accepted.append({**row, "target_text": target_text})
    return accepted, problems


def _translation_problem_cue_ids(document: Any) -> list[str]:
    """Read the review queue written by the completed translation revision."""
    for change in reversed(document.changes):
        if change.operation != "contextual_translation":
            continue
        raw_ids = change.metadata.get("translation_problem_cue_ids", [])
        if not isinstance(raw_ids, (list, tuple)):
            return []
        return list(dict.fromkeys(str(cue_id) for cue_id in raw_ids if str(cue_id)))
    return []


def translation_stale_cue_ids(job_dir: Path, state: Mapping[str, Any]) -> list[str]:
    if state.get("state") != "succeeded":
        return []
    path = job_dir / "translation" / "latest.json"
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        latest = ProjectStore.open(job_dir / "project").load_latest()
        if latest is None:
            return []
        current = _source_hashes_by_lineage(latest.document)
        return [
            str(row["cue_id"])
            for row in result.get("cues", [])
            if row.get("source_hash") not in current.get(str(row.get("cue_id")), set())
        ]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return []


def _recover_completed_task(job_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    if state.get("state") not in _ACTIVE:
        return state
    if str(state.get("task_id")) in _OWNED_TRANSLATION_TASK_IDS:
        return state
    recovered = dict(state)
    recovered.update(
        state="interrupted",
        message="后端重启，翻译任务已中断",
        error="backend_restarted",
        finished_at=state.get("finished_at") or _now(),
    )
    atomic_write_json(translation_status_path(job_dir), recovered)
    return recovered


def load_translation_status(job_dir: Path) -> dict[str, Any] | None:
    path = translation_status_path(job_dir)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TranslationTaskError("翻译状态文件损坏") from exc
    if not isinstance(value, dict) or value.get("schema_version") != TRANSLATION_TASK_SCHEMA:
        raise TranslationTaskError("翻译状态契约不匹配")
    recovered = _recover_completed_task(job_dir, value)
    return {**recovered, "stale_cue_ids": translation_stale_cue_ids(job_dir, recovered)}


def _progress(stage_progress_path: Path) -> tuple[float, str]:
    try:
        value = json.loads(stage_progress_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0.05, "正在准备字幕翻译"
    stages = value.get("stages", {}) if isinstance(value, dict) else {}
    fractions: list[float] = []
    active = "字幕翻译"
    for name in ("字幕翻译",):
        row = stages.get(name, {}) if isinstance(stages, dict) else {}
        planned = max(1, int(row.get("planned", 0) or 0))
        accepted = min(planned, int(row.get("accepted", 0) or 0))
        status = str(row.get("status", "pending"))
        fractions.append(1.0 if status.startswith("completed") else accepted / planned)
        if status in {"running", "repairing"}:
            active = name
    return 0.05 + 0.9 * sum(fractions), f"{active} 翻译处理中"


def _command(
    *,
    job_dir: Path,
    run_dir: Path,
    source_revision_id: str,
    workers: int,
    settings: Mapping[str, Any],
) -> list[str]:
    return python_script_command(
        "scripts/run_production_translation.py",
        "--job-dir", str(job_dir),
        "--output-dir", str(run_dir),
        "--expected-revision-id", source_revision_id,
        "--settings-file", str(run_dir / "settings_snapshot.json"),
    )


def create_translation_task(
    job_dir: Path,
    *,
    expected_revision_id: str,
    workers: int,
    settings: Mapping[str, Any],
    start_background: bool = True,
    task_id: str | None = None,
) -> dict[str, Any]:
    if not 1 <= int(workers) <= 256:
        raise TranslationTaskError("translation workers 必须在 1..256")
    store = ProjectStore.open(job_dir / "project")
    latest = store.load_latest()
    if latest is None or latest.revision_id != expected_revision_id:
        raise TranslationTaskError("翻译所基于的编辑版本已变化")
    current = load_translation_status(job_dir)
    if current is not None and current.get("state") in _ACTIVE:
        raise TranslationTaskError("已有翻译任务正在运行")

    task_id = task_id or f"translation_{uuid.uuid4().hex}"
    source_language_selection = str(
        settings.get("translation_source_language_selection", "")
    )
    source_language = str(settings.get("translation_source_language", ""))
    if source_language_selection not in {"Auto", "mixed", "zh-CN", "en", "ja", "ko"}:
        raise TranslationTaskError("translation source language selection is missing or invalid")
    if source_language not in {"mixed", "zh-CN", "en", "ja", "ko"}:
        raise TranslationTaskError("effective translation source language is missing or invalid")
    target_language = str(settings.get("target_language_mode", "auto_opposite"))
    if target_language not in {"auto_opposite", "zh-CN", "en", "ja", "ko"}:
        raise TranslationTaskError(
            "target language must be one of auto_opposite, zh-CN, en, ja, ko"
        )
    run_dir = job_dir / "translation" / "runs" / task_id
    run_dir.mkdir(parents=True, exist_ok=False)
    state = {
        "schema_version": TRANSLATION_TASK_SCHEMA,
        "task_id": task_id,
        "project_id": job_dir.name,
        "state": "queued",
        "progress": 0.0,
        "message": "等待启动翻译",
        "error": "",
        "based_on_revision_id": expected_revision_id,
        "source_language_selection": source_language_selection,
        "source_language": source_language,
        "target_language": target_language,
        "result_revision_id": None,
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "run_path": run_dir.relative_to(job_dir).as_posix(),
        "artifacts": {},
    }
    atomic_write_json(translation_status_path(job_dir), state)
    _OWNED_TRANSLATION_TASK_IDS.add(task_id)
    if start_background:
        threading.Thread(
            target=run_translation_task,
            args=(job_dir, task_id, int(workers), dict(settings)),
            daemon=True,
        ).start()
    return state


def run_translation_task(
    job_dir: Path, task_id: str, workers: int, settings: Mapping[str, Any]
) -> None:
    state = load_translation_status(job_dir)
    if state is None or state.get("task_id") != task_id:
        raise TranslationTaskError("翻译任务身份不匹配")
    run_dir = job_dir / str(state["run_path"])
    status_path = translation_status_path(job_dir)
    state.pop("stale_cue_ids", None)
    state.update(state="running", progress=0.02, message="正在启动翻译", started_at=_now())
    atomic_write_json(status_path, state)
    atomic_write_json(
        run_dir / "settings_snapshot.json",
        {
            key: value for key, value in settings.items()
            if not any(marker in key.lower() for marker in ("api_key", "secret", "password", "authorization"))
            and not key.startswith("_")
        },
    )
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr:
            process = subprocess.Popen(
                _command(
                    job_dir=job_dir,
                    run_dir=run_dir,
                    source_revision_id=str(state["based_on_revision_id"]),
                    workers=workers,
                    settings=settings,
                ),
                cwd=str(PROJECT_ROOT),
                stdout=stdout,
                stderr=stderr,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            with _ACTIVE_PROCESSES_LOCK:
                _ACTIVE_PROCESSES[task_id] = process
            while process.poll() is None:
                with _ACTIVE_PROCESSES_LOCK:
                    cancelled = task_id in _CANCELLED_TASK_IDS
                if cancelled:
                    process.terminate()
                    break
                progress, message = _progress(run_dir / TRANSLATION_PROGRESS_FILENAME)
                state.update(progress=round(progress, 4), message=message)
                atomic_write_json(status_path, state)
                time.sleep(0.25)
            return_code = process.wait()
            with _ACTIVE_PROCESSES_LOCK:
                _ACTIVE_PROCESSES.pop(task_id, None)
                cancelled = task_id in _CANCELLED_TASK_IDS
        if cancelled:
            state.update(
                state="cancelled", progress=0.0, message="翻译已取消",
                error="", finished_at=_now(),
            )
            finish_editor_ai_task(job_dir, task_id, EditorAiTaskState.CANCELLED)
            _OWNED_TRANSLATION_TASK_IDS.discard(task_id)
            with _ACTIVE_PROCESSES_LOCK:
                _CANCELLED_TASK_IDS.discard(task_id)
            atomic_write_json(status_path, state)
            return
        if return_code:
            error = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise TranslationTaskError(error or f"翻译失败 exit={return_code}")
        pointer_path = run_dir / TRANSLATION_REVISION_FILENAME
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        result_revision_id = str(pointer["translation_revision_id"])
        final_srt = run_dir / TRANSLATION_SUBTITLE_FILENAME
        if not final_srt.is_file():
            raise TranslationTaskError("翻译未生成最终字幕")
        store = ProjectStore.open(job_dir / "project")
        source_revision = store.load_revision(str(state["based_on_revision_id"]))
        result_revision = store.load_revision(result_revision_id)
        source_rows = _source_rows(source_revision.document)
        translated_text = _translated_text_by_source_cue(result_revision.document)
        translation_rows, problem_cue_ids = _accepted_translation_rows(
            source_rows, translated_text
        )
        review_problem_cue_ids = _translation_problem_cue_ids(result_revision.document)
        result_path = job_dir / "translation" / "latest.json"
        atomic_write_json(result_path, {
            "schema_version": TRANSLATION_RESULT_SCHEMA,
            "task_id": task_id,
            "project_id": job_dir.name,
            "based_on_revision_id": str(state["based_on_revision_id"]),
            "source_language": str(state["source_language"]),
            "target_language": str(state["target_language"]),
            "cues": translation_rows,
            "problem_cue_ids": problem_cue_ids,
            "review_problem_cue_ids": review_problem_cue_ids,
        })
        state.update(
            state="succeeded",
            progress=1.0,
            message=(
                f"翻译完成，{len(review_problem_cue_ids)} 条需要人工检查并已计入问题字幕"
                if review_problem_cue_ids else "翻译完成"
            ),
            result_revision_id=result_revision_id,
            finished_at=_now(),
            artifacts={
                "final_srt": final_srt.relative_to(job_dir).as_posix(),
                "revision_pointer": pointer_path.relative_to(job_dir).as_posix(),
                "translation_result": result_path.relative_to(job_dir).as_posix(),
            },
        )
        finish_editor_ai_task(
            job_dir,
            task_id,
            EditorAiTaskState.SUCCEEDED,
            result_revision_id=result_revision_id,
        )
        _OWNED_TRANSLATION_TASK_IDS.discard(task_id)
    except Exception as exc:
        state.update(
            state="failed",
            message="翻译失败",
            error=str(exc),
            finished_at=_now(),
        )
        try:
            finish_editor_ai_task(
                job_dir,
                task_id,
                EditorAiTaskState.FAILED,
                error={"code": "translation_failed", "message": str(exc)[:2000]},
            )
        except Exception:
            pass
        _OWNED_TRANSLATION_TASK_IDS.discard(task_id)
        with _ACTIVE_PROCESSES_LOCK:
            _ACTIVE_PROCESSES.pop(task_id, None)
            _CANCELLED_TASK_IDS.discard(task_id)
    atomic_write_json(status_path, state)
