from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import re
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_flash_map_pro_editor import build_plan_from_cuts  # noqa: E402
from scripts.segmentation_support import (  # noqa: E402
    SegmentationError,
    _direct_report,
    call_model,
    resolve_api_key,
    write_two_level_artifacts,
)
from substar_core.artifacts import atomic_write_json, atomic_write_text  # noqa: E402
from substar_core.contracts.editor_document import (  # noqa: E402
    build_editor_document,
    source_tokens_from_asr,
)
from substar_core.domain import (  # noqa: E402
    ChangeKind,
    ChangeProvenance,
)
from substar_core.glossary import active_glossary, glossary_prompt  # noqa: E402
from substar_core.prompt_registry import (  # noqa: E402
    normalize_source_language,
    render_prompt,
    source_language_for_units,
)
from substar_core.language_layout import layout_tokens  # noqa: E402
from substar_core.storage import ProjectStore  # noqa: E402
from substar_core.segmentation.input_contract import load_segmentation_material  # noqa: E402
from substar_core.segmentation.execution_planner import (  # noqa: E402
    DEFAULT_MAXIMUM_SECONDS,
    DEFAULT_MINIMUM_SECONDS,
    execution_block_plan,
)
from substar_core.segmentation.validation import evaluate_direct_plan  # noqa: E402
from substar_core.stage_progress import StageProgress  # noqa: E402


ROUTES = {"semantic"}
_RUNTIME_PRINT_LOCK = threading.Lock()


def emit_runtime_event(event: str, payload: Any | None = None) -> None:
    record = {"event": event, "payload": payload}
    with _RUNTIME_PRINT_LOCK:
        print(
            "SUBSTAR_RUNTIME_EVENT\t"
            + json.dumps(record, ensure_ascii=False, separators=(",", ":")),
            flush=True,
        )


FUNCTION_END = {
    "a", "an", "the", "this", "that", "these", "those", "my", "your",
    "his", "her", "its", "our", "their", "of", "in", "on", "at", "from",
    "by", "with", "for", "into", "onto", "through", "across", "under",
    "over", "between", "among", "against", "without", "within", "about",
    "and", "or", "but", "nor", "to", "not", "never", "no", "am", "is",
    "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "can", "could", "will", "would", "shall",
    "should", "may", "might", "must", "who", "whom", "whose", "which",
    "where", "when", "because", "although", "though", "if", "unless",
}
WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?|\d+(?:[.,]\d+)?")
TERMINAL_RE = re.compile(r"[.!?。！？][\"'”’)]*$")
SEMANTIC_VIEW_TRAILING_PUNCTUATION_RE = re.compile(r"[.,?!;:]+$")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def unit_text(unit: Any) -> str:
    return str(unit.text).strip()


def last_word(text: str) -> str:
    found = WORD_RE.findall(text)
    return found[-1].lower().replace("’", "'") if found else ""


def display_text(units: list[Any], left: int, right: int) -> str:
    return layout_tokens([unit_text(unit) for unit in units[left : right + 1]])


def finalize_display_cuts(
    local_cuts: set[int], execution_seams: list[int], *, final_alignment_index: int
) -> set[int]:
    """Keep every independent model-call seam as a final Cue boundary.

    Semantic grouping validates each execution block in isolation, so neither
    adjacent call has authorized a Cue that crosses their shared seam.  Losing
    that seam can re-join two individually valid Cue fragments and create a
    post-validation hard-limit violation.
    """
    return {
        int(value)
        for value in [*local_cuts, *execution_seams]
        if int(value) < int(final_alignment_index)
    }


def frozen_layout_kwargs(args: Any) -> dict[str, Any]:
    """Return only task-frozen layout constraints for validation and fallback."""
    return {
        "baseline_punctuation": "preserve",
        "raised_punctuation": "preserve",
        "source_language": args.source_language,
        "english_hard_limit": int(args.english_hard_limit),
        "chinese_hard_limit": int(args.chinese_hard_limit),
        "mixed_hard_limit": int(args.mixed_hard_limit),
        "japanese_hard_limit": int(args.japanese_hard_limit),
        "korean_hard_limit": int(args.korean_hard_limit),
        "english_count_spaces": True,
        "english_count_punctuation": True,
        "minimum_cue_duration_ms": 400,
    }


def boundary_gap(units: list[Any], position: int) -> float:
    if position < 0 or position + 1 >= len(units):
        return 0.0
    return max(0.0, float(units[position + 1].start) - float(units[position].end))


def speaker_change(units: list[Any], position: int) -> bool:
    if position < 0 or position + 1 >= len(units):
        return False
    left, right = units[position], units[position + 1]
    return bool(
        left.speaker_id
        and right.speaker_id
        and left.speaker_id != right.speaker_id
        and min(float(left.speaker_confidence), float(right.speaker_confidence)) >= 0.75
    )


def must_not_split(units: list[Any], position: int) -> bool:
    if position < 0 or position + 1 >= len(units):
        return False
    left = unit_text(units[position])
    right = unit_text(units[position + 1])
    if not left or not right or TERMINAL_RE.search(left):
        return False
    return last_word(left) in FUNCTION_END


def boundary_quality(units: list[Any], position: int) -> float:
    unit = units[position]
    score = 0.0
    if bool(unit.sentence_end):
        score -= 18.0
    if TERMINAL_RE.search(unit_text(unit)):
        score -= 12.0
    score -= min(boundary_gap(units, position), 2.0) * 12.0
    if speaker_change(units, position):
        score -= 100.0
    if must_not_split(units, position):
        score += 1000.0
    return score


