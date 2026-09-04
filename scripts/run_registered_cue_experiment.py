from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from substar_core.artifacts import atomic_write_json
from substar_core.creation import freeze_prompt_snapshot
from substar_core.domain import ChangeKind, ChangeProvenance
from substar_core.segmentation import validate_segmentation_request
from substar_core.segmentation.contracts import canonical_sha256
from substar_core.storage import ProjectStore
from substar_core.task_info import load_task_info, save_task_info


PROJECTS = ROOT / "data" / "projects-v2"
RUNTIME = ROOT / "data" / ".substar-workbench" / "task-runtime"
TERMINAL_STATES = {"succeeded", "succeeded_with_issues", "failed", "cancelled"}
SUPPORT_FILES = (
    "alignment.tsv",
    "asr_ingest_report.json",
    "audio_16k_mono.wav",
    "master_transcript.txt",
    "provider_submission_audit.json",
    "recognition_evidence.json",
    "recognition_request.json",
    "run_manifest.json",
    "segmentation_material.json",
)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def request_json(
    method: str,
    url: str,
    *,
    payload: Mapping[str, Any] | None = None,
    timeout: float = 60,
) -> dict[str, Any]:
    response = requests.request(method, url, json=payload, timeout=timeout)
    if not response.ok:
        raise RuntimeError(f"{method} {url} -> {response.status_code}: {response.text}")
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError(f"{method} {url} returned a non-object")
    return value


def task(base_url: str, task_id: str) -> dict[str, Any]:
    return request_json("GET", f"{base_url}/api/tasks/{task_id}")


def wait_tasks(
    base_url: str,
    task_ids: Mapping[str, str],
    *,
    timeout_seconds: float,
) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    previous: dict[str, tuple[Any, ...]] = {}
    while True:
        rows = {name: task(base_url, task_id) for name, task_id in task_ids.items()}
        for name, row in rows.items():
            marker = (
                row.get("state"),
                row.get("progress"),
                row.get("step"),
                row.get("message"),
            )
            if marker != previous.get(name):
                print(
                    f"[{name}] {row.get('state')} "
                    f"{float(row.get('progress') or 0) * 100:.0f}% "
                    f"{row.get('message') or ''}",
                    flush=True,
                )
                previous[name] = marker
        if all(str(row.get("state")) in TERMINAL_STATES for row in rows.values()):
            return rows
        if time.monotonic() >= deadline:
            raise TimeoutError("experiment tasks did not finish before timeout")
        time.sleep(2)


def link_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def copy_support_files(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for name in SUPPORT_FILES:
        path = source / name
        if path.is_file():
            link_or_copy(path, destination / name)
    source_input = source / "input"
    if source_input.is_dir():
        for path in source_input.rglob("*"):
            if path.is_file():
                link_or_copy(path, destination / "input" / path.relative_to(source_input))


def creation_shell(
    source: Path,
    destination: Path,
    *,
    project_id: str,
    display_name: str,
    segmentation_task_id: str = "",
    status: str = "processing",
) -> None:
    creation = read_json(source / "project_creation.json")
    creation["created_at"] = time.time()
    atomic_write_json(destination / "project_creation.json", creation)
    task_info = load_task_info(source, source.name)
    save_task_info(destination, project_id, {**task_info, "display_name": display_name})
    files = []
    for path in sorted(destination.iterdir()):
        if path.is_file() and path.name != "creation_state.json":
            files.append({
                "name": path.name,
                "size": path.stat().st_size,
                "url": f"/api/project-creations/{project_id}/files/{path.name}",
            })
    atomic_write_json(destination / "creation_state.json", {
        "id": project_id,
        "filename": str(creation.get("source_file") or ""),
        "display_name": display_name,
        "workflow_mode": "subtitle_creation",
        "source_job_name": "",
        "settings_overrides": {
            "language": task_info.get("language", "Auto"),
            "recognition_profile_label": "复用既有 ASR 证据",
            "recognition_profile_id": "reused_asr",
            "translation_workers": 64,
        },
        "status": status,
        "message": (
            "实验已创建，可以进入编辑模式"
            if status == "awaiting_edit" else "正在运行 Cue Script 真实实验"
        ),
        "progress": 1.0 if status == "awaiting_edit" else 0.01,
        "ai_progress": {},
        "error": "",
        "files": files,
        "stage_progress": {"schema_version": "substar.stage-progress.v1", "stages": {}},
        "created_at": time.time(),
        "attempt": 1,
        "transcription_task_id": "",
        "segmentation_task_id": segmentation_task_id,
        "runtime_log_url": f"/api/project-creations/{project_id}/logs",
        "tutorial_case_id": "",
        "submission_key": f"experiment-{uuid.uuid4()}",
        "submission_fingerprint": hashlib.sha256(project_id.encode()).hexdigest(),
        "cancel_requested": False,
    })


def segmentation_request(source: Path, destination: Path, project_id: str) -> dict[str, Any]:
    source_state = read_json(source / "creation_state.json")
    old_task_id = str(source_state.get("segmentation_task_id") or "")
    if not old_task_id:
        raise ValueError("source project has no segmentation task")
    database = ROOT / "data" / ".substar-workbench" / "runtime-v2.sqlite3"
    import sqlite3

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT input_json FROM tasks WHERE task_id=? AND task_type='segmentation'",
            (old_task_id,),
        ).fetchone()
    if row is None:
        raise ValueError("source segmentation task input is unavailable")
    value = json.loads(row[0])
    snapshot = freeze_prompt_snapshot(destination, ROOT)
    value["source_asset_id"] = project_id
    value["prompt_snapshot"] = snapshot
    unsigned = {key: item for key, item in value.items() if key != "input_fingerprint"}
    value["input_fingerprint"] = canonical_sha256(unsigned)
    return validate_segmentation_request(value)


