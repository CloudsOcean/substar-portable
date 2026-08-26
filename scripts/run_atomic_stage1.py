from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_flash_map_pro_editor import (  # noqa: E402
    build_p1_windows,
    build_plan_from_cuts,
    cuts_from_group_breaks,
    group_batches,
    merge_p1_windows,
    normalize_p1,
    protection_for_editor,
    render_local_payload,
    render_p1_window_payload,
    select_hard_valid_local_candidate,
)
from scripts.run_stage1_pipeline import (  # noqa: E402
    Stage1PipelineError,
    _direct_report,
    call_model,
    read,
    resolve_api_key,
    source_punctuation_kwargs,
    write_json,
    write_two_level_artifacts,
)
from substar_core.stage1 import extract_alignment, extract_master  # noqa: E402
from substar_core.stage1_direct import evaluate_direct_plan  # noqa: E402
from substar_core.stage_progress import StageProgress  # noqa: E402


def units_tsv(units: list[Any]) -> str:
    return "\n".join(
        f"{u.index}\t{u.start:.3f}\t{u.end:.3f}\t{u.text}\t"
        f"{u.sentence_id if u.sentence_id is not None else '-'}\t"
        f"{1 if u.sentence_start else 0}\t{1 if u.sentence_end else 0}"
        for u in units
    )


