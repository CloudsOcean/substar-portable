from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from substar_core.artifacts import atomic_write_json
from scripts.run_registered_cue_experiment import (
    PROJECTS,
    RUNTIME,
    clone_segmented_project,
    copy_tree,
    exchange_records,
    file_manifest,
    link_or_copy,
    read_json,
    request_json,
    usage_summary,
    wait_tasks,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rerun one real translation mode from registered segmented evidence."
    )
    parser.add_argument("--segmented-project", required=True)
    parser.add_argument(
        "--mapping-mode", choices=("one_to_one", "many_to_many"), required=True
    )
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--seed-cache-project",
        help="Copy only validated content-addressed cache entries; exchanges remain new.",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8769")
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    args = parser.parse_args()

    source = (PROJECTS / args.segmented_project).resolve()
    if PROJECTS.resolve() not in source.parents or not source.is_dir():
        raise ValueError("segmented project is invalid")
    request_json("GET", f"{args.base_url}/api/runtime/identity")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    short = "tr1" if args.mapping_mode == "one_to_one" else "trm"
    project_id = f"{stamp}_cue_text_{short}_{suffix}"
    project = PROJECTS / project_id
    state = read_json(source / "creation_state.json")
    segmentation_task_id = str(state.get("segmentation_task_id") or "")
    revision_id = clone_segmented_project(
        source,
        project,
        project_id=project_id,
        display_name=f"Cue文本协议复验 · {args.mapping_mode}",
        segmentation_task_id=segmentation_task_id,
    )
    if args.seed_cache_project:
        seed = (PROJECTS / args.seed_cache_project / "translation" / "block_cache").resolve()
        if PROJECTS.resolve() not in seed.parents or not seed.is_dir():
            raise ValueError("seed cache project is invalid")
        destination_cache = project / "translation" / "block_cache"
        destination_cache.mkdir(parents=True, exist_ok=True)
        for entry in seed.glob("*.json"):
            link_or_copy(entry, destination_cache / entry.name)
    started = request_json(
        "POST",
        f"{args.base_url}/api/projects/{project_id}/translation",
        payload={
            "expected_revision_id": revision_id,
            "workers": 64,
            "source_language": "en",
            "target_language": "zh-CN",
            "mapping_mode": args.mapping_mode,
        },
    )
    task_id = str(started["task_id"])
    task_row = wait_tasks(
        args.base_url,
        {"translation": task_id},
        timeout_seconds=args.timeout_seconds,
    )["translation"]

    archive = ROOT / "data" / "experiments" / args.label
    archive.mkdir(parents=True, exist_ok=False)
    exchange_dir = project / "translation" / "block_cache" / "exchanges"
    records = exchange_records(exchange_dir)
    copy_tree(exchange_dir, archive / "raw_exchanges" / "translation")
    runtime_artifacts = (
        RUNTIME / task_id / "attempts" / str(task_row["attempt"]) / "artifacts"
    )
    copy_tree(runtime_artifacts, archive / "artifacts" / "translation")
    for candidate in (
        project / "translation_report.json",
        project / "translation" / "latest.json",
    ):
        if candidate.is_file():
            copy_tree(candidate, archive / "published" / candidate.name)
    copy_tree(ROOT / "prompts" / "production", archive / "prompt_snapshot" / "production")
    atomic_write_json(archive / "task.json", task_row)

    report = {
        "schema_version": "substar.registered-translation-experiment.v1",
        "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_segmented_project_id": source.name,
        "source_segmentation_task_id": segmentation_task_id,
        "project_id": project_id,
        "task_id": task_id,
        "mapping_mode": args.mapping_mode,
        "seed_cache_project_id": args.seed_cache_project or None,
        "state": task_row["state"],
        "result": task_row.get("result"),
        "statistics": usage_summary(records, task_row),
        "branch": subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=ROOT, text=True
        ).strip(),
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "dirty_worktree": bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True
        ).strip()),
        "pricing": {
            "currency": "CNY",
            "estimated_cost": None,
            "reason": "No verified split input/output price for the configured model is stored locally.",
        },
    }
    atomic_write_json(archive / "report.json", report)
    stats = report["statistics"]
    lines = [
        "# Registered translation real-API rerun",
        "",
        f"- Source segmented project: `{source.name}`",
        f"- Project: `{project_id}`",
        f"- Task: `{task_id}`",
        f"- Mapping mode: `{args.mapping_mode}`",
        f"- State: `{task_row['state']}`",
        "- ASR and segmentation evidence were reused; neither stage was called again.",
        f"- Calls: {stats['call_count']} ({stats['repair_call_count']} repair)",
        f"- Tokens: {stats['total_tokens']} (prompt {stats['prompt_tokens']}, completion {stats['completion_tokens']}, reasoning {stats['reasoning_tokens']})",
        f"- API duration sum: {stats['api_duration_sum_seconds']} s; task wall: {stats['task_wall_seconds']} s.",
        "",
        "Exact prompts, requests, raw responses, finalized results and telemetry are in `raw_exchanges/translation/`.",
    ]
    (archive / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    atomic_write_json(archive / "manifest.sha256.json", file_manifest(archive))
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print(f"ARCHIVE={archive}", flush=True)


if __name__ == "__main__":
    main()
