from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_stage1_pipeline import (  # noqa: E402
    SCHEMAS,
    SPEC,
    Stage1PipelineError,
    call_model,
    read,
    resolve_api_key,
    shared_context,
    source_punctuation_kwargs,
    write_json,
)
from substar_core.stage1 import extract_alignment, extract_master  # noqa: E402
from substar_core.stage1_chunking import _unit_original_ranges  # noqa: E402
from substar_core.stage1_direct import evaluate_direct_plan, structural_issues  # noqa: E402


LOCAL_PROMPT = PROJECT_ROOT / "prompts" / "03A_L_索引计划局部候选.md"
JUDGE_PROMPT = PROJECT_ROOT / "prompts" / "03A_J_局部候选盲评.md"
JUDGE_SCHEMA = PROJECT_ROOT / "schemas" / "stage1_local_judge.schema.json"
TARGET_CODES = {
    "cross_group_dangling_phrase",
    "crossed_sentence_boundary",
    "dangling_function_phrase",
    "dangling_line_end",
    "mergeable_short_cue",
    "sub_minimum_duration_cue",
    "suspected_named_entity_apposition_cut",
}
CRITICAL_CODES = {
    "cross_group_dangling_phrase",
    "crossed_sentence_boundary",
    "dangling_function_phrase",
    "dangling_line_end",
    "sub_minimum_duration_cue",
}


def metrics(result: Any) -> dict[str, int]:
    counts = Counter(
        str(item.get("code", "")) for item in result.review_notices
    )
    return {
        "hard_issues": len(result.issues),
        "critical": sum(counts[code] for code in CRITICAL_CODES),
        "mergeable": counts["mergeable_short_cue"],
        "crossed": counts["crossed_sentence_boundary"],
        "multi_sentence": counts["multi_sentence_group"],
        "review_total": len(result.review_notices),
        "cue_count": int(result.validation.get("stats", {}).get("cue_count", 0)),
    }


def dominates(before: dict[str, int], after: dict[str, int]) -> bool:
    if after["hard_issues"]:
        return False
    protected_dimensions = (
        "critical",
        "mergeable",
        "crossed",
        "multi_sentence",
        "cue_count",
    )
    if any(after[key] > before[key] for key in protected_dimensions):
        return False
    return any(after[key] < before[key] for key in protected_dimensions)


def group_positions(plan: dict[str, Any]) -> dict[str, int]:
    return {
        str(group.get("group_id", "")): position
        for position, group in enumerate(plan.get("groups", []))
    }


def problem_windows(
    plan: dict[str, Any],
    result: Any,
    *,
    radius: int,
    cluster_distance: int,
    extra_issues: list[dict[str, Any]] | None = None,
) -> list[tuple[int, int, list[dict[str, Any]]]]:
    positions = group_positions(plan)
    targets: list[tuple[int, dict[str, Any]]] = []
    notices = list(result.review_notices) + list(extra_issues or [])
    for item in notices:
        if (
            str(item.get("code", "")) not in TARGET_CODES
            and str(item.get("origin", "")) != "critic"
        ):
            continue
        position = positions.get(str(item.get("group_id", "")))
        if position is not None:
            targets.append((position, item))
    if not targets:
        return []
    clusters: list[list[tuple[int, dict[str, Any]]]] = []
    for target in sorted(targets, key=lambda value: value[0]):
        if (
            not clusters
            or target[0] - clusters[-1][-1][0] > cluster_distance
        ):
            clusters.append([target])
        else:
            clusters[-1].append(target)
    count = len(plan.get("groups", []))
    windows: list[tuple[int, int, list[dict[str, Any]]]] = []
    groups = plan["groups"]
    for cluster in clusters:
        left = max(0, cluster[0][0] - radius)
        right = min(count - 1, cluster[-1][0] + radius)
        windows.append(
            (
                int(groups[left]["alignment_start"]),
                int(groups[right]["alignment_end"]),
                [item for _, item in cluster],
            )
        )
    return windows


def locate_window(
    plan: dict[str, Any],
    alignment_start: int,
    alignment_end: int,
) -> tuple[int, int]:
    groups = plan["groups"]
    left = next(
        position
        for position, group in enumerate(groups)
        if int(group["alignment_end"]) >= alignment_start
    )
    right = max(
        position
        for position, group in enumerate(groups)
        if int(group["alignment_start"]) <= alignment_end
    )
    return left, right