def clone_segmented_project(
    source_project: Path,
    destination: Path,
    *,
    project_id: str,
    display_name: str,
    segmentation_task_id: str,
) -> str:
    copy_support_files(source_project, destination)
    creation_shell(
        source_project,
        destination,
        project_id=project_id,
        display_name=display_name,
        segmentation_task_id=segmentation_task_id,
        status="awaiting_edit",
    )
    revision = ProjectStore.open(source_project / "project").load_latest()
    if revision is None:
        raise ValueError("segmentation project has no document revision")
    store = ProjectStore.create(destination / "project", project_id=project_id)
    cloned = store.save(
        revision.document,
        provenance=ChangeProvenance(
            kind=ChangeKind.IMPORT,
            operation="cue_script_experiment_clone",
            actor="experiment-runner",
            metadata={
                "source_project_id": source_project.name,
                "source_revision_id": revision.revision_id,
                "segmentation_task_id": segmentation_task_id,
            },
        ),
    )
    return cloned.revision_id


def iso_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def exchange_records(directory: Path) -> list[dict[str, Any]]:
    return [read_json(path) for path in sorted(directory.glob("*.json"))]


def usage_summary(records: Iterable[Mapping[str, Any]], task_row: Mapping[str, Any]) -> dict[str, Any]:
    rows = list(records)
    prompt = completion = reasoning = cached = api_attempts = 0
    api_seconds = 0.0
    repair_calls = 0
    prompt_chars = response_chars = 0
    system_hashes: set[str] = set()
    for row in rows:
        telemetry = row.get("transport_telemetry", row)
        telemetry = telemetry if isinstance(telemetry, Mapping) else {}
        usage = telemetry.get("usage", {})
        usage = usage if isinstance(usage, Mapping) else {}
        completion_details = usage.get("completion_tokens_details", {})
        completion_details = completion_details if isinstance(completion_details, Mapping) else {}
        prompt_details = usage.get("prompt_tokens_details", {})
        prompt_details = prompt_details if isinstance(prompt_details, Mapping) else {}
        prompt += int(usage.get("prompt_tokens", 0) or 0)
        completion += int(usage.get("completion_tokens", 0) or 0)
        reasoning += int(completion_details.get("reasoning_tokens", 0) or 0)
        cached += int(prompt_details.get("cached_tokens", 0) or 0)
        api_seconds += float(telemetry.get("duration_seconds", 0) or 0)
        api_attempts += int(telemetry.get("transport_attempt_count", 1) or 1)
        repair_calls += "repair" in str(row.get("stage", "")).lower()
        prompt_chars += len(str(row.get("request_text") or ""))
        response_chars += len(str(row.get("raw_model_response") or ""))
        digest = str(row.get("system_prompt_sha256") or "")
        if digest:
            system_hashes.add(digest)
    wall = max(
        0.0,
        iso_seconds(str(task_row.get("finished_at") or ""))
        - iso_seconds(str(task_row.get("started_at") or "")),
    )
    return {
        "call_count": len(rows),
        "primary_call_count": len(rows) - repair_calls,
        "repair_call_count": repair_calls,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "cached_prompt_tokens": cached,
        "total_tokens": prompt + completion,
        "request_text_characters": prompt_chars,
        "raw_response_characters": response_chars,
        "unique_system_prompt_count": len(system_hashes),
        "api_duration_sum_seconds": round(api_seconds, 3),
        "task_wall_seconds": round(wall, 3),
        "concurrency_factor": round(api_seconds / wall, 2) if wall else None,
        "transport_attempt_count": api_attempts,
    }


