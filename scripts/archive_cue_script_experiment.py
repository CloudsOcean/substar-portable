from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import requests


ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def task(base_url: str, task_id: str) -> dict[str, Any]:
    response = requests.get(f"{base_url}/api/tasks/{task_id}", timeout=30)
    response.raise_for_status()
    return response.json()


def timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def exchange_rows(
    directory: Path, *, started_at: str, finished_at: str,
) -> list[dict[str, Any]]:
    left = timestamp(started_at) - 2.0
    right = timestamp(finished_at) + 2.0
    rows = []
    for path in sorted(directory.glob("*.json")):
        # Nanosecond-prefixed files are immutable, post-migration exchanges.
        if "_" not in path.stem or not path.stem.split("_", 1)[0].isdigit():
            continue
        event_seconds = int(path.stem.split("_", 1)[0]) / 1_000_000_000
        if left <= event_seconds <= right:
            rows.append({"path": path, "value": read_json(path)})
    return rows


def usage_summary(records: Iterable[Mapping[str, Any]], wall_seconds: float) -> dict[str, Any]:
    rows = list(records)
    prompt = completion = reasoning = cached = total = 0
    api_seconds = 0.0
    transport_attempts = 0
    finalizer_failures = 0
    repair_calls = 0
    for row in rows:
        telemetry = row.get("transport_telemetry", row)
        telemetry = telemetry if isinstance(telemetry, Mapping) else {}
        usage = telemetry.get("usage", {})
        usage = usage if isinstance(usage, Mapping) else {}
        details = usage.get("completion_tokens_details", {})
        details = details if isinstance(details, Mapping) else {}
        prompt_details = usage.get("prompt_tokens_details", {})
        prompt_details = prompt_details if isinstance(prompt_details, Mapping) else {}
        prompt += int(usage.get("prompt_tokens", 0) or 0)
        completion += int(usage.get("completion_tokens", 0) or 0)
        reasoning += int(details.get("reasoning_tokens", 0) or 0)
        cached += int(prompt_details.get("cached_tokens", 0) or 0)
        total += int(usage.get("total_tokens", 0) or 0)
        api_seconds += float(telemetry.get("duration_seconds", 0) or 0)
        transport_attempts += int(telemetry.get("transport_attempt_count", 0) or 0)
        finalizer_failures += bool(row.get("finalizer_error"))
        repair_calls += "repair" in str(row.get("stage", ""))
    return {
        "call_count": len(rows),
        "primary_call_count": len(rows) - repair_calls,
        "repair_call_count": repair_calls,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "cached_prompt_tokens": cached,
        "total_tokens": total,
        "prompt_share_percent": round(prompt * 100 / total, 2) if total else 0,
        "completion_share_percent": round(completion * 100 / total, 2) if total else 0,
        "api_duration_sum_seconds": round(api_seconds, 3),
        "task_wall_seconds": round(wall_seconds, 3),
        "concurrency_factor": round(api_seconds / wall_seconds, 2) if wall_seconds else None,
        "transport_attempt_count": transport_attempts,
        "finalizer_failure_count": finalizer_failures,
    }