def render_local_material(
    master: str,
    units: list[Any],
    groups: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> tuple[str, list[Any]]:
    first = int(groups[0]["alignment_start"])
    last = int(groups[-1]["alignment_end"])
    unit_positions = {unit.index: position for position, unit in enumerate(units)}
    start_position = unit_positions[first]
    end_position = unit_positions[last]
    local_units = units[start_position : end_position + 1]
    ranges = _unit_original_ranges(master, units)
    char_start = ranges[start_position][0]
    char_end = (
        ranges[end_position + 1][0]
        if end_position + 1 < len(ranges)
        else len(master)
    )
    local_master = master[char_start:char_end].strip()
    alignment = "\n".join(
        f"{unit.index}\t{unit.start:.3f}\t{unit.end:.3f}\t{unit.text}\t"
        f"{unit.sentence_id if unit.sentence_id is not None else '-'}\t"
        f"{1 if unit.sentence_start else 0}\t{1 if unit.sentence_end else 0}"
        for unit in local_units
    )
    material = "\n\n".join(
        [
            "# LOCAL_WINDOW",
            f"required_alignment_start: {first}\nrequired_alignment_end: {last}",
            "## MASTER_TRANSCRIPT\n```text\n" + local_master + "\n```",
            (
                "## ALIGNMENT\n"
                "index / start / end / text / whisper_sentence_id / "
                "sentence_start / sentence_end\n```tsv\n"
                + alignment
                + "\n```"
            ),
            "# INITIAL_WINDOW_PLAN\n" + json.dumps(
                {
                    "schema_version": "substar.stage1.direct.v1",
                    "source_language": "Auto",
                    "groups": groups,
                    "coverage_check": {"complete": True, "ordered": True},
                },
                ensure_ascii=False,
            ),
            "# PROGRAM_ISSUES\n" + json.dumps(issues, ensure_ascii=False),
        ]
    )
    return material, local_units


def renumber(plan: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(plan)
    for number, group in enumerate(value.get("groups", []), start=1):
        group["group_id"] = f"g{number:04d}"
    value["coverage_check"] = {"complete": True, "ordered": True}
    return value


def splice(
    plan: dict[str, Any],
    left: int,
    right: int,
    replacement: dict[str, Any],
) -> dict[str, Any]:
    value = copy.deepcopy(plan)
    value["groups"] = (
        value["groups"][:left]
        + copy.deepcopy(replacement["groups"])
        + value["groups"][right + 1 :]
    )
    return renumber(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage1 局部候选与单调接受实验")
    parser.add_argument("material", type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--api-key-env", default="SUBSTAR_LLM_API_KEY")
    parser.add_argument("--candidates", type=int, default=3)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--radius", type=int, default=1)
    parser.add_argument("--cluster-distance", type=int, default=0)
    parser.add_argument("--max-windows", type=int)
    parser.add_argument("--external-issues", type=Path)
    parser.add_argument("--blind-seed", type=int, default=20260726)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    api_key, key_source = resolve_api_key(args.api_key_env)
    if not api_key:
        raise RuntimeError("未配置 Stage1 LLM API key")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    material = read(args.material)
    master = extract_master(material)
    units = extract_alignment(material)
    plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
    current = evaluate_direct_plan(
        master,
        units,
        plan,
        **source_punctuation_kwargs(),
    )
    if not current.valid:
        raise RuntimeError(f"输入计划未通过硬校验：{current.issues[:5]}")

    extra_issues: list[dict[str, Any]] = []
    if args.external_issues:
        external = json.loads(args.external_issues.read_text(encoding="utf-8-sig"))
        extra_issues = [
            {**item, "code": "critic_boundary", "origin": "critic"}
            for item in external.get("issues", [])
            if item.get("severity") in {"high", "medium"}
        ]
    windows = problem_windows(
        plan,
        current,
        radius=max(0, args.radius),
        cluster_distance=max(0, args.cluster_distance),
        extra_issues=extra_issues,
    )
    if args.max_windows is not None:
        windows = windows[: args.max_windows]
    experiment: list[dict[str, Any]] = []
    accepted = 0

    for window_number, (window_start, window_end, issues) in enumerate(windows, start=1):
        left, right = locate_window(plan, window_start, window_end)
        frozen_groups = copy.deepcopy(plan["groups"][left : right + 1])
        local_material, local_units = render_local_material(
            master,
            units,
            frozen_groups,
            issues,
        )
        window_dir = args.output_dir / f"w{window_number:04d}"
        window_dir.mkdir(parents=True, exist_ok=True)
        (window_dir / "local_material.md").write_text(local_material, encoding="utf-8")
        system_prompt = "\n\n".join(
            [
                read(LOCAL_PROMPT),
                shared_context(local_material),
                "# OUTPUT_SCHEMA\n" + read(SCHEMAS["direct"]),
            ]
        )

        def request(candidate_number: int) -> tuple[int, dict[str, Any], dict[str, Any]]:
            candidate, telemetry = call_model(
                base_url=args.base_url,
                api_key=api_key,
                model=args.model,
                system_prompt=system_prompt,
                user_payload=local_material,
                timeout=args.timeout,
                max_tokens=16384,
                json_mode=True,
                thinking_mode="enabled",
                reasoning_effort="high",
                request_attempts=2,
            )
            return candidate_number, candidate, telemetry

        responses: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(args.workers, args.candidates))
        ) as executor:
            futures = [
                executor.submit(request, number)
                for number in range(1, args.candidates + 1)
            ]
            for future in concurrent.futures.as_completed(futures):
                try:
                    responses.append(future.result())
                except Exception as exc:
                    experiment.append(
                        {
                            "window": window_number,
                            "candidate": None,
                            "accepted": False,
                            "error": str(exc),
                        }
                    )

        before_metrics = metrics(current)
        viable: list[tuple[tuple[int, ...], int, dict[str, Any], Any]] = []
        valid_candidates: list[
            tuple[int, dict[str, Any], dict[str, Any], Any, dict[str, int]]
        ] = []
        for candidate_number, candidate, telemetry in responses:
            write_json(
                window_dir / f"candidate_{candidate_number:02d}.json",
                candidate,
            )
            write_json(
                window_dir / f"candidate_{candidate_number:02d}_telemetry.json",
                telemetry,
            )
            candidate = renumber(candidate)
            candidate_structural = structural_issues(candidate, local_units)
            if candidate_structural:
                experiment.append(
                    {
                        "window": window_number,
                        "candidate": candidate_number,
                        "accepted": False,
                        "reason": "local_structural_failure",
                        "issues": candidate_structural,
                    }
                )
                continue
            proposed = splice(plan, left, right, candidate)
            proposed_result = evaluate_direct_plan(
                master,
                units,
                proposed,
                **source_punctuation_kwargs(),
            )
            after_metrics = metrics(proposed_result)
            accepted_candidate = dominates(before_metrics, after_metrics)
            experiment.append(
                {
                    "window": window_number,
                    "candidate": candidate_number,
                    "accepted": accepted_candidate,
                    "before": before_metrics,
                    "after": after_metrics,
                }
            )
            valid_candidates.append(
                (
                    candidate_number,
                    candidate,
                    proposed,
                    proposed_result,
                    after_metrics,
                )
            )
            if accepted_candidate:
                rank = (
                    after_metrics["critical"],
                    after_metrics["mergeable"],
                    after_metrics["crossed"],
                    after_metrics["multi_sentence"],
                    after_metrics["review_total"],
                    after_metrics["cue_count"],
                )
                viable.append(
                    (rank, candidate_number, proposed, proposed_result)
                )

        judged_winner: tuple[int, dict[str, Any], Any] | None = None
        # A deterministic score can prove that a candidate is structurally
        # non-worse, but it cannot prove semantic superiority.  This applies
        # equally to program-detected and critic-detected windows: otherwise a
        # candidate can "fix" a dangling-word counter by moving the break into
        # a worse idiomatic position.  Therefore every automatic edit must beat
        # the frozen original in an anonymous semantic judgment.
        if valid_candidates:
            original_local = {
                "schema_version": "substar.stage1.direct.v1",
                "source_language": "Auto",
                "groups": renumber(
                    {
                        "schema_version": "substar.stage1.direct.v1",
                        "source_language": "Auto",
                        "groups": frozen_groups,
                        "coverage_check": {"complete": True, "ordered": True},
                    }
                )["groups"],
                "coverage_check": {"complete": True, "ordered": True},
            }
            anonymous: list[tuple[str, str, dict[str, Any]]] = [
                ("original", "original", original_local)
            ]
            anonymous.extend(
                (
                    f"candidate_{number}",
                    f"candidate_{number}",
                    candidate,
                )
                for number, candidate, _, _, _ in valid_candidates
            )
            random.Random(args.blind_seed + window_number).shuffle(anonymous)
            labels = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            label_map: dict[str, str] = {}
            judge_candidates: list[dict[str, Any]] = []
            for label, (internal_id, _, candidate_plan) in zip(labels, anonymous):
                label_map[label] = internal_id
                judge_candidates.append(
                    {"candidate_id": label, "plan": candidate_plan}
                )
            try:
                judge_result, judge_telemetry = call_model(
                    base_url=args.base_url,
                    api_key=api_key,
                    model=args.model,
                    system_prompt="\n\n".join(
                        [
                            read(JUDGE_PROMPT),
                            shared_context(local_material),
                            "# OUTPUT_SCHEMA\n" + read(JUDGE_SCHEMA),
                        ]
                    ),
                    user_payload="\n\n".join(
                        [
                            local_material,
                            "# ANONYMOUS_CANDIDATES\n"
                            + json.dumps(judge_candidates, ensure_ascii=False),
                        ]
                    ),
                    timeout=args.timeout,
                    max_tokens=8192,
                    json_mode=True,
                    thinking_mode="enabled",
                    reasoning_effort="high",
                    request_attempts=2,
                )
            except (OSError, ValueError, Stage1PipelineError) as exc:
                # Local repair is optional improvement.  If its semantic judge
                # fails, preserve the frozen original and continue with later
                # windows instead of aborting or accepting a metric-only edit.
                write_json(
                    window_dir / "judge_failure.json",
                    {
                        "fallback": "keep_original",
                        "error": str(exc),
                        "model": args.model,
                    },
                )
                experiment.append(
                    {
                        "window": window_number,
                        "judge_failed": True,
                        "committed": False,
                        "reason": "semantic_judge_failed_keep_original",
                    }
                )
            else:
                write_json(window_dir / "judge.json", judge_result)
                write_json(window_dir / "judge_telemetry.json", judge_telemetry)
                winner_internal = label_map.get(str(judge_result.get("winner_id", "")))
                if (
                    winner_internal
                    and winner_internal != "original"
                    and bool(judge_result.get("materially_better"))
                    and float(judge_result.get("confidence", 0)) >= 0.7
                ):
                    winner_number = int(winner_internal.rsplit("_", 1)[1])
                    for number, _, proposed, proposed_result, after_metrics in valid_candidates:
                        if number != winner_number:
                            continue
                        no_regression = (
                            not after_metrics["hard_issues"]
                            and all(
                                after_metrics[key] <= before_metrics[key]
                                for key in (
                                    "critical",
                                "mergeable",
                                "crossed",
                                "multi_sentence",
                                "cue_count",
                                )
                            )
                        )
                        if no_regression:
                            judged_winner = (
                                number,
                                proposed,
                                proposed_result,
                            )
                        break
                experiment.append(
                    {
                        "window": window_number,
                        "judge": judge_result,
                        "winner_internal": winner_internal,
                        "accepted_by_judge": judged_winner is not None,
                    }
                )

        if judged_winner is not None:
            winner, plan, current = judged_winner
            accepted += 1
            write_json(window_dir / "accepted_plan.json", plan)
            experiment.append(
                {
                    "window": window_number,
                    "winner": winner,
                    "committed": True,
                    "acceptance": "blind_judge_non_regression",
                    "metrics": metrics(current),
                }
            )
        else:
            experiment.append(
                {
                    "window": window_number,
                    "committed": False,
                    "reason": (
                        "semantic_judge_did_not_confirm_improvement"
                        if valid_candidates
                        else "no_pareto_improvement"
                    ),
                }
            )

    write_json(args.output_dir / "stage1_local_candidate_plan.json", plan)
    (args.output_dir / "stage03A_source_draft.txt").write_text(
        current.draft,
        encoding="utf-8",
    )
    write_json(
        args.output_dir / "local_candidate_report.json",
        {
            "schema_version": "substar.stage1.local-candidates.v1",
            "key_source": key_source,
            "window_count": len(windows),
            "accepted_window_count": accepted,
            "final_metrics": metrics(current),
            "experiments": experiment,
        },
    )
    print(
        json.dumps(
            {
                "windows": len(windows),
                "accepted": accepted,
                "metrics": metrics(current),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