def balanced_target_times(units: list[Any], maximum_seconds: int) -> list[float]:
    """Evenly distribute the minimum number of groups capped at five minutes."""
    if not units:
        return []
    start = float(units[0].start)
    finish = float(units[-1].end)
    duration = max(0.0, finish - start)
    limit = max(1.0, float(maximum_seconds))
    if duration <= limit:
        return []
    group_count = int(math.ceil(duration / limit))
    group_seconds = duration / group_count
    return [
        round(start + group_seconds * number, 3)
        for number in range(1, group_count)
    ]


def direct_seam_request(
    units: list[Any], target_times: list[float], context_seconds: float = 30.0,
    *, sentence_boundary_policy: str = "reference",
) -> dict[str, Any]:
    """Give ExecutionPlanning timed transcript windows, not program-ranked seam candidates."""
    targets: list[dict[str, Any]] = []
    for number, target in enumerate(target_times, start=1):
        rows = [
            {
                "index": int(unit.index),
                "start": float(unit.start),
                "end": float(unit.end),
                "text": unit_text(unit),
                "speaker_id": unit.speaker_id,
            }
            for unit in units
            if float(unit.end) >= target - context_seconds
            and float(unit.start) <= target + context_seconds
        ]
        targets.append(
            {
                "target_id": f"seam{number:04d}",
                "target_time": target,
                "context_seconds_each_side": context_seconds,
                "allowed_after_alignment": [
                    int(row["index"])
                    for row in rows
                    if int(row["index"]) != int(units[-1].index)
                    and target - context_seconds <= float(row["end"])
                    <= target + context_seconds
                ],
                "rows": rows,
            }
        )
    return {
        "duration_seconds": round(
            float(units[-1].end) - float(units[0].start), 3
        ),
        "targets": targets,
    }


def cached_semantic_grouping_responses(path: Path | None) -> dict[str, dict[str, Any]]:
    """Recover completed merged responses from a previous interrupted stdout log."""
    found: dict[str, dict[str, Any]] = {}
    if path is None or not path.is_file():
        return found
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw_line.startswith("SUBSTAR_RUNTIME_EVENT\t"):
            continue
        try:
            record = json.loads(raw_line.split("\t", 1)[1])
            event = str(record.get("event", ""))
            payload = record.get("payload") or {}
            if not event.endswith(" API response"):
                continue
            if not event.startswith("semantic_grouping "):
                continue
            block_id = str(payload.get("block_id") or event.split()[1])
            output = payload.get("output")
            if isinstance(output, dict):
                found[block_id] = output
        except (TypeError, ValueError, json.JSONDecodeError, IndexError):
            continue
    return found


def program_direct_seams(units: list[Any], target_times: list[float]) -> list[int]:
    from substar_core.segmentation.execution_planner import plan_execution_seams

    forbidden = {
        int(units[position].index)
        for position in range(max(0, len(units) - 1))
        if must_not_split(units, position)
    }
    target_seconds = (
        max(1.0, float(units[-1].end) - float(units[0].start))
        if not target_times
        else max(
            1.0,
            (float(units[-1].end) - float(units[0].start))
            / (len(target_times) + 1),
        )
    )
    seams, _evidence = plan_execution_seams(
        units,
        target_seconds=target_seconds,
        minimum_seconds=DEFAULT_MINIMUM_SECONDS,
        maximum_seconds=DEFAULT_MAXIMUM_SECONDS,
        forbidden_after=forbidden,
    )
    return seams


def chunk_ranges(units: list[Any], seams: list[int]) -> list[tuple[int, int]]:
    positions = {int(unit.index): pos for pos, unit in enumerate(units)}
    ranges: list[tuple[int, int]] = []
    left = 0
    for seam in seams:
        right = positions[seam]
        ranges.append((left, right))
        left = right + 1
    ranges.append((left, len(units) - 1))
    return [row for row in ranges if row[0] <= row[1]]


def planning_skip_reason(units: list[Any], target_seconds: int) -> str:
    """Return why seam selection needs no model call, or an empty string."""
    if not units:
        return "empty_alignment"
    duration = max(0.0, float(units[-1].end) - float(units[0].start))
    if duration <= float(target_seconds):
        return "duration_at_or_below_target"
    return ""


def model_json(
    *, model: str, base_url: str, api_key: str, system: str, user: Any, timeout: int,
    auth_mode: str = "bearer",
    telemetry: list[dict[str, Any]] | None = None, stage: str = "", block_id: str = "",
    thinking_mode: str = "enabled", reasoning_effort: str = "low",
    max_tokens: int = 131072, temperature: float = 0.0,
) -> dict[str, Any]:
    value: dict[str, Any] | None = None
    budgets = [max_tokens] + [value for value in (131072, 262144, 393216) if value > max_tokens]
    for output_budget in budgets:
        try:
            emit_runtime_event(
                f"{stage} {block_id} API request",
                {
                    "model": model,
                    "maxTokens": output_budget,
                    "thinkingMode": thinking_mode,
                    "reasoningEffort": reasoning_effort if thinking_mode == "enabled" else None,
                    "input": user,
                },
            )
            value, call_info = call_model(
                base_url=base_url,
                api_key=api_key,
                auth_mode=auth_mode,
                model=model,
                system_prompt=system,
                user_payload=json.dumps(user, ensure_ascii=False),
                timeout=timeout,
                max_tokens=output_budget,
                json_mode=False,
                thinking_mode=thinking_mode,
                reasoning_effort=reasoning_effort,
                temperature=temperature,
                request_attempts=2,
            )
            if telemetry is not None:
                telemetry.append({"stage": stage, "block_id": block_id, **call_info})
            emit_runtime_event(
                f"{stage} {block_id} API response",
                {"output": value, "telemetry": call_info},
            )
            break
        except SegmentationError as exc:
            if "截断" not in str(exc) or output_budget == 393216:
                raise
    if value is None:
        raise SegmentationError("API 未返回有效 JSON")
    if not isinstance(value, dict):
        raise SegmentationError("API 顶层必须返回 JSON object")
    return value


