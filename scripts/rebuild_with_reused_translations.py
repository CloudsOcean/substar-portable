from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.config import load_settings  # noqa: E402
from substar_core.stage1 import display_normalize  # noqa: E402
from substar_core.stage2 import (  # noqa: E402
    Cue,
    build_cues,
    call_translation_model,
    chunk_groups,
    render_srt,
    subtitle_visual_width,
    validate_final,
    validate_translation,
)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_srt_targets(path: Path) -> dict[int, str]:
    result: dict[int, str] = {}
    for block in re.split(r"\r?\n\s*\r?\n", path.read_text(encoding="utf-8-sig").strip()):
        lines = block.splitlines()
        if len(lines) >= 4:
            result[int(lines[0])] = lines[3].strip()
    return result


def signature(cues: list[Cue]) -> list[tuple[int, int]]:
    return [(cue.alignment_start, cue.alignment_end) for cue in cues]


def overlap(left: Cue, right: Cue) -> int:
    return max(
        0,
        min(left.alignment_end, right.alignment_end)
        - max(left.alignment_start, right.alignment_start)
        + 1,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="复用旧译文，只重译 Stage1 边界发生变化的意义组"
    )
    parser.add_argument("--job-dir", required=True, type=Path)
    parser.add_argument("--old-stage1-dir", required=True, type=Path)
    parser.add_argument("--old-srt", required=True, type=Path)
    parser.add_argument("--new-stage1-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--no-api", action="store_true")
    args = parser.parse_args()

    settings = load_settings(include_secret=True)
    alignment = read_json(args.job_dir / "alignment.json")
    old_plan = read_json(args.old_stage1_dir / "stage1_direct_plan.json")
    new_plan = read_json(args.new_stage1_dir / "stage1_direct_plan.json")
    old_draft = (args.old_stage1_dir / "stage03A_source_draft.txt").read_text(
        encoding="utf-8"
    )
    new_draft = (args.new_stage1_dir / "stage03A_source_draft.txt").read_text(
        encoding="utf-8"
    )
    old_cues, _ = build_cues(
        old_draft,
        old_plan,
        alignment,
        source_baseline_punctuation="normalize",
    )
    new_cues, new_model_groups = build_cues(
        new_draft,
        new_plan,
        alignment,
        source_baseline_punctuation="preserve",
    )
    old_targets = read_srt_targets(args.old_srt)
    for cue in old_cues:
        cue.target = old_targets[cue.cue_id]

    old_by_group: dict[str, list[Cue]] = {}
    new_by_group: dict[str, list[Cue]] = {}
    for cue in old_cues:
        old_by_group.setdefault(cue.group_id, []).append(cue)
    for cue in new_cues:
        new_by_group.setdefault(cue.group_id, []).append(cue)

    changed_group_ids = {
        group_id
        for group_id, cues in new_by_group.items()
        if signature(cues) != signature(old_by_group.get(group_id, []))
    }
    reused = 0
    fallback = 0
    hard_limit_fallback = 0
    for group_id, cues in new_by_group.items():
        if group_id in changed_group_ids:
            continue
        old_lookup = {
            (cue.alignment_start, cue.alignment_end): cue.target
            for cue in old_by_group[group_id]
        }
        for cue in cues:
            cue.target = old_lookup[(cue.alignment_start, cue.alignment_end)]
            reused += 1

    changed_groups = [
        group for group in new_model_groups if group["group_id"] in changed_group_ids
    ]
    api_failures: list[dict[str, Any]] = []
    translated: dict[int, str] = {}
    api_key = str(settings.get("translation_api_key", ""))
    if changed_groups and api_key and not args.no_api:
        prompt = (PROJECT_ROOT / "prompts" / "03B_组级翻译与映射_JSON.md").read_text(
            encoding="utf-8"
        )
        chunks = chunk_groups(changed_groups, max_groups=12, max_characters=5000)

        def process(number: int, chunk: list[dict[str, Any]]) -> tuple[int, dict[int, str]]:
            try:
                result, _ = call_translation_model(
                    base_url=str(settings["translation_api_base_url"]),
                    api_key=api_key,
                    model=str(settings["translation_api_model"]),
                    system_prompt=prompt,
                    groups=chunk,
                    timeout=min(300, int(settings["translation_api_timeout_seconds"])),
                    thinking_mode="disabled",
                    reasoning_effort=str(settings["translation_reasoning_effort"]),
                )
                return number, validate_translation(
                    result,
                    chunk,
                    target_baseline_punctuation="normalize",
                    target_raised_punctuation="preserve",
                )
            except Exception as exc:
                api_failures.append(
                    {
                        "chunk": number,
                        "group_ids": [group["group_id"] for group in chunk],
                        "error": str(exc),
                    }
                )
                return number, {}

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(args.workers, len(chunks))
        ) as executor:
            futures = [
                executor.submit(process, number, chunk)
                for number, chunk in enumerate(chunks, start=1)
            ]
            for future in concurrent.futures.as_completed(futures):
                _, targets = future.result()
                translated.update(targets)

    cue_by_id = {cue.cue_id: cue for cue in new_cues}
    for cue_id, text in translated.items():
        cue_by_id[cue_id].target = text

    for group_id in changed_group_ids:
        old_group = old_by_group.get(group_id, [])
        for cue in new_by_group[group_id]:
            if cue.target:
                continue
            candidates = sorted(
                ((overlap(cue, old), old) for old in old_group),
                key=lambda item: item[0],
                reverse=True,
            )
            selected = candidates[0][1] if candidates and candidates[0][0] else None
            if selected is None:
                selected = min(
                    old_cues,
                    key=lambda old: abs(old.alignment_start - cue.alignment_start),
                )
            cue.target = display_normalize(
                selected.target,
                baseline_punctuation="normalize",
                raised_punctuation="preserve",
            )
            fallback += 1

    for cue in new_cues:
        target_han = len(re.findall(r"[\u3400-\u9fff]", cue.target))
        if target_han <= 24 and subtitle_visual_width(cue.target) <= 48:
            continue
        old_group = old_by_group.get(cue.group_id, [])
        candidates = sorted(
            ((overlap(cue, old), old) for old in old_group),
            key=lambda item: item[0],
            reverse=True,
        )
        selected = candidates[0][1] if candidates and candidates[0][0] else None
        if selected is None:
            continue
        cue.target = display_normalize(
            selected.target,
            baseline_punctuation="normalize",
            raised_punctuation="preserve",
        )
        hard_limit_fallback += 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_srt = args.output_dir / "substar_bilingual_reviewed_v3.srt"
    output_srt.write_text(render_srt(new_cues), encoding="utf-8-sig")
    report = validate_final(
        new_cues,
        source_baseline_punctuation="preserve",
        target_baseline_punctuation="normalize",
    )
    report["reuse"] = {
        "old_cue_count": len(old_cues),
        "new_cue_count": len(new_cues),
        "unchanged_group_count": len(new_by_group) - len(changed_group_ids),
        "changed_group_count": len(changed_group_ids),
        "reused_cue_count": reused,
        "api_translated_cue_count": len(translated),
        "fallback_cue_count": fallback,
        "hard_limit_fallback_cue_count": hard_limit_fallback,
        "api_failures": api_failures,
    }
    write_json(args.output_dir / "rebuild_report.json", report)
    print(json.dumps(report["reuse"], ensure_ascii=False))
    print(
        f"complete cues={len(new_cues)} "
        f"review={report['summary']['review_required_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