def copy_tree(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def file_manifest(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "manifest.sha256.json":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append({
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": digest,
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-project", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8769")
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    args = parser.parse_args()

    source = (PROJECTS / args.source_project).resolve()
    if PROJECTS.resolve() not in source.parents or not source.is_dir():
        raise ValueError("source project is invalid")
    request_json("GET", f"{args.base_url}/api/runtime/identity")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    project_ids = {
        "segmentation": f"{stamp}_cue_text_split_{suffix}",
        "calibration": f"{stamp}_cue_text_cal_{suffix}",
        "translation_one_to_one": f"{stamp}_cue_text_tr1_{suffix}",
        "translation_many_to_many": f"{stamp}_cue_text_trm_{suffix}",
    }
    names = {
        "segmentation": "Cue文本协议实验 · 切分",
        "calibration": "Cue文本协议实验 · AI校准",
        "translation_one_to_one": "Cue文本协议实验 · 逐条翻译",
        "translation_many_to_many": "Cue文本协议实验 · 多对多翻译",
    }

    split = PROJECTS / project_ids["segmentation"]
    copy_support_files(source, split)
    creation_shell(
        source,
        split,
        project_id=project_ids["segmentation"],
        display_name=names["segmentation"],
    )
    split_input = segmentation_request(source, split, project_ids["segmentation"])
    split_task = request_json(
        "POST",
        f"{args.base_url}/api/projects/{project_ids['segmentation']}/tasks",
        payload={
            "task_type": "segmentation",
            "input_schema": split_input["schema_version"],
            "input": split_input,
        },
    )
    split_task_id = str(split_task["task_id"])
    split_result = wait_tasks(
        args.base_url,
        {"segmentation": split_task_id},
        timeout_seconds=args.timeout_seconds,
    )["segmentation"]
    if split_result["state"] not in {"succeeded", "succeeded_with_issues"}:
        raise RuntimeError(f"segmentation failed: {split_result}")
    creation_shell_path = split / "creation_state.json"
    state = read_json(creation_shell_path)
    state.update({
        "status": "awaiting_edit",
        "message": "实验切分已交付，可以进入编辑模式",
        "progress": 1.0,
        "segmentation_task_id": split_task_id,
        "ai_progress": dict(split_result.get("result", {}).get("ai_progress") or {}),
    })
    atomic_write_json(creation_shell_path, state)

    revisions: dict[str, str] = {}
    for stage in ("calibration", "translation_one_to_one", "translation_many_to_many"):
        revisions[stage] = clone_segmented_project(
            split,
            PROJECTS / project_ids[stage],
            project_id=project_ids[stage],
            display_name=names[stage],
            segmentation_task_id=split_task_id,
        )

    started = {
        "calibration": request_json(
            "POST",
            f"{args.base_url}/api/projects/{project_ids['calibration']}/ai-calibrate",
            payload={"expected_revision_id": revisions["calibration"], "instruction": ""},
        ),
        "translation_one_to_one": request_json(
            "POST",
            f"{args.base_url}/api/projects/{project_ids['translation_one_to_one']}/translation",
            payload={
                "expected_revision_id": revisions["translation_one_to_one"],
                "workers": 64,
                "source_language": "en",
                "target_language": "zh-CN",
                "mapping_mode": "one_to_one",
            },
        ),
        "translation_many_to_many": request_json(
            "POST",
            f"{args.base_url}/api/projects/{project_ids['translation_many_to_many']}/translation",
            payload={
                "expected_revision_id": revisions["translation_many_to_many"],
                "workers": 64,
                "source_language": "en",
                "target_language": "zh-CN",
                "mapping_mode": "many_to_many",
            },
        ),
    }
    task_ids = {name: str(row["task_id"]) for name, row in started.items()}
    stage_tasks = wait_tasks(
        args.base_url,
        task_ids,
        timeout_seconds=args.timeout_seconds,
    )
    tasks = {"segmentation": split_result, **stage_tasks}

    archive = ROOT / "data" / "experiments" / args.label
    archive.mkdir(parents=True, exist_ok=False)
    records_by_stage: dict[str, list[dict[str, Any]]] = {}
    split_result_path = (
        RUNTIME / split_task_id / "attempts" / str(split_result["attempt"])
        / "work" / "algorithm" / "segmentation_algorithm_result.json"
    )
    split_algorithm = read_json(split_result_path)
    records_by_stage["segmentation"] = list(
        split_algorithm.get("provenance", {}).get("api_calls", [])
    )
    copy_tree(split_result_path, archive / "artifacts" / "segmentation_algorithm_result.json")
    atomic_write_json(archive / "raw_exchanges" / "segmentation.json", records_by_stage["segmentation"])

    directory_names = {
        "calibration": "calibration",
        "translation_one_to_one": "translation",
        "translation_many_to_many": "translation",
    }
    for stage in directory_names:
        project = PROJECTS / project_ids[stage]
        exchange_dir = project / directory_names[stage] / "block_cache" / "exchanges"
        records_by_stage[stage] = exchange_records(exchange_dir)
        copy_tree(exchange_dir, archive / "raw_exchanges" / stage)
        runtime_artifacts = (
            RUNTIME / task_ids[stage] / "attempts" / str(tasks[stage]["attempt"])
            / "artifacts"
        )
        copy_tree(runtime_artifacts, archive / "artifacts" / stage)
        for candidate in (
            project / "calibration" / "audit.json",
            project / "translation_report.json",
            project / "translation" / "latest.json",
        ):
            if candidate.is_file():
                copy_tree(candidate, archive / "published" / stage / candidate.name)

    for stage, row in tasks.items():
        atomic_write_json(archive / "tasks" / f"{stage}.json", row)
    copy_tree(ROOT / "prompts" / "production", archive / "prompt_snapshot" / "production")

    stats = {
        stage: usage_summary(records_by_stage[stage], tasks[stage])
        for stage in tasks
    }
    all_records = [row for stage in records_by_stage.values() for row in stage]
    totals = usage_summary(all_records, {
        "started_at": min(str(row.get("started_at") or "") for row in tasks.values()),
        "finished_at": max(str(row.get("finished_at") or "") for row in tasks.values()),
    })
    report = {
        "schema_version": "substar.registered-cue-experiment.v1",
        "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_project_id": source.name,
        "branch": subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=ROOT, text=True
        ).strip(),
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "dirty_worktree": bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True
        ).strip()),
        "project_ids": project_ids,
        "task_ids": {"segmentation": split_task_id, **task_ids},
        "task_states": {stage: row["state"] for stage, row in tasks.items()},
        "statistics": {**stats, "all_model_stages": totals},
        "pricing": {
            "currency": "CNY",
            "estimated_cost": None,
            "reason": "模型 glm-5.3-flash 的公开按输入/输出拆分单价未写入程序；避免伪造金额。",
        },
    }
    atomic_write_json(archive / "report.json", report)
    lines = [
        "# Registered Cue Script real-API experiment",
        "",
        f"- Source project: `{source.name}`",
        f"- Branch: `{report['branch']}`",
        f"- Commit: `{report['commit']}` (working tree dirty: `{report['dirty_worktree']}`)",
        "- All source ASR evidence was reused; ASR was not called again.",
        "- Calibration, one-to-one translation and many-to-many translation were dispatched concurrently after segmentation.",
        "",
        "| Stage | Project | Task | State | Calls | Repair | Prompt | Completion | Reasoning | Cached | API sum (s) | Wall (s) |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for stage in tasks:
        row = stats[stage]
        lines.append(
            f"| {stage} | `{project_ids[stage]}` | `{report['task_ids'][stage]}` | "
            f"{tasks[stage]['state']} | {row['call_count']} | {row['repair_call_count']} | "
            f"{row['prompt_tokens']} | {row['completion_tokens']} | {row['reasoning_tokens']} | "
            f"{row['cached_prompt_tokens']} | {row['api_duration_sum_seconds']} | {row['task_wall_seconds']} |"
        )
    lines.extend((
        "",
        "Exact system prompts, request text, raw responses, finalized results and provider telemetry are retained under `raw_exchanges/`.",
        "The SHA-256 manifest makes later review able to detect any changed archive file.",
    ))
    (archive / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    atomic_write_json(archive / "manifest.sha256.json", file_manifest(archive))
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print(f"ARCHIVE={archive}", flush=True)


if __name__ == "__main__":
    main()