def copy_tree(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--segmentation-task", required=True)
    parser.add_argument("--calibration-task", required=True)
    parser.add_argument("--translation-task", required=True)
    parser.add_argument("--exploratory-task", action="append", default=[])
    parser.add_argument("--label", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8769")
    args = parser.parse_args()

    project = ROOT / "data" / "projects-v2" / args.project_id
    runtime = ROOT / "data" / ".substar-workbench" / "task-runtime"
    archive = ROOT / "data" / "experiments" / args.label
    archive.mkdir(parents=True, exist_ok=True)

    tasks = {
        "segmentation": task(args.base_url, args.segmentation_task),
        "calibration": task(args.base_url, args.calibration_task),
        "translation": task(args.base_url, args.translation_task),
    }
    for name, value in tasks.items():
        write_json(archive / "tasks" / f"{name}.json", value)
    exploratory_tasks = {}
    for task_id in args.exploratory_task:
        value = task(args.base_url, task_id)
        exploratory_tasks[task_id] = value
        write_json(archive / "tasks" / "exploratory" / f"{task_id}.json", value)
        copy_tree(
            runtime / task_id,
            archive / "exploratory" / "task-runtime" / task_id,
        )

    segmentation_result_path = (
        runtime / args.segmentation_task / "attempts" / "1" / "work"
        / "algorithm" / "segmentation_algorithm_result.json"
    )
    segmentation_result = read_json(segmentation_result_path)
    segmentation_calls = list(segmentation_result.get("provenance", {}).get("api_calls", []))
    write_json(archive / "raw_exchanges" / "segmentation.json", segmentation_calls)
    copy_tree(
        segmentation_result_path,
        archive / "artifacts" / "segmentation_algorithm_result.json",
    )

    selected_exchanges: dict[str, list[dict[str, Any]]] = {}
    for name in ("calibration", "translation"):
        value = tasks[name]
        rows = exchange_rows(
            project / name / "block_cache" / "exchanges",
            started_at=str(value["started_at"]),
            finished_at=str(value["finished_at"]),
        )
        selected_exchanges[name] = [row["value"] for row in rows]
        for number, row in enumerate(rows, start=1):
            copy_tree(
                row["path"],
                archive / "raw_exchanges" / name / f"{number:03d}_{row['path'].name}",
            )
        # Earlier exploratory builds used cache-key filenames that could be
        # replaced on retry. Preserve every exchange still retained on disk,
        # separately from the immutable, time-windowed measured-run subset.
        copy_tree(
            project / name / "block_cache" / "exchanges",
            archive / "exploratory" / "all_retained_exchanges" / name,
        )

    for name, task_id in (
        ("calibration", args.calibration_task),
        ("translation", args.translation_task),
    ):
        copy_tree(
            runtime / task_id / "attempts" / str(tasks[name]["attempt"]) / "artifacts",
            archive / "artifacts" / name,
        )

    wall = {
        name: max(0.0, timestamp(value["finished_at"]) - timestamp(value["started_at"]))
        for name, value in tasks.items()
    }
    statistics = {
        "segmentation": usage_summary(segmentation_calls, wall["segmentation"]),
        "calibration": usage_summary(selected_exchanges["calibration"], wall["calibration"]),
        "translation": usage_summary(selected_exchanges["translation"], wall["translation"]),
    }
    statistics["all_model_stages"] = usage_summary(
        [*segmentation_calls, *selected_exchanges["calibration"], *selected_exchanges["translation"]],
        sum(wall.values()),
    )

    calibration_audit = read_json(project / "calibration" / "audit.json")
    translation_repair = read_json(
        runtime / args.translation_task / "attempts" / str(tasks["translation"]["attempt"])
        / "artifacts" / "contextual_translation_repair.json"
    )
    translation_report = read_json(project / "translation_report.json")
    translation_units = [
        unit
        for group in translation_report.get("presentation", {}).get("groups", [])
        for unit in group.get("meaning_units", [])
        if isinstance(unit, Mapping)
    ]
    actions = [
        action
        for block in calibration_audit.get("blocks", [])
        for action in block.get("accepted_actions", [])
        if isinstance(action, Mapping)
    ]
    risky_lexical = [
        {
            "action_id": row.get("action_id"),
            "before_text": row.get("before_text"),
            "after_text": row.get("after_text"),
            "disposition": row.get("disposition"),
        }
        for row in actions
        if row.get("kind") in {"replace_token", "replace_span"}
    ]
    report = {
        "schema_version": "substar.cue-script-experiment.v1",
        "label": args.label,
        "project_id": args.project_id,
        "protocol": "SUBSTAR-CUE-SCRIPT/1",
        "branch": subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=ROOT, text=True
        ).strip(),
        "base_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "task_ids": {
            "segmentation": args.segmentation_task,
            "calibration": args.calibration_task,
            "translation": args.translation_task,
        },
        "exploratory_task_ids": list(exploratory_tasks),
        "task_states": {name: value["state"] for name, value in tasks.items()},
        "statistics": statistics,
        "outcomes": {
            "segmentation": {
                "cue_count": len(segmentation_result.get("cues", [])),
                "exception_count": len(segmentation_result.get("exceptions", [])),
                "problem_block_count": int(tasks["segmentation"].get("result", {}).get("ai_progress", {}).get("problem_count", 0) or 0),
            },
            "calibration": {
                **dict(calibration_audit.get("summary", {})),
                "task_problem_count": int(tasks["calibration"].get("result", {}).get("ai_progress", {}).get("problem_count", 0) or 0),
                "lexical_actions": risky_lexical,
            },
            "translation": {
                "mapping_mode": tasks["translation"].get("result", {}).get("mapping_mode"),
                "task_problem_count": int(tasks["translation"].get("result", {}).get("ai_progress", {}).get("problem_count", 0) or 0),
                "repair_group_count": len(translation_repair.get("model_repair", {}).get("attempted_group_ids", [])),
                "unresolved_group_count": len(translation_repair.get("invalid_group_ids", [])),
                "group_count": len(translation_report.get("presentation", {}).get("groups", [])),
                "meaning_unit_count": len(translation_units),
                "empty_target_count": sum(
                    not str(unit.get("target_text", "")).strip()
                    for unit in translation_units
                ),
                "validation_warning_count": len(
                    translation_report.get("validation", {}).get("warnings", [])
                ),
            },
        },
        "audit_findings": [
            "The model-to-program boundary used only local Cue/word aliases; internal IDs were restored by deterministic ledgers.",
            "The final measured calibration task completed 8/8 blocks without structural repair or manual-review blocks.",
            "The final measured one-to-one translation completed 8/8 blocks; one missing primary Cue was repaired in one request and no Cue remained unresolved.",
            "A semantic lexical rewrite (sensible -> so-called) was observed in calibration. The finalizer policy was tightened after the run: dissimilar lexical substitutions are now review-only, while case, punctuation, close spelling corrections, and safe N:1 written-form merges remain auto-applicable.",
            "A decorative-symbol rewrite (actually -> actually‡) was also observed. Unsupported symbols are now review-only and covered by a regression test.",
            "One segmentation block used the delivery fallback, showing that the text protocol removes ID-binding errors but does not by itself eliminate model boundary-quality failures.",
            "Every immutable exchange from the measured runs and every older exchange still retained by the exploratory caches is archived with a SHA-256 manifest.",
        ],
    }
    write_json(archive / "report.json", report)

    markdown = [
        "# Cue Script real-API experiment",
        "",
        f"- Project: `{args.project_id}`",
        f"- Branch: `{report['branch']}`",
        f"- Protocol: `{report['protocol']}`",
        "",
        "| Stage | State | Calls | Repair | Prompt tokens | Completion tokens | Total tokens | API seconds (sum) | Wall seconds |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("segmentation", "calibration", "translation"):
        row = statistics[name]
        markdown.append(
            f"| {name} | {tasks[name]['state']} | {row['call_count']} | {row['repair_call_count']} | "
            f"{row['prompt_tokens']} | {row['completion_tokens']} | {row['total_tokens']} | "
            f"{row['api_duration_sum_seconds']} | {row['task_wall_seconds']} |"
        )
    markdown.extend(("", "## Findings", ""))
    markdown.extend(f"- {finding}" for finding in report["audit_findings"])
    (archive / "README.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")

    manifest = []
    for path in sorted(archive.rglob("*")):
        if path.is_file() and path.name != "manifest.sha256.json":
            manifest.append({
                "path": path.relative_to(archive).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            })
    write_json(archive / "manifest.sha256.json", manifest)
    print(archive)


if __name__ == "__main__":
    main()
