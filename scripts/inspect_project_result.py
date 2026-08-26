#!/usr/bin/env python3
"""Extract a compact, read-only split/translation audit from a Substar V2 project."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


OPEN_END_WORDS = {
    "a", "an", "the", "of", "to", "for", "by", "with", "from", "into",
    "in", "on", "at", "and", "or", "but", "because", "if", "when", "that",
    "which", "who", "whose", "is", "are", "was", "were", "be", "been",
    "being", "has", "have", "had", "do", "does", "did", "will", "would",
    "can", "could", "should", "may", "might", "must", "as", "than",
}


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def resolve_project(value: str, runtime_root: Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_dir():
        return candidate.resolve()
    candidate = runtime_root / value
    if candidate.is_dir():
        return candidate.resolve()
    raise FileNotFoundError(f"project not found: {value}")


def select_revision(project: Path, revision_id: str | None) -> tuple[Path, dict[str, Any]]:
    manifest = load_json(project / "project_v2" / "manifest.json", {})
    entries = manifest.get("revisions") or []
    wanted = revision_id or manifest.get("latest_revision_id")
    for entry in entries:
        if entry.get("revision_id") == wanted:
            path = project / "project_v2" / entry["path"]
            return path, load_json(path, {})
    files = sorted((project / "project_v2" / "revisions").glob("*.json"))
    if not files:
        raise FileNotFoundError("project has no V2 revisions")
    path = files[-1]
    return path, load_json(path, {})


def cue_range(value: str | None, total: int) -> tuple[int, int]:
    if not value:
        return 1, total
    match = re.fullmatch(r"\s*(\d+)(?:\s*[-:]\s*(\d+))?\s*", value)
    if not match:
        raise ValueError("--cues must look like 1-10 or 7")
    start = max(1, int(match.group(1)))
    end = min(total, int(match.group(2) or start))
    return min(start, end), max(start, end)


def source_text_for_cue(cue: dict[str, Any], display_by_id: dict[str, dict[str, Any]]) -> str:
    return " ".join(
        display_by_id[token_id].get("text", "")
        for token_id in cue.get("display_token_ids", [])
        if token_id in display_by_id
    ).strip()


def source_bounds_for_cue(
    cue: dict[str, Any],
    display_by_id: dict[str, dict[str, Any]],
    source_index: dict[str, int],
) -> tuple[int | None, int | None]:
    indices: list[int] = []
    for display_id in cue.get("display_token_ids", []):
        display = display_by_id.get(display_id, {})
        for source_id in display.get("source_token_ids", []):
            if source_id in source_index:
                indices.append(source_index[source_id])
    return (min(indices), max(indices)) if indices else (None, None)


def latest_p3_breaks(project: Path) -> dict[str, list[int]]:
    log_path = project / "experimental_stage1.stdout.log"
    if not log_path.exists():
        return {}
    result: dict[str, list[int]] = {}
    for line in log_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.startswith("SUBSTAR_RUNTIME_EVENT\t"):
            continue
        try:
            event = json.loads(line.split("\t", 1)[1])
        except (IndexError, json.JSONDecodeError):
            continue
        if not str(event.get("event", "")).startswith("P3 ") or "response" not in event.get("event", ""):
            continue
        groups = (((event.get("payload") or {}).get("output") or {}).get("groups") or [])
        result = {str(g.get("group_id")): list(g.get("line_breaks_after") or []) for g in groups}
    return result


def flags_for_boundary(text: str, next_text: str) -> list[str]:
    words = re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)*", text)
    next_words = re.findall(r"[A-Za-z]+(?:['’-][A-Za-z]+)*", next_text)
    if not words:
        return []
    last = words[-1].lower()
    flags: list[str] = []
    if last in OPEN_END_WORDS:
        flags.append(f"open-end:{last}")
    if last.endswith("'s") or last.endswith("’s"):
        flags.append("possessive-head-split")
    if last.endswith("ing") and next_words and next_words[0].lower() in {"a", "an", "the", "this", "that", "these", "those"}:
        flags.append("verb-object-split")
    return flags


def build_report(project: Path, revision_path: Path, revision: dict[str, Any]) -> dict[str, Any]:
    document = revision.get("document") or {}
    display_tokens = document.get("display_tokens") or []
    source_tokens = document.get("source_tokens") or []
    cues = document.get("cues") or []
    display_by_id = {str(item.get("token_id")): item for item in display_tokens}
    source_index = {str(item.get("token_id")): int(item.get("index", pos)) for pos, item in enumerate(source_tokens)}

    translation_plan = load_json(project / "stage1_experiment" / "stage1_translation_group_plan.json", {}) or {}
    display_plan = load_json(project / "stage1_experiment" / "stage1_direct_plan.json", {}) or {}
    protection = load_json(project / "stage1_experiment" / "p2_protection.json", {}) or {}
    stage_manifest = load_json(project / "stage1_experiment" / "experiment_stage_manifest.json", {}) or {}
    p3_breaks = latest_p3_breaks(project)

    translation_groups = translation_plan.get("groups") or []
    display_groups = display_plan.get("groups") or []
    spans = protection.get("spans") or []

    def covering_group(start: int | None, end: int | None, groups: list[dict[str, Any]]) -> str | None:
        if start is None or end is None:
            return None
        for group in groups:
            if int(group.get("alignment_start", -1)) <= start and int(group.get("alignment_end", -1)) >= end:
                return str(group.get("group_id"))
        return None

    rows: list[dict[str, Any]] = []
    for pos, cue in enumerate(cues):
        start_index, end_index = source_bounds_for_cue(cue, display_by_id, source_index)
        source_text = source_text_for_cue(cue, display_by_id)
        target = cue.get("target") or {}
        row = {
            "cue": pos + 1,
            "cue_id": cue.get("cue_id"),
            "time": [cue.get("start"), cue.get("end")],
            "source_range": [start_index, end_index],
            "source": source_text,
            "target": target.get("target_text") or "",
            "target_original": target.get("original_text") or "",
            "target_operation": ((target.get("provenance") or {}).get("operation")),
            "translation_group": covering_group(start_index, end_index, translation_groups),
            "display_group": covering_group(start_index, end_index, display_groups),
            "flags": [],
        }
        rows.append(row)

    target_counts = Counter(row["target"] for row in rows if row["target"])
    for pos, row in enumerate(rows):
        next_text = rows[pos + 1]["source"] if pos + 1 < len(rows) else ""
        row["flags"].extend(flags_for_boundary(row["source"], next_text))
        if row["target"] and target_counts[row["target"]] > 1:
            row["flags"].append(f"duplicate-target:{target_counts[row['target']]}")
        start_index, end_index = row["source_range"]
        if end_index is not None:
            for span in spans:
                span_start = int(span.get("alignment_start", -1))
                span_end = int(span.get("alignment_end", -1))
                if span_start <= end_index < span_end:
                    row["flags"].append(f"cuts-protection:{span.get('span_id')}")

    group_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["translation_group"]:
            group_rows[row["translation_group"]].append(row)
    groups: list[dict[str, Any]] = []
    for group in translation_groups:
        group_id = str(group.get("group_id"))
        members = group_rows.get(group_id, [])
        groups.append({
            "group_id": group_id,
            "source_range": [group.get("alignment_start"), group.get("alignment_end")],
            "cue_ids": [row["cue"] for row in members],
            "source": " / ".join(row["source"] for row in members),
            "targets": list(dict.fromkeys(row["target"] for row in members if row["target"])),
            "raw_p3_breaks_after": p3_breaks.get(group_id, []),
        })

    calls = {
        str(call.get("stage")): {
            "model": call.get("model"),
            "thinking_mode": call.get("thinking_mode"),
            "reasoning_effort": call.get("reasoning_effort"),
            "duration_seconds": call.get("duration_seconds"),
        }
        for call in (stage_manifest.get("api_calls") or [])
    }
    return {
        "project_id": project.name,
        "project_path": str(project),
        "revision_file": revision_path.name,
        "revision_id": revision.get("revision_id"),
        "revision_operation": ((revision.get("provenance") or {}).get("operation")),
        "cue_count": len(rows),
        "stage_calls": calls,
        "cues": rows,
        "translation_groups": groups,
    }


def markdown(report: dict[str, Any], start: int, end: int) -> str:
    lines = [
        f"# Substar result audit: {report['project_id']}",
        "",
        f"- Revision: `{report.get('revision_id')}` (`{report.get('revision_operation')}`)",
        f"- Cue count: {report.get('cue_count')}",
        f"- Range shown: {start}-{end}",
        f"- Stage calls: `{json.dumps(report.get('stage_calls'), ensure_ascii=False)}`",
        "",
        "## Cues",
        "",
        "| Cue | Source range | Translation group | Source | Target | Flags |",
        "|---:|:---:|:---:|---|---|---|",
    ]
    selected = report["cues"][start - 1:end]
    for row in selected:
        source = str(row["source"]).replace("|", "\\|")
        target = str(row["target"]).replace("|", "\\|")
        flags = ", ".join(row["flags"])
        bounds = row["source_range"]
        lines.append(
            f"| {row['cue']} | {bounds[0]}-{bounds[1]} | {row.get('translation_group') or ''} | "
            f"{source} | {target} | {flags} |"
        )

    wanted_groups = {row.get("translation_group") for row in selected}
    lines.extend(["", "## Meaning/translation groups", ""])
    for group in report["translation_groups"]:
        if group["group_id"] not in wanted_groups:
            continue
        lines.extend([
            f"### {group['group_id']} · cues {group['cue_ids']}",
            "",
            f"- Source: {group['source']}",
            f"- Raw P3 breaks after token indices: {group['raw_p3_breaks_after']}",
            f"- Unique targets: {group['targets']}",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="project ID or project directory")
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "projects",
    )
    parser.add_argument("--revision", help="specific revision ID; defaults to latest")
    parser.add_argument("--cues", help="cue range, for example 1-10")
    parser.add_argument("--format", choices=("md", "json"), default="md")
    parser.add_argument("--output", type=Path, help="write output to a file instead of stdout")
    args = parser.parse_args()

    try:
        project = resolve_project(args.project, args.runtime_root)
        revision_path, revision = select_revision(project, args.revision)
        report = build_report(project, revision_path, revision)
        start, end = cue_range(args.cues, report["cue_count"])
        output = (
            json.dumps({**report, "cues": report["cues"][start - 1:end]}, ensure_ascii=False, indent=2)
            if args.format == "json"
            else markdown(report, start, end)
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding="utf-8")
        else:
            sys.stdout.write(output)
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