def model_json(
    *,
    model: str,
    base_url: str,
    api_key: str,
    system: str,
    user: str,
    timeout: int,
    max_tokens: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return call_model(
        base_url=base_url,
        api_key=api_key,
        model=model,
        system_prompt=system,
        user_payload=user,
        timeout=timeout,
        max_tokens=max_tokens,
        json_mode=True,
        thinking_mode="enabled",
        reasoning_effort="high",
        request_attempts=2,
    )


def with_contract_retries(
    *,
    invoke: Callable[[str], tuple[dict[str, Any], dict[str, Any]]],
    validate: Callable[[dict[str, Any]], Any],
    attempts: int,
    progress: StageProgress | None = None,
    stage: str = "",
    block_id: str = "",
) -> tuple[Any, dict[str, Any], list[dict[str, Any]]]:
    error = ""
    telemetry: dict[str, Any] = {}
    history: list[dict[str, Any]] = []
    for number in range(attempts + 1):
        if progress is not None:
            if number:
                progress.event(
                    stage,
                    "retry",
                    block_id=block_id,
                    detail={"validation_error": error},
                )
            progress.event(stage, "sent", block_id=block_id)
        try:
            raw, telemetry = invoke(error)
        except Exception as exc:
            error = f"API 请求失败：{exc}"
            history.append(
                {"attempt": number + 1, "valid": False, "error": error}
            )
            continue
        if progress is not None:
            progress.event(stage, "response", block_id=block_id)
        try:
            normalized = validate(raw)
            history.append({"attempt": number + 1, "valid": True})
            if progress is not None:
                progress.event(stage, "accepted", block_id=block_id)
            return normalized, telemetry, history
        except Exception as exc:
            error = str(exc)
            history.append(
                {"attempt": number + 1, "valid": False, "error": error}
            )
    if progress is not None:
        progress.event(
            stage,
            "failed",
            block_id=block_id,
            detail={"validation_error": error},
        )
    raise Stage1PipelineError(
        f"局部任务初次执行及{attempts}次修复均未通过：{error}"
    )


def seam_candidates(
    units: list[Any],
    protection: dict[str, Any],
    target_seconds: int,
) -> list[list[int]]:
    forbidden = {
        cut
        for span in protection.get("spans", [])
        for cut in range(
            int(span["alignment_start"]),
            int(span["alignment_end"]),
        )
    }
    duration = float(units[-1].end)
    targets: list[float] = []
    cursor = float(target_seconds)
    while cursor < duration - target_seconds * 0.45:
        targets.append(cursor)
        cursor += target_seconds
    result: list[list[int]] = []
    for target in targets:
        ranked = sorted(
            (
                (
                    abs(float(unit.end) - target)
                    - (18 if bool(unit.sentence_end) else 0),
                    int(unit.index),
                )
                for unit in units[:-1]
                if int(unit.index) not in forbidden
                and abs(float(unit.end) - target) <= 90
            )
        )
        options = [index for _, index in ranked[:12]]
        if not options:
            raise Stage1PipelineError(f"P2 在 {target:.1f}s 附近没有安全候选")
        result.append(sorted(options))
    return result


def validate_seams(
    raw: dict[str, Any],
    candidates: list[list[int]],
) -> list[int]:
    if raw.get("schema_version") != "substar.stage1.execution-seams.v1":
        raise Stage1PipelineError("P2 schema_version 错误")
    values = raw.get("boundaries_after")
    if not isinstance(values, list) or len(values) != len(candidates):
        raise Stage1PipelineError("P2 没有为每个目标区间选择一个接缝")
    cuts = [int(value) for value in values]
    if any(cut not in options for cut, options in zip(cuts, candidates)):
        raise Stage1PipelineError("P2 返回了候选集合之外的接缝")
    if cuts != sorted(set(cuts)):
        raise Stage1PipelineError("P2 接缝没有严格递增")
    return cuts


def chunk_ranges(units: list[Any], cuts: list[int]) -> list[tuple[int, int]]:
    first = int(units[0].index)
    last = int(units[-1].index)
    result: list[tuple[int, int]] = []
    start = first
    for cut in cuts:
        result.append((start, cut))
        start = cut + 1
    result.append((start, last))
    return result


def validate_meaning_groups(
    raw: dict[str, Any],
    start: int,
    end: int,
    hard_spans: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if raw.get("schema_version") != "substar.stage1.meaning-groups.v2":
        raise Stage1PipelineError("P3A schema_version 错误")
    rows = raw.get("groups")
    if not isinstance(rows, list) or not rows:
        raise Stage1PipelineError("P3A 缺少意义组")
    cursor = start
    normalized: list[dict[str, Any]] = []
    for row in rows:
        left = int(row.get("alignment_start", -1))
        right = int(row.get("alignment_end", -1))
        if left != cursor or right < left or right > end:
            raise Stage1PipelineError(
                f"P3A 覆盖错误 expected={cursor}, got={left}-{right}"
            )
        if any(
            int(span["alignment_start"]) <= right < int(span["alignment_end"])
            for span in hard_spans
        ):
            raise Stage1PipelineError(f"P3A 在 {right} 切入 hard")
        continuity = row.get("continuity_after", {})
        normalized.append(
            {
                "alignment_start": left,
                "alignment_end": right,
                "continuity_after": {
                    "relation": str(continuity.get("relation", "related")),
                    "confidence": float(continuity.get("confidence", 0.7)),
                    "reason": str(continuity.get("reason", "")),
                },
            }
        )
        cursor = right + 1
    if cursor - 1 != end:
        raise Stage1PipelineError(
            f"P3A 末端错误 expected={end}, got={cursor - 1}"
        )
    return normalized


def raw_line_metrics(
    raw: dict[str, Any],
    groups: list[dict[str, Any]],
    units: list[Any],
    *,
    hard_limit: int = 55,
) -> list[dict[str, Any]]:
    """Measure the model's proposed lines with the product's real counter."""

    by_index = {int(unit.index): unit for unit in units}
    raw_by_id = {
        str(row.get("group_id")): row
        for row in raw.get("groups", [])
        if isinstance(row, dict)
    }
    metrics: list[dict[str, Any]] = []
    for group in groups:
        group_id = str(group["group_id"])
        start = int(group["alignment_start"])
        end = int(group["alignment_end"])
        proposed = raw_by_id.get(group_id, {}).get("line_breaks_after", [])
        cuts = sorted(
            {
                int(cut)
                for cut in proposed
                if isinstance(cut, int) and start <= cut < end
            }
        )
        left = start
        for line_number, right in enumerate(cuts + [end], start=1):
            text = " ".join(
                str(by_index[index].text)
                for index in range(left, right + 1)
            )
            length = len(text)
            metrics.append(
                {
                    "group_id": group_id,
                    "line": line_number,
                    "alignment_start": left,
                    "alignment_end": right,
                    "text": text,
                    "character_count": length,
                    "hard_limit": hard_limit,
                    "status": "ok" if length <= hard_limit else "hard_overflow",
                }
            )
            left = right + 1
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Substar 原子化并发切分流水线")
    parser.add_argument("material", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--api-key-env", default="SUBSTAR_LLM_API_KEY")
    parser.add_argument("--p1-model", required=True)
    parser.add_argument("--p2-model", required=True)
    parser.add_argument("--p3a-model", required=True)
    parser.add_argument("--p3b-model", required=True)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--p1-core-units", type=int, default=200)
    parser.add_argument("--p1-overlap-units", type=int, default=48)
    parser.add_argument("--chunk-target-seconds", type=int, default=240)
    parser.add_argument("--p3b-target-units", type=int, default=180)
    parser.add_argument("--contract-retries", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--max-tokens", type=int, default=65536)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--progress-file", type=Path)
    args = parser.parse_args()
    progress = StageProgress(args.progress_file)

    api_key, key_source = resolve_api_key(args.api_key_env)
    if not api_key:
        raise RuntimeError("未配置 Stage1 LLM API key")
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    material = read(args.material)
    master = extract_master(material)
    units = extract_alignment(material)
    first, last = int(units[0].index), int(units[-1].index)

    windows = build_p1_windows(
        units,
        core_units=args.p1_core_units,
        overlap_units=args.p1_overlap_units,
    )
    progress.plan(
        "P1",
        len(windows),
        block_ids=[f"w{number:04d}" for number in range(1, len(windows) + 1)],
    )
    p1_dir = output / "p1_windows"
    p1_dir.mkdir(exist_ok=True)

    def p1_window(number: int, window: dict[str, int]):
        final_path = p1_dir / f"window_{number:04d}.json"
        if args.resume and final_path.exists():
            return number, json.loads(final_path.read_text(encoding="utf-8"))
        payload = render_p1_window_payload(units, window)
        local = units[
            window["context_left_pos"] : window["context_right_pos"] + 1
        ]

        def invoke(error: str):
            suffix = (
                "\n\n# CONTRACT_REPAIR\n修复以下错误，仅返回完整JSON："
                + error
                if error
                else ""
            )
            return model_json(
                model=args.p1_model,
                base_url=args.base_url,
                api_key=api_key,
                system=read(PROJECT_ROOT / "prompts/04P1_Flash_全片分层保护.md")
                + suffix,
                user=payload,
                timeout=args.timeout,
                max_tokens=32000,
            )

        value, telemetry, history = with_contract_retries(
            invoke=invoke,
            validate=lambda raw: normalize_p1(
                raw,
                local,
                require_coverage=True,
                expected_coverage=(window["core_start"], window["core_end"]),
                owner_core=(window["core_start"], window["core_end"]),
            ),
            attempts=args.contract_retries,
            progress=progress,
            stage="P1",
            block_id=f"w{number:04d}",
        )
        write_json(final_path, value)
        write_json(p1_dir / f"window_{number:04d}_api.json", telemetry)
        write_json(p1_dir / f"window_{number:04d}_attempts.json", history)
        return number, value

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(args.workers, len(windows))
    ) as executor:
        p1_rows = list(
            executor.map(
                lambda item: p1_window(*item),
                enumerate(windows, start=1),
            )
        )
    p1, p1_audit = merge_p1_windows(
        [value for _, value in sorted(p1_rows)], units
    )
    write_json(output / "p1_protection.json", p1)
    write_json(output / "p1_window_merge_audit.json", p1_audit)
    progress.finish("P1")

    candidates = seam_candidates(units, p1, args.chunk_target_seconds)
    progress.plan("P2", 1, block_ids=["seams"])
    seam_payload = {
        "target_seconds": args.chunk_target_seconds,
        "candidate_sets": [
            {
                "target": number,
                "options": [
                    {
                        "after_alignment": index,
                        "time": next(
                            round(float(unit.end), 3)
                            for unit in units
                            if int(unit.index) == index
                        ),
                        "left": " ".join(
                            str(unit.text)
                            for unit in units[max(0, index - first - 8) : index - first + 1]
                        ),
                        "right": " ".join(
                            str(unit.text)
                            for unit in units[index - first + 1 : index - first + 9]
                        ),
                    }
                    for index in options
                ],
            }
            for number, options in enumerate(candidates, start=1)
        ],
        "protection": protection_for_editor(p1),
    }

    def invoke_p2(error: str):
        return model_json(
            model=args.p2_model,
            base_url=args.base_url,
            api_key=api_key,
            system=read(PROJECT_ROOT / "prompts/05P2_安全并发接缝.md")
            + (
                "\n\n# CONTRACT_REPAIR\n" + error
                if error
                else ""
            ),
            user=json.dumps(seam_payload, ensure_ascii=False),
            timeout=args.timeout,
            max_tokens=16000,
        )

    seams, p2_call, p2_attempts = with_contract_retries(
        invoke=invoke_p2,
        validate=lambda raw: validate_seams(raw, candidates),
        attempts=args.contract_retries,
        progress=progress,
        stage="P2",
        block_id="seams",
    )
    ranges = chunk_ranges(units, seams)
    write_json(
        output / "p2_execution_chunks.json",
        {
            "schema_version": "substar.stage1.execution-chunks.v1",
            "boundaries_after": seams,
            "chunks": [
                {"chunk_id": f"c{n:04d}", "alignment_start": a, "alignment_end": b}
                for n, (a, b) in enumerate(ranges, start=1)
            ],
        },
    )
    write_json(output / "p2_api_call.json", p2_call)
    write_json(output / "p2_attempts.json", p2_attempts)
    progress.finish("P2")

    positions = {int(unit.index): pos for pos, unit in enumerate(units)}
    p3a_dir = output / "p3a_chunks"
    p3a_dir.mkdir(exist_ok=True)
    p3b_dir = output / "p3b_batches"
    p3b_dir.mkdir(exist_ok=True)
    manual_review_batches: list[dict[str, Any]] = []
    progress.plan(
        "P3A",
        len(ranges),
        block_ids=[f"c{number:04d}" for number in range(1, len(ranges) + 1)],
    )
    progress.plan("P3B", 0)

    def p3b_for_groups(
        chunk_number: int,
        local_groups: list[dict[str, Any]],
    ) -> tuple[dict[str, list[int]], int]:
        local_batches = group_batches(
            local_groups, args.p3b_target_units
        )
        p3b_block_ids = [
            f"c{chunk_number:04d}b{number:03d}"
            for number in range(1, len(local_batches) + 1)
        ]
        progress.plan(
            "P3B",
            len(local_batches),
            additive=True,
            block_ids=p3b_block_ids,
        )
        selected_all: dict[str, list[int]] = {}

        def run_batch(
            batch_number: int,
            bounds: tuple[int, int],
        ) -> tuple[int, dict[str, list[int]]]:
            left, right = bounds
            core = local_groups[left : right + 1]
            payload = render_local_payload(
                master, units, local_groups, p1, left, right
            )
            hard = [
                span
                for span in p1["spans"]
                if span["protection_level"] == "hard"
                and int(span["alignment_end"])
                >= int(core[0]["alignment_start"])
                and int(span["alignment_start"])
                <= int(core[-1]["alignment_end"])
            ]

            def validate(raw: dict[str, Any]):
                if (
                    raw.get("schema_version")
                    != "substar.stage1.direct-layout.v1"
                ):
                    raise Stage1PipelineError(
                        "P3B schema_version 错误"
                    )
                wrapped = {
                    "schema_version": (
                        "substar.stage1.local-candidates.v1"
                    ),
                    "candidates": [
                        {
                            "candidate_id": "direct",
                            "groups": raw.get("groups", []),
                        }
                    ],
                    "selected_candidate_id": "direct",
                }
                try:
                    selected, _ = select_hard_valid_local_candidate(
                        wrapped,
                        core,
                        hard,
                        master=master,
                        units=units,
                        protection=p1,
                    )
                except Stage1PipelineError as exc:
                    metrics = raw_line_metrics(raw, core, units)
                    raise Stage1PipelineError(
                        f"{exc}\nLINE_METRICS="
                        + json.dumps(metrics, ensure_ascii=False)
                    ) from exc
                return selected

            def invoke(error: str):
                return model_json(
                    model=args.p3b_model,
                    base_url=args.base_url,
                    api_key=api_key,
                    system=read(
                        PROJECT_ROOT
                        / "prompts/05P3B_唯一显示切点.md"
                    )
                    + "\n\n# ACTIVE_OUTPUT_PROFILE\n"
                    + json.dumps(
                        source_punctuation_kwargs(),
                        ensure_ascii=False,
                    )
                    + (
                        "\n\n# CONTRACT_REPAIR\n" + error
                        if error
                        else ""
                    ),
                    user=payload,
                    timeout=args.timeout,
                    max_tokens=32000,
                )

            try:
                selected, telemetry, history = with_contract_retries(
                    invoke=invoke,
                    validate=validate,
                    attempts=args.contract_retries,
                    progress=progress,
                    stage="P3B",
                    block_id=f"c{chunk_number:04d}b{batch_number:03d}",
                )
            except Stage1PipelineError as exc:
                selected = {
                    str(group["group_id"]): []
                    for group in core
                }
                telemetry = {
                    "fallback": "manual_review_unsplit",
                    "error": str(exc),
                }
                history = [
                    {
                        "attempt": args.contract_retries + 2,
                        "valid": False,
                        "fallback": "manual_review_unsplit",
                    }
                ]
                manual_review_batches.append(
                    {
                        "block_id": (
                            f"c{chunk_number:04d}b{batch_number:03d}"
                        ),
                        "group_ids": [
                            str(group["group_id"]) for group in core
                        ],
                        "error": str(exc),
                    }
                )
            stem = (
                f"chunk_{chunk_number:04d}_"
                f"batch_{batch_number:03d}"
            )
            write_json(p3b_dir / f"{stem}.json", selected)
            write_json(p3b_dir / f"{stem}_api.json", telemetry)
            write_json(p3b_dir / f"{stem}_attempts.json", history)
            return batch_number, selected

        futures = [
            p3b_executor.submit(run_batch, number, bounds)
            for number, bounds in enumerate(local_batches, start=1)
        ]
        for future in concurrent.futures.as_completed(futures):
            _, selected = future.result()
            overlap = set(selected_all) & set(selected)
            if overlap:
                raise Stage1PipelineError(
                    f"P3B 局部批次所有权重叠：{sorted(overlap)}"
                )
            selected_all.update(selected)
        return selected_all, len(local_batches)

    def p3a_chunk(number: int, bounds: tuple[int, int]):
        start, end = bounds
        core = units[positions[start] : positions[end] + 1]
        context = units[
            max(0, positions[start] - 20) :
            min(len(units), positions[end] + 21)
        ]
        hard = [
            span
            for span in p1["spans"]
            if span["protection_level"] == "hard"
            and int(span["alignment_end"]) >= start
            and int(span["alignment_start"]) <= end
        ]
        payload = "\n\n".join(
            [
                f"# CORE_OWNERSHIP\n{start}..{end}",
                "# CONTEXT_ALIGNMENT\n```tsv\n" + units_tsv(context) + "\n```",
                "# P1_PROTECTION\n"
                + json.dumps(protection_for_editor(p1), ensure_ascii=False),
            ]
        )

        def invoke(error: str):
            return model_json(
                model=args.p3a_model,
                base_url=args.base_url,
                api_key=api_key,
                system=read(PROJECT_ROOT / "prompts/05P3A_局部意义组.md")
                + ("\n\n# CONTRACT_REPAIR\n" + error if error else ""),
                user=payload,
                timeout=args.timeout,
                max_tokens=32000,
            )

        value, telemetry, history = with_contract_retries(
            invoke=invoke,
            validate=lambda raw: validate_meaning_groups(raw, start, end, hard),
            attempts=args.contract_retries,
            progress=progress,
            stage="P3A",
            block_id=f"c{number:04d}",
        )
        write_json(p3a_dir / f"chunk_{number:04d}.json", {"groups": value})
        write_json(p3a_dir / f"chunk_{number:04d}_api.json", telemetry)
        write_json(p3a_dir / f"chunk_{number:04d}_attempts.json", history)
        for local_number, group in enumerate(value, start=1):
            group["group_id"] = (
                f"c{number:04d}g{local_number:04d}"
            )
        # Streaming hand-off: this execution chunk enters P3B immediately
        # after its P3A contract passes; it does not wait for sibling chunks.
        selected, batch_count = p3b_for_groups(number, value)
        return number, value, selected, batch_count

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, args.workers)
    ) as p3b_executor:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(args.workers, len(ranges))
        ) as executor:
            p3a_rows = list(
                executor.map(
                    lambda item: p3a_chunk(*item),
                    enumerate(ranges, start=1),
                )
            )
    meaning_groups: list[dict[str, Any]] = []
    selected_breaks: dict[str, list[int]] = {}
    p3b_batch_count = 0
    for _, rows, breaks, batch_count in sorted(p3a_rows):
        meaning_groups.extend(rows)
        overlap = set(selected_breaks) & set(breaks)
        if overlap:
            raise Stage1PipelineError(
                f"P3B 执行块所有权重叠：{sorted(overlap)}"
            )
        selected_breaks.update(breaks)
        p3b_batch_count += batch_count
    progress.finish("P3A")
    progress.finish("P3B", with_review=bool(manual_review_batches))
    write_json(
        output / "p3b_manual_review.json",
        {
            "schema_version": "substar.stage1.manual-review.v1",
            "blocks": manual_review_batches,
        },
    )
    meaning_plan = {
        "schema_version": "substar.stage1.meaning-groups.v2",
        "groups": meaning_groups,
    }
    write_json(output / "stage1_translation_group_plan.json", meaning_plan)
    write_json(output / "p3a_meaning_groups.json", meaning_plan)

    cuts = cuts_from_group_breaks(meaning_groups, selected_breaks, last)
    plan = build_plan_from_cuts(cuts, units, p1, meaning_groups)
    result = evaluate_direct_plan(
        master,
        units,
        plan,
        review_confidence=0.72,
        **source_punctuation_kwargs(),
    )
    allowed_manual_codes = {
        "draft_english_over_55",
        "draft_visual_width_over_hard_limit",
    }
    issue_codes = {
        str(issue.get("code", ""))
        for issue in result.issues
        if isinstance(issue, dict)
    }
    if not result.valid and (
        not manual_review_batches
        or not issue_codes.issubset(allowed_manual_codes)
    ):
        raise Stage1PipelineError(f"P3B 全片硬校验失败：{result.issues[:20]}")
    write_json(output / "stage1_direct_plan.json", plan)
    write_json(output / "stage1_display_layout_plan.json", plan)
    write_two_level_artifacts(output, master, units, plan)
    (output / "stage03A_source_draft.txt").write_text(
        result.draft, encoding="utf-8"
    )
    write_json(
        output / "stage1_validation.json",
        _direct_report(result, repaired=False, attempts=0),
    )
    write_json(
        output / "atomic_stage_manifest.json",
        {
            "schema_version": "substar.atomic-stage1.v1",
            "models": {
                "P1": args.p1_model,
                "P2": args.p2_model,
                "P3A": args.p3a_model,
                "P3B": args.p3b_model,
            },
            "p1_windows": len(windows),
            "p2_chunks": len(ranges),
            "p3a_groups": len(meaning_groups),
            "p3b_batches": p3b_batch_count,
            "contract_retries": args.contract_retries,
            "key_source": key_source,
        },
    )
    print(f"complete cues={len(plan['groups'])}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