def units_payload(
    units: list[Any], left: int, right: int, context: int = 20,
    *, sentence_boundary_policy: str = "reference",
) -> dict[str, Any]:
    context_left = max(0, left - context)
    context_right = min(len(units) - 1, right + context)
    return {
        "core_ownership": [int(units[left].index), int(units[right].index)],
        "rows": [
            {
                "index": int(unit.index),
                "start": float(unit.start),
                "end": float(unit.end),
                "text": unit_text(unit),
                "speaker_id": unit.speaker_id,
                "owner": left <= position <= right,
            }
            for position, unit in enumerate(units[context_left : context_right + 1], start=context_left)
        ],
    }


def normalize_exception_rows(value: Any) -> list[dict[str, Any]]:
    """Keep model exception details while enforcing the internal row shape."""
    if value is None:
        return []
    raw_rows = value if isinstance(value, list) else [value]
    return [
        row
        if isinstance(row, dict)
        else {"code": "MODEL_EXCEPTION", "detail": str(row)}
        for row in raw_rows
    ]


def semantic_grouping_binding(
    units: list[Any], left: int, right: int, chunk_number: int, *,
    sentence_boundary_policy: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = units_payload(
        units, left, right, sentence_boundary_policy=sentence_boundary_policy
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    binding = {
        "input_fingerprint": fingerprint,
        "block_id": f"c{chunk_number:04d}",
        "ownership": {
            "alignment_start": int(units[left].index),
            "alignment_end": int(units[right].index),
        },
    }
    return payload, binding


def validate_semantic_grouping_result(
    value: dict[str, Any], units: list[Any], bounds: tuple[int, int],
    chunk_number: int, binding: dict[str, Any], hard_limit: int,
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
    set[int], list[dict[str, Any]],
]:
    left, right = bounds
    expected_keys = {
        "schema_version", "input_fingerprint", "block_id", "ownership",
        "meaning_groups", "exceptions",
    }
    if set(value) != expected_keys:
        raise SegmentationError("semantic grouping 返回字段必须与冻结契约完全一致")
    if value.get("schema_version") != "substar.semantic-grouping-result.v1":
        raise SegmentationError("semantic grouping schema_version 错误")
    for field in ("input_fingerprint", "block_id", "ownership"):
        if value.get(field) != binding[field]:
            raise SegmentationError(f"semantic grouping {field} 与当前输入不匹配")

    raw_groups = value.get("meaning_groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise SegmentationError("semantic grouping 缺少意义组")
    first = int(units[left].index)
    last = int(units[right].index)
    positions = {int(unit.index): position for position, unit in enumerate(units)}
    groups: list[dict[str, Any]] = []
    cuts: set[int] = set()
    expected_start = first
    for number, raw in enumerate(raw_groups, start=1):
        if not isinstance(raw, dict) or set(raw) != {
            "alignment_start", "alignment_end", "line_breaks_after"
        }:
            raise SegmentationError("semantic grouping 意义组字段错误")
        start = int(raw["alignment_start"])
        end = int(raw["alignment_end"])
        if start != expected_start or not start <= end <= last:
            raise SegmentationError("semantic grouping 未连续完整覆盖所属区间")
        breaks = [int(item) for item in raw["line_breaks_after"]]
        if not breaks or breaks != sorted(set(breaks)) or breaks[-1] != end:
            raise SegmentationError("semantic grouping Cue 边界必须递增并以组尾结束")
        if any(item < start or item > end for item in breaks):
            raise SegmentationError("semantic grouping Cue 边界越过意义组")
        cue_start = start
        for cue_end in breaks:
            if cue_start not in positions or cue_end not in positions:
                raise SegmentationError("semantic grouping 引用了不存在的 alignment index")
            text = display_text(units, positions[cue_start], positions[cue_end])
            if len(text) > hard_limit:
                matching_overflow = any(
                    isinstance(item, dict)
                    and item.get("code") == "indivisible_overflow"
                    and int(item.get("alignment_start", -1)) <= cue_start
                    and int(item.get("alignment_end", -1)) >= cue_end
                    for item in value.get("exceptions", [])
                )
                if not matching_overflow:
                    legal_candidates = []
                    for candidate in range(cue_start, cue_end):
                        left_text = display_text(
                            units, positions[cue_start], positions[candidate]
                        )
                        right_text = display_text(
                            units, positions[candidate + 1], positions[cue_end]
                        )
                        if len(left_text) <= hard_limit and len(right_text) <= hard_limit:
                            legal_candidates.append(
                                {
                                    "line_break_after": candidate,
                                    "left_length": len(left_text),
                                    "right_length": len(right_text),
                                }
                            )
                    raise SegmentationError(
                        "semantic grouping Cue "
                        f"alignment {cue_start}-{cue_end} 长度 {len(text)} "
                        f"超过硬上限 {hard_limit}；原文={json.dumps(text, ensure_ascii=False)}。"
                        f"请在 {cue_start}-{max(cue_start, cue_end - 1)} 内选择一个或多个"
                        "合法自然边界加入 line_breaks_after；"
                        "满足左右两段均不超限的候选="
                        f"{json.dumps(legal_candidates, ensure_ascii=False)}；"
                        "候选只表示长度合法，仍须由你依据语义和语法选择；"
                        "不得删除、改写或重排词元。"
                    )
            if cue_end < last:
                cuts.add(cue_end)
            cue_start = cue_end + 1
        groups.append({
            "group_id": f"c{chunk_number:04d}g{number:04d}",
            "alignment_start": start,
            "alignment_end": end,
        })
        expected_start = end + 1
    if expected_start != last + 1:
        raise SegmentationError("semantic grouping 未覆盖所属区间尾部")

    allowed_exception_codes = {
        "indivisible_overflow", "source_timing_conflict", "speaker_boundary_conflict"
    }
    exceptions = normalize_exception_rows(value.get("exceptions", []))
    for row in exceptions:
        if row.get("code") not in allowed_exception_codes:
            raise SegmentationError("semantic grouping exception code 不在冻结集合中")
        if not {"code", "alignment_start", "alignment_end", "detail"} <= set(row):
            raise SegmentationError("semantic grouping exception 字段不完整")
        exception_start = int(row["alignment_start"])
        exception_end = int(row["alignment_end"])
        if not first <= exception_start <= exception_end <= last:
            raise SegmentationError("semantic grouping exception 越过所属区间")
    return [], groups, [], cuts, exceptions


def _salvage_semantic_groups(
    value: Any,
    units: list[Any],
    bounds: tuple[int, int],
    chunk_number: int,
    binding: Mapping[str, Any],
    hard_limit: int,
) -> tuple[list[dict[str, Any]], set[int]]:
    """Accept only independently valid model groups; never invent their content."""
    if not isinstance(value, Mapping):
        return [], set()
    if value.get("schema_version") != "substar.semantic-grouping-result.v1":
        return [], set()
    if any(value.get(field) != binding[field] for field in ("input_fingerprint", "block_id", "ownership")):
        return [], set()
    raw_groups = value.get("meaning_groups")
    if not isinstance(raw_groups, list):
        return [], set()
    left, right = bounds
    first, last = int(units[left].index), int(units[right].index)
    positions = {int(unit.index): position for position, unit in enumerate(units)}
    candidates: list[tuple[int, int, list[int]]] = []
    for raw in raw_groups:
        try:
            if not isinstance(raw, Mapping) or set(raw) != {
                "alignment_start", "alignment_end", "line_breaks_after"
            }:
                continue
            start, end = int(raw["alignment_start"]), int(raw["alignment_end"])
            breaks = [int(item) for item in raw["line_breaks_after"]]
            if not first <= start <= end <= last:
                continue
            if not breaks or breaks != sorted(set(breaks)) or breaks[-1] != end:
                continue
            if any(item < start or item > end for item in breaks):
                continue
            cue_start = start
            valid = True
            for cue_end in breaks:
                if cue_start not in positions or cue_end not in positions:
                    valid = False
                    break
                if len(display_text(units, positions[cue_start], positions[cue_end])) > hard_limit:
                    valid = False
                    break
                cue_start = cue_end + 1
            if valid:
                candidates.append((start, end, breaks))
        except (TypeError, ValueError):
            continue
    groups: list[dict[str, Any]] = []
    cuts: set[int] = set()
    previous_end = first - 1
    for start, end, breaks in sorted(candidates):
        if start <= previous_end:
            continue
        groups.append({
            "group_id": f"c{chunk_number:04d}g{start:06d}",
            "alignment_start": start,
            "alignment_end": end,
        })
        cuts.update(item for item in breaks if item < last)
        previous_end = end
    return groups, cuts


def _merge_frozen_groups(
    frozen: list[dict[str, Any]], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    result = list(frozen)
    for candidate in candidates:
        start, end = int(candidate["alignment_start"]), int(candidate["alignment_end"])
        if any(
            not (end < int(row["alignment_start"]) or start > int(row["alignment_end"]))
            for row in result
        ):
            continue
        result.append(candidate)
    return sorted(result, key=lambda row: int(row["alignment_start"]))


def _groups_cover(groups: list[dict[str, Any]], first: int, last: int) -> bool:
    cursor = first
    for group in sorted(groups, key=lambda row: int(row["alignment_start"])):
        if int(group["alignment_start"]) != cursor:
            return False
        cursor = int(group["alignment_end"]) + 1
    return cursor == last + 1


def request_semantic_grouping_block(
    units: list[Any], bounds: tuple[int, int], chunk_number: int, args: Any,
    system_prompt: str, glossary: list[dict[str, Any]],
    progress: StageProgress | None = None,
    cached_value: dict[str, Any] | None = None,
    *,
    repair_prompt: str | None = None,
) -> tuple[
    int,
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    set[int],
    list[dict[str, Any]],
]:
    """Run semantic grouping and Cue layout with a strict input-bound contract."""
    left, right = bounds
    block_id = f"c{chunk_number:04d}"
    payload, binding = semantic_grouping_binding(
        units, left, right, chunk_number,
        sentence_boundary_policy=getattr(args, "sentence_boundary_policy", "reference"),
    )
    request = {
        **payload,
        "result_binding": binding,
        "active_output_profile": {
            "source_language": getattr(args, "source_language", "en"),
            "hard_limit": args.hard_limit,
            "count_rule": "all_unicode_characters",
        },
    }
    initial_error: Exception | None = None
    try:
        value = cached_value or model_json(
            model=args.grouping_model,
            base_url=args.base_url,
            api_key=args.api_key,
            auth_mode=args.auth_mode,
            system=system_prompt + "\n\n" + glossary_prompt(glossary or []),
            user=request,
            timeout=args.timeout,
            telemetry=args.api_telemetry,
            stage="semantic_grouping",
            block_id=block_id,
            thinking_mode=args.grouping_thinking_mode,
            reasoning_effort=args.grouping_reasoning_effort,
            max_tokens=args.grouping_max_tokens,
            temperature=args.grouping_temperature,
        )
    except Exception as exc:
        # Invalid JSON from the primary call is a repairable block failure,
        # not a fatal pipeline failure. Keep the block in the repair loop.
        initial_error = exc
        value = {}
        emit_runtime_event(
            f"semantic grouping {block_id} 主响应无效，转入结构修复",
            {"stage": "semantic_grouping", "block_id": block_id, "error": str(exc)},
        )
    if cached_value is None:
        if progress is not None:
            progress.event("semantic_grouping", "response", block_id=block_id)
    else:
        emit_runtime_event(
            f"semantic grouping {block_id} 已复用中断前响应",
            {"stage": "semantic_grouping", "block_id": block_id},
        )
    last_error: Exception | None = initial_error
    frozen_groups: list[dict[str, Any]] = []
    frozen_cuts: set[int] = set()
    first, last = int(units[left].index), int(units[right].index)
    repair_attempts = int(getattr(args, "repair_attempts", 0))
    for attempt in range(0, repair_attempts + 1):
        try:
            if frozen_groups:
                raise SegmentationError("repair response must preserve previously accepted groups")
            spans, groups, corrections, cuts, exceptions = validate_semantic_grouping_result(
                value, units, bounds, chunk_number, binding, args.hard_limit
            )
            if progress is not None:
                progress.event("semantic_grouping", "accepted", block_id=block_id)
            if attempt:
                if progress is not None:
                    progress.event("semantic_grouping_repair", "accepted", block_id=block_id)
                emit_runtime_event(
                    f"切分修复 {block_id} 验收通过",
                    {"stage": "semantic_grouping_repair", "block_id": block_id, "attempt": attempt},
                )
            return chunk_number, spans, groups, corrections, cuts, exceptions
        except Exception as exc:
            last_error = exc
            candidates, candidate_cuts = _salvage_semantic_groups(
                value, units, bounds, chunk_number, binding, args.hard_limit
            )
            frozen_groups = _merge_frozen_groups(frozen_groups, candidates)
            frozen_cuts.update(candidate_cuts)
            if _groups_cover(frozen_groups, first, last):
                return chunk_number, [], frozen_groups, [], frozen_cuts, []
            if attempt >= repair_attempts:
                break
            repair_attempt = attempt + 1
            if progress is not None:
                progress.plan("semantic_grouping_repair", 1, additive=True, block_ids=[block_id])
                progress.event(
                    "semantic_grouping", "retry", block_id=block_id,
                    detail={"validation_error": str(exc)},
                )
                progress.event("semantic_grouping_repair", "sent", block_id=block_id)
            emit_runtime_event(
                f"切分修复 {block_id} 已发送",
                {
                    "stage": "semantic_grouping_repair", "block_id": block_id,
                    "attempt": repair_attempt, "validation_error": str(exc),
                },
            )
            try:
                value = model_json(
                    model=getattr(args, "repair_model", "") or args.grouping_model,
                    base_url=args.base_url,
                    api_key=args.api_key,
                    auth_mode=args.auth_mode,
                    system=(repair_prompt or system_prompt) + "\n\n" + glossary_prompt(glossary or []),
                    user={
                        **request,
                        "rejected_output": value,
                        "program_validation_error": str(exc),
                        "accepted_groups_frozen": frozen_groups,
                        "repair_attempt": repair_attempt,
                    },
                    timeout=args.timeout,
                    telemetry=args.api_telemetry,
                    stage="semantic_grouping_repair",
                    block_id=block_id,
                    thinking_mode=getattr(args, "repair_thinking_mode", "disabled"),
                    reasoning_effort=getattr(args, "repair_reasoning_effort", "low"),
                    max_tokens=getattr(args, "repair_max_tokens", 65536),
                    temperature=getattr(args, "repair_temperature", 0.0),
                )
            except Exception as repair_exc:
                # A malformed repair response is also recoverable. Leave an
                # empty candidate for the next validation/final-release pass.
                last_error = repair_exc
                value = {}
                emit_runtime_event(
                    f"切分修复 {block_id} 返回无效 JSON，继续保证交付",
                    {
                        "stage": "semantic_grouping_repair",
                        "block_id": block_id,
                        "attempt": repair_attempt,
                        "error": str(repair_exc),
                    },
                )
            if progress is not None:
                progress.event("semantic_grouping_repair", "response", block_id=block_id)
            emit_runtime_event(
                f"切分修复 {block_id} 已返回",
                {"stage": "semantic_grouping_repair", "block_id": block_id, "attempt": repair_attempt},
            )
    # Delivery policy: preserve successful blocks exactly. An unresolved block
    # becomes one structurally contained problem Cue; the program must not
    # invent semantic boundaries that compete with the model.
    local_units = units[left : right + 1]
    groups = list(frozen_groups)
    cuts: set[int] = set(frozen_cuts)
    exceptions: list[dict[str, Any]] = []
    cursor = first
    for group in [*groups, {"alignment_start": last + 1, "alignment_end": last}]:
        gap_end = int(group["alignment_start"]) - 1
        if cursor <= gap_end:
            groups.append({
                "group_id": f"c{chunk_number:04d}problem{cursor:06d}",
                "alignment_start": cursor,
                "alignment_end": gap_end,
            })
            if gap_end < int(units[-1].index):
                cuts.add(gap_end)
            exceptions.append({
                "code": "semantic_grouping_unresolved",
                "block_id": block_id,
                "alignment_start": cursor,
                "alignment_end": gap_end,
                "detail": "首次切分与一次精确修复均未通过结构校验；保留为问题字幕。",
            })
        cursor = max(cursor, int(group["alignment_end"]) + 1)
    groups.sort(key=lambda row: int(row["alignment_start"]))

    fallback_audit = {
        "schema_version": "substar.segmentation-fallback.v1",
        "block_id": block_id,
        "alignment_start": int(local_units[0].index),
        "alignment_end": int(local_units[-1].index),
        "repair_attempts": repair_attempts,
        "validation_error": str(last_error),
        "accepted_group_count": len(frozen_groups),
        "problem_cue_count": len(exceptions),
    }
    atomic_write_json(
        args.output_dir / f"semantic_grouping_fallback_{block_id}.json",
        fallback_audit,
    )
    if progress is not None:
        progress.event(
            "semantic_grouping",
            "failed",
            block_id=block_id,
            detail={
                "delivery": "problem_subtitle",
                "validation_error": str(last_error),
                "review_cue_count": len(exceptions),
            },
        )
        if repair_attempts:
            progress.event(
                "semantic_grouping_repair",
                "failed",
                block_id=block_id,
                detail={"delivery": "problem_subtitle"},
            )
    emit_runtime_event(
        f"切分块 {block_id} 已登记为问题字幕并继续交付",
        {
            "stage": "semantic_grouping",
            "block_id": block_id,
            "problemCueCount": len(exceptions),
            "repairAttempts": repair_attempts,
        },
    )
    return chunk_number, [], groups, [], cuts, exceptions


def main(
    argv: list[str] | None = None,
    *,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Substar deterministic planning and semantic grouping")
    parser.add_argument("material", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--route", required=True, choices=sorted(ROUTES))
    parser.add_argument("--base-url", default="")
    parser.add_argument("--auth-mode", choices=["bearer", "api-key"], default="bearer")
    parser.add_argument("--grouping-model", required=True)
    parser.add_argument(
        "--grouping-thinking-mode", choices=["enabled", "disabled"], default="disabled"
    )
    parser.add_argument(
        "--grouping-reasoning-effort",
        choices=["low", "medium", "high", "max", "xhigh"],
        default="low",
    )
    parser.add_argument("--grouping-max-tokens", type=int, default=131072)
    parser.add_argument("--grouping-temperature", type=float, default=0.0)
    parser.add_argument("--repair-attempts", type=int, default=1)
    parser.add_argument("--repair-model", default="")
    parser.add_argument("--repair-thinking-mode", choices=["enabled", "disabled"], default="disabled")
    parser.add_argument("--repair-reasoning-effort", choices=["low", "medium", "high", "max", "xhigh"], default="low")
    parser.add_argument("--repair-max-tokens", type=int, default=65536)
    parser.add_argument("--repair-temperature", type=float, default=0.0)
    parser.add_argument("--resume-response-log", type=Path)
    parser.add_argument("--target-seconds", type=int, default=180)
    parser.add_argument("--english-hard-limit", type=int, default=55)
    parser.add_argument("--chinese-hard-limit", type=int, default=24)
    parser.add_argument("--mixed-hard-limit", type=int, default=25)
    parser.add_argument("--japanese-hard-limit", type=int, default=25)
    parser.add_argument("--korean-hard-limit", type=int, default=32)
    parser.add_argument("--target-hard-limit", type=int, default=24)
    parser.add_argument("--source-language", default="Auto")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--progress-file", type=Path)
    parser.add_argument("--project-store-dir", type=Path)
    parser.add_argument("--project-id", default="")
    parser.add_argument(
        "--candidate-only",
        action="store_true",
        help="Write a validated editor-document candidate without committing project state.",
    )
    parser.add_argument(
        "--validate-material-only",
        action="store_true",
        help="Validate the frozen Worker material contract and exit without provider access.",
    )
    parser.add_argument(
        "--glossary-snapshot",
        type=Path,
        help="Use the task-frozen glossary instead of reading mutable application settings.",
    )
    parser.add_argument("--source-kind", required=True, choices=["asr"])
    parser.add_argument("--source-asset-id", required=True)
    parser.add_argument(
        "--sentence-boundary-policy",
        choices=["reference", "reconstruct", "unpunctuated"],
        default="unpunctuated",
        help="Treat native ASR sentence boundaries as soft references or hide them.",
    )
    args = parser.parse_args(argv)
    # The production command may deliver a reviewable draft after model repair
    # by removing only illegal intermediate display breaks. Direct callers and
    # validators stay strict unless they opt into this final-delivery policy.
    args.allow_deterministic_release = True

    emit_runtime_event("segmentation initialization", {"step": "read_material"})
    master, units = load_segmentation_material(args.material)
    if not units:
        raise SegmentationError("alignment 为空")
    if args.validate_material_only:
        emit_runtime_event(
            "segmentation material validated",
            {"unit_count": len(units), "source_character_count": len(master)},
        )
        return 0
    args.output_dir.mkdir(parents=True, exist_ok=True)
    progress = StageProgress(args.progress_file, on_update=progress_callback)
    source_language = normalize_source_language(args.source_language, units)
    prompt_variant = {
        "zh-CN": "zh",
        "en": "en",
        "ja": "ja",
        "ko": "ko",
        "mixed": "mixed",
    }.get(source_language, "en")
    language_limits = {
        "zh-CN": args.chinese_hard_limit,
        "en": args.english_hard_limit,
        "ja": args.japanese_hard_limit,
        "ko": args.korean_hard_limit,
        "mixed": args.mixed_hard_limit,
    }
    args.source_language = source_language
    args.hard_limit = int(language_limits.get(source_language, args.english_hard_limit))
    # The internal layout solver still accepts its former target parameter as
    # a heuristic.  It is equal to the hard cap, so no advisory/soft product
    # threshold remains in the active contract.
    args.soft_limit = args.hard_limit
    emit_runtime_event(
        "segmentation initialization",
        {"step": "render_prompts", "unit_count": len(units)},
    )
    semantic_grouping_prompt = render_prompt("semantic_grouping", variant=prompt_variant)
    semantic_grouping_repair_prompt = render_prompt(
        "semantic_grouping_repair", variant=prompt_variant
    )
    boundary_evidence_prompt = render_prompt(
        "boundary_evidence", variant=args.sentence_boundary_policy
    )
    grouping_system_prompt = (
        semantic_grouping_prompt.text + "\n\n" + boundary_evidence_prompt.text
    )
    repair_system_prompt = (
        semantic_grouping_repair_prompt.text + "\n\n" + boundary_evidence_prompt.text
    )
    rule_profile = grouping_system_prompt
    if args.glossary_snapshot is not None:
        raw_glossary = json.loads(read(args.glossary_snapshot))
        if not isinstance(raw_glossary, list) or any(
            not isinstance(item, dict) for item in raw_glossary
        ):
            raise SegmentationError("glossary snapshot contract is invalid")
        glossary = raw_glossary
    else:
        glossary = active_glossary()
    args.api_telemetry = []
    args.api_key = ""
    emit_runtime_event("segmentation initialization", {"step": "resolve_credential"})
    args.api_key, key_source = resolve_api_key("SUBSTAR_MODEL_API_KEY")
    if not args.api_key:
        # Direct invocations from pre-provider builds may still carry the old
        # variable. The supervised worker never writes this legacy slot.
        args.api_key, key_source = resolve_api_key("DEEPSEEK_API_KEY")
    if not args.api_key:
        raise SegmentationError("语义切分缺少当前模型服务密钥")

    emit_runtime_event(
        "segmentation initialization",
        {"step": "plan", "credential_source": key_source},
    )
    target_times = balanced_target_times(units, args.target_seconds)
    planning_skipped_reason = planning_skip_reason(units, args.target_seconds)
    planning_request = direct_seam_request(units, target_times)
    resume_planning_request_path = args.output_dir / "planning_direct_seam_request.json"
    if planning_request is None and args.resume_response_log and resume_planning_request_path.is_file():
        planning_request = json.loads(read(resume_planning_request_path))
    if planning_request is None:
        planning_request = {"targets": []}
    planning_block_ids = [str(item["target_id"]) for item in planning_request["targets"]]
    progress.plan(
        "execution_planning", 0 if planning_skipped_reason else len(planning_block_ids),
        block_ids=[] if planning_skipped_reason else planning_block_ids,
    )
    resume_planning_path = args.output_dir / "planning_execution_chunks.json"
    if args.resume_response_log and resume_planning_path.is_file():
        resumed_plan = json.loads(read(resume_planning_path))
        seams = [int(item) for item in resumed_plan.get("boundaries_after", [])]
        planning_exceptions = list(resumed_plan.get("exceptions", []))
        emit_runtime_event(
            "execution planning 已复用中断前安全接缝",
            {"seam_count": len(seams), "source": str(resume_planning_path)},
        )
    elif planning_skipped_reason:
        seams = []
        planning_exceptions = []
    else:
        seams = program_direct_seams(units, target_times)
        planning_exceptions = []
        for block_id in planning_block_ids:
            progress.event("execution_planning", "accepted", block_id=block_id)
    progress.finish("execution_planning", with_review=bool(planning_exceptions))
    ranges = chunk_ranges(units, seams)
    atomic_write_json(args.output_dir / "planning_direct_seam_request.json", planning_request)
    execution_blocks = []
    for number, (left, right) in enumerate(ranges, start=1):
        fingerprint_payload = [
            {
                "index": int(unit.index),
                "start": float(unit.start),
                "end": float(unit.end),
                "text": unit_text(unit),
            }
            for unit in units[left : right + 1]
        ]
        source_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        execution_blocks.append({
            "block_id": f"c{number:04d}",
            "chunk_id": f"c{number:04d}",
            "start_ms": round(float(units[left].start) * 1000),
            "end_ms": round(float(units[right].end) * 1000),
            "alignment_start": int(units[left].index),
            "alignment_end": int(units[right].index),
            "source_sha256": source_fingerprint,
        })
    execution_manifest = {
        "schema_version": "substar.execution-block-plan.v1",
        "target_seconds": int(args.target_seconds),
        "seam_radius_seconds": 60,
        "boundaries_after": seams,
        "blocks": execution_blocks,
        "chunks": execution_blocks,
        "exceptions": planning_exceptions,
        "skipped_reason": planning_skipped_reason or None,
    }
    atomic_write_json(args.output_dir / "split_input_plan.json", execution_manifest)

    progress.plan(
        "semantic_grouping", len(ranges),
        block_ids=[f"c{number:04d}" for number in range(1, len(ranges) + 1)],
    )
    meaning_groups: list[dict[str, Any]] = []
    grouping_exceptions: list[dict[str, Any]] = []
    merged_cuts: set[int] = set()
    response_cache = cached_semantic_grouping_responses(args.resume_response_log)
    if response_cache:
        emit_runtime_event(
            "semantic grouping 已载入中断响应缓存",
            {"cached_blocks": sorted(response_cache), "count": len(response_cache)},
        )
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ranges)) as executor:
        futures = []
        for number, bounds in enumerate(ranges, start=1):
            block_id = f"c{number:04d}"
            cached_value = response_cache.get(block_id)
            if cached_value is None:
                progress.event("semantic_grouping", "sent", block_id=block_id)
            futures.append(executor.submit(
                request_semantic_grouping_block,
                units, bounds, number, args, grouping_system_prompt, glossary,
                progress, cached_value, repair_prompt=repair_system_prompt,
            ))
        rows = [future.result() for future in concurrent.futures.as_completed(futures)]
    for _number, _spans, groups, _corrections, local_cuts, exceptions in sorted(rows):
        meaning_groups.extend(groups)
        merged_cuts.update(local_cuts)
        grouping_exceptions.extend(exceptions)
    progress.finish("semantic_grouping", with_review=bool(grouping_exceptions))
    atomic_write_json(
        args.output_dir / "semantic_grouping_plan.json",
        {"schema_version": "substar.semantic-grouping-plan.v1", "groups": meaning_groups},
    )
    accepted_group_ends = {
        int(group["alignment_end"])
        for group in meaning_groups
        if isinstance(group, Mapping)
        and isinstance(group.get("alignment_end"), int)
        and int(group["alignment_end"]) < int(units[-1].index)
    }
    downstream_plan = execution_block_plan(
        units,
        target_seconds=float(args.target_seconds),
        minimum_seconds=DEFAULT_MINIMUM_SECONDS,
        maximum_seconds=DEFAULT_MAXIMUM_SECONDS,
        allowed_after=accepted_group_ends,
        basis="accepted_semantic_groups",
    )
    atomic_write_json(args.output_dir / "execution_block_plan.json", downstream_plan)
    cuts = finalize_display_cuts(
        merged_cuts,
        seams,
        final_alignment_index=int(units[-1].index),
    )
    layout_exceptions: list[dict[str, Any]] = []
    protection = {
        "schema_version": "substar.segmentation-protection.v1",
        "spans": [],
        "preferred_breaks_after": [],
        "forbidden_breaks_after": [],
    }
    plan = build_plan_from_cuts(cuts, units, protection, meaning_groups)
    result = evaluate_direct_plan(
        master, units, plan, review_confidence=0.72, **frozen_layout_kwargs(args)
    )
    atomic_write_json(args.output_dir / "cue_layout_plan.json", plan)
    atomic_write_text(args.output_dir / "source_draft.txt", result.draft)
    atomic_write_json(
        args.output_dir / "segmentation_algorithm_validation.json",
        _direct_report(result, repaired=False, attempts=0),
    )
    exceptions = [*planning_exceptions, *grouping_exceptions, *layout_exceptions]
    segmentation_result = {
        "schema_version": "substar.segmentation-algorithm-result.v1",
        "route": args.route,
        "execution_plan": downstream_plan,
        "split_input_plan": execution_manifest,
        "meaning_groups": meaning_groups,
        "display_breaks": sorted(cuts),
        "cues": plan["groups"],
        "exceptions": exceptions,
        "validation": _direct_report(result, repaired=False, attempts=0),
        "provenance": {
            "source_language": source_language,
            "sentence_boundary_policy": args.sentence_boundary_policy,
            "semantic_grouping_prompt": semantic_grouping_prompt.metadata(),
            "semantic_grouping_repair_prompt": semantic_grouping_repair_prompt.metadata(),
            "boundary_evidence_prompt": boundary_evidence_prompt.metadata(),
            "models": {
                "execution_planning": "deterministic.v1",
                "semantic_grouping": args.grouping_model,
                "semantic_grouping_repair": getattr(args, "repair_model", "") or args.grouping_model,
            },
            "key_source": key_source,
            "api_calls": sorted(
                args.api_telemetry,
                key=lambda row: (str(row.get("stage", "")), str(row.get("block_id", ""))),
            ),
        },
    }
    atomic_write_json(args.output_dir / "segmentation_algorithm_result.json", segmentation_result)

    source_tokens = source_tokens_from_asr(units, source_asset_id=args.source_asset_id)
    editor_document = build_editor_document(
        source_tokens=source_tokens,
        source_kind=args.source_kind,
        source_asset_id=args.source_asset_id,
        execution_plan={
            "blocks": list(downstream_plan["blocks"]),
            "boundaries_after": list(downstream_plan["boundaries_after"]),
            "skipped_reason": None,
        },
        semantic_grouping={
            "protections": [],
            "meaning_groups": meaning_groups,
            "canonicalizations": [],
            "review_regions": grouping_exceptions,
        },
        cue_layout={"display_breaks": sorted(cuts)},
    )
    if args.candidate_only:
        atomic_write_json(
            args.output_dir / "editor_document_candidate.json", editor_document.to_dict()
        )
        print(
            f"complete candidate chunks={len(ranges)} cues={len(plan['groups'])}",
            flush=True,
        )
        return 0
    if args.project_store_dir is None or not args.project_id:
        raise SegmentationError(
            "project store and project id are required unless --candidate-only is used"
        )
    if (args.project_store_dir / "manifest.json").is_file():
        project_store = ProjectStore.open(args.project_store_dir)
        current = project_store.load_latest()
        expected_revision_id = current.revision_id if current is not None else None
        revision_operation = "segmentation_rebuild"
    else:
        project_store = ProjectStore.create(
            args.project_store_dir, project_id=args.project_id
        )
        expected_revision_id = None
        revision_operation = "segmentation_initial_document"
    revision = project_store.save(
        editor_document,
        provenance=ChangeProvenance(
            kind=ChangeKind.IMPORT,
            operation=revision_operation,
            actor="segmentation",
            metadata={
                "route": args.route,
                "source_kind": args.source_kind,
                "source_asset_id": args.source_asset_id,
            },
        ),
        expected_revision_id=expected_revision_id,
    )
    atomic_write_json(
        args.output_dir / "editor_document_candidate.json", editor_document.to_dict()
    )
    atomic_write_json(
        args.output_dir / "editor_revision.json",
        {
            "schema_version": "substar.editor-revision-pointer.v1",
            "project_id": args.project_id,
            "revision_id": revision.revision_id,
            "revision_number": revision.revision_number,
            "document_hash": editor_document.content_hash(),
        },
    )
    print(
        f"complete route={args.route} chunks={len(ranges)} cues={len(plan['groups'])}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
