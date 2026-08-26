from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.segmentation_support import (  # noqa: E402
    SegmentationError,
    _direct_report,
    endpoint,
    extract_json,
    resolve_api_key,
    shared_context,
    source_punctuation_kwargs,
    write_json,
    write_two_level_artifacts,
)
from substar_core.segmentation.material import (  # noqa: E402
    display_normalize,
    extract_alignment,
    extract_master,
)
from substar_core.segmentation.validation import evaluate_direct_plan  # noqa: E402


DEFAULT_MODELS = ("deepseek-v4-flash", "deepseek-v4-pro")
STAGES = {
    "p1": (
        PROJECT_ROOT / "prompts" / "03P1_全片意义组与保护分析.md",
        PROJECT_ROOT / "schemas" / "stage1_global_analysis.schema.json",
    ),
    "p2": (
        PROJECT_ROOT / "prompts" / "03P2_全片唯一切分计划.md",
        PROJECT_ROOT / "schemas" / "stage1_global_plan.schema.json",
    ),
    "p3": (
        PROJECT_ROOT / "prompts" / "03P3_全片切分只读审阅.md",
        PROJECT_ROOT / "schemas" / "stage1_global_review.schema.json",
    ),
}


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def system_prompt(stage: str, material: str) -> str:
    prompt_path, schema_path = STAGES[stage]
    return "\n\n".join(
        [
            prompt_path.read_text(encoding="utf-8"),
            shared_context(material),
            "# OUTPUT_SCHEMA\n" + schema_path.read_text(encoding="utf-8"),
        ]
    )


def call_streaming_model(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    timeout: int,
    max_tokens: int,
    reasoning_effort: str,
    raw_response_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "thinking": {"type": "enabled"},
        "reasoning_effort": reasoning_effort,
        "response_format": {"type": "json_object"},
    }
    started = time.perf_counter()
    content_parts: list[str] = []
    finish_reason: str | None = None
    usage: dict[str, Any] = {}
    with requests.post(
        endpoint(base_url),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=(60, timeout),
        stream=True,
    ) as response:
        response.raise_for_status()
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line or raw_line.startswith(":"):
                continue
            if not raw_line.startswith("data:"):
                continue
            data = raw_line[5:].strip()
            if data == "[DONE]":
                break
            event = json.loads(data)
            if event.get("usage"):
                usage = event["usage"]
            choices = event.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(str(delta["content"]))
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])
    if finish_reason == "length":
        raise SegmentationError("模型输出达到长度上限")
    content = "".join(content_parts)
    if not content:
        raise SegmentationError("流式响应没有最终 content")
    if raw_response_path is not None:
        raw_response_path.parent.mkdir(parents=True, exist_ok=True)
        raw_response_path.write_text(content, encoding="utf-8")
    return extract_json(content), {
        "schema_version": "substar.stage1.api-call.v1",
        "model": model,
        "reasoning_effort": reasoning_effort,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "finish_reason": finish_reason,
        "usage": usage,
        "stream": True,
        "request_attempts": 1,
    }


def validate_p1(
    analysis: dict[str, Any],
    *,
    first_index: int,
    last_index: int,
) -> list[dict[str, Any]]:
    if analysis.get("schema_version") != "substar.stage1.global-analysis.v1":
        raise SegmentationError("P1 schema_version 错误")
    groups = analysis.get("groups")
    if not isinstance(groups, list) or not groups:
        raise SegmentationError("P1 缺少意义组")
    zero_based_ids = all(
        group.get("group_id") == f"g{number - 1:04d}"
        for number, group in enumerate(groups, start=1)
    )
    if zero_based_ids:
        for number, group in enumerate(groups, start=1):
            group["group_id"] = f"g{number:04d}"
    expected = first_index
    warnings: list[dict[str, Any]] = (
        [
            {
                "code": "zero_based_group_ids_normalized",
                "reason": "覆盖顺序完整，仅把g0000起始的顺序编号归一化为g0001起始",
            }
        ]
        if zero_based_ids
        else []
    )
    for number, group in enumerate(groups, start=1):
        if group.get("group_id") != f"g{number:04d}":
            raise SegmentationError(f"P1 group_id 不连续：{group.get('group_id')}")
        start = group.get("alignment_start")
        end = group.get("alignment_end")
        if start != expected or not isinstance(end, int) or end < start:
            raise SegmentationError(
                f"P1 索引覆盖错误：expected={expected}, got={start}-{end}"
            )
        for span in group.get("protected_spans", []):
            if not (
                isinstance(span.get("alignment_start"), int)
                and isinstance(span.get("alignment_end"), int)
                and start
                <= span["alignment_start"]
                <= span["alignment_end"]
                <= end
            ):
                warnings.append(
                    {
                        "code": "protected_span_outside_group",
                        "group_id": group["group_id"],
                        "span": span,
                    }
                )
        continuity = group.get("continuity_after")
        if not isinstance(continuity, dict):
            raise SegmentationError(
                f"P1 缺少 continuity_after：{group['group_id']}"
            )
        if continuity.get("relation") not in {
            "continuous",
            "related",
            "separate",
        }:
            raise SegmentationError(
                f"P1 continuity_after.relation 无效：{group['group_id']}"
            )
        if not isinstance(continuity.get("confidence"), (int, float)):
            raise SegmentationError(
                f"P1 continuity_after.confidence 无效：{group['group_id']}"
            )
        expected = end + 1
    if expected != last_index + 1:
        raise SegmentationError(
            f"P1 末索引错误：expected={last_index}, got={expected - 1}"
        )
    if groups[-1]["continuity_after"]["relation"] != "separate":
        warnings.append(
            {
                "code": "terminal_continuity_normalized",
                "group_id": groups[-1]["group_id"],
                "from": groups[-1]["continuity_after"]["relation"],
                "to": "separate",
            }
        )
        groups[-1]["continuity_after"] = {
            "relation": "separate",
            "confidence": 1.0,
            "reason": "terminal programme boundary",
            "speaker_transition": "unknown",
        }
    return warnings


def normalize_p2(
    value: dict[str, Any],
    analysis: dict[str, Any],
) -> dict[str, Any]:
    if value.get("schema_version") != "substar.stage1.global-plan.v1":
        raise SegmentationError("P2 schema_version 错误")
    raw_groups = value.get("groups")
    analysis_groups = analysis["groups"]
    if not isinstance(raw_groups, list):
        raise SegmentationError("P2 groups 必须是数组")
    if len(raw_groups) != len(analysis_groups):
        # A model may preserve every alignment unit but subdivide one of P1's
        # immutable groups. This is safely reversible: fold the subdivisions
        # back into the P1 envelope and retain their internal boundaries.
        # Any gap, overlap, cross-P1 span, or coverage change still fails.
        folded: list[dict[str, Any]] = []
        cursor = 0
        for source in analysis_groups:
            source_start = source["alignment_start"]
            source_end = source["alignment_end"]
            next_start = source_start
            breaks: set[int] = set()
            while cursor < len(raw_groups):
                raw = raw_groups[cursor]
                raw_start = raw.get("alignment_start")
                raw_end = raw.get("alignment_end")
                if (
                    not isinstance(raw_start, int)
                    or not isinstance(raw_end, int)
                    or raw_start != next_start
                    or raw_end < raw_start
                    or raw_end > source_end
                ):
                    break
                breaks.update(
                    item
                    for item in raw.get("line_breaks_after", [])
                    if isinstance(item, int) and raw_start <= item < raw_end
                )
                if raw_end < source_end:
                    breaks.add(raw_end)
                next_start = raw_end + 1
                cursor += 1
                if raw_end == source_end:
                    break
            if next_start != source_end + 1:
                raise SegmentationError(
                    f"P2 组数与 P1 不同且无法安全折回："
                    f"{len(raw_groups)}/{len(analysis_groups)}"
                )
            folded.append(
                {
                    "group_id": source["group_id"],
                    "alignment_start": source_start,
                    "alignment_end": source_end,
                    "line_breaks_after": sorted(breaks),
                }
            )
        if cursor != len(raw_groups):
            raise SegmentationError(
                f"P2 组数与 P1 不同且存在额外覆盖："
                f"{len(raw_groups)}/{len(analysis_groups)}"
            )
        raw_groups = folded
    if len(raw_groups) != len(analysis_groups):
        raise SegmentationError(
            f"P2 组数与 P1 不同：{len(raw_groups or [])}/{len(analysis_groups)}"
        )
    groups: list[dict[str, Any]] = []
    for raw, source in zip(raw_groups, analysis_groups):
        for key in ("group_id", "alignment_start", "alignment_end"):
            if raw.get(key) != source.get(key):
                raise SegmentationError(
                    f"P2 改变 P1 {key}：{source.get('group_id')}"
                )
        groups.append(
            {
                "group_id": source["group_id"],
                "alignment_start": source["alignment_start"],
                "alignment_end": source["alignment_end"],
                # A line break denotes an internal boundary. Compatible
                # models occasionally repeat alignment_end as the last break,
                # which would create an empty display line when the renderer
                # appends alignment_end itself. Removing out-of-range and
                # duplicate values is schema normalization, not a semantic
                # re-layout.
                "line_breaks_after": sorted(
                    {
                        int(value)
                        for value in raw.get("line_breaks_after", [])
                        if isinstance(value, int)
                        and source["alignment_start"] <= value < source["alignment_end"]
                    }
                ),
                "alternative_breaks_after": [],
                "confidence": 0.8,
                "needs_review": False,
                "protected_spans": [
                    {
                        **span,
                        "span_id": f"{source['group_id']}_s{position:03d}",
                        "parent_span_id": None,
                        "child_span_ids": [],
                    }
                    for position, span in enumerate(
                        source.get("protected_spans", []), start=1
                    )
                    if source["alignment_start"]
                    <= span.get("alignment_start", -1)
                    <= span.get("alignment_end", -1)
                    <= source["alignment_end"]
                ],
                "deletions": [],
                "corrections": [],
                "continuity_after": {
                    "relation": str(
                        source.get("continuity_after", {}).get(
                            "relation", "separate"
                        )
                    ),
                    "confidence": float(
                        source.get("continuity_after", {}).get(
                            "confidence", 0.5
                        )
                    ),
                    "reason": str(
                        source.get("continuity_after", {}).get(
                            "reason", "P1 did not provide a detailed reason"
                        )
                    ),
                    "speaker_transition": str(
                        source.get("continuity_after", {}).get(
                            "speaker_transition", "unknown"
                        )
                    ),
                },
                "reason": "P1 full-program analysis; P2 unique full-program layout",
            }
        )
    return {
        "schema_version": "substar.stage1.direct.v1",
        "source_language": str(value.get("source_language", "unknown")),
        "groups": groups,
        "coverage_check": {"complete": True, "ordered": True},
    }


def invalid_plan_groups(
    validation: dict[str, Any],
    direct_plan: dict[str, Any],
) -> list[str]:
    ids: set[str] = set()
    groups = direct_plan.get("groups", [])
    for issue in validation.get("plan_issues", []):
        if not isinstance(issue, dict):
            continue
        code = str(issue.get("code", ""))
        detail = issue.get("detail", {})
        direct_group_id = str(issue.get("group_id", ""))
        if direct_group_id:
            ids.add(direct_group_id)
        try:
            number = int(detail.get("group", 0))
        except (TypeError, ValueError):
            continue
        if 1 <= number <= len(groups):
            ids.add(str(groups[number - 1]["group_id"]))
    return sorted(ids)


def invalid_hard_limit_groups(
    validation: dict[str, Any],
    direct_plan: dict[str, Any],
) -> list[str]:
    ids: set[str] = set()
    groups = direct_plan.get("groups", [])
    for issue in validation.get("plan_issues", []):
        if not isinstance(issue, dict):
            continue
        code = str(issue.get("code", ""))
        detail = issue.get("detail", {})
        if not code.startswith(("draft_english_over_", "draft_chinese_over_")):
            continue
        try:
            number = int(detail.get("group", 0))
        except (TypeError, ValueError):
            continue
        if 1 <= number <= len(groups):
            ids.add(str(groups[number - 1]["group_id"]))
    return sorted(ids)


def apply_group_break_repairs(
    direct_plan: dict[str, Any],
    repair: dict[str, Any],
    invalid_ids: list[str],
) -> None:
    if repair.get("schema_version") != "substar.stage1.p2-repair.v1":
        raise SegmentationError("P2有限复议 schema_version 错误")
    supplied = repair.get("groups")
    if not isinstance(supplied, list):
        raise SegmentationError("P2有限复议缺少groups")
    expected = set(invalid_ids)
    received = {
        str(item.get("group_id")) for item in supplied if isinstance(item, dict)
    }
    if received != expected:
        raise SegmentationError(
            f"P2有限复议组覆盖错误：expected={sorted(expected)}, got={sorted(received)}"
        )
    by_id = {
        str(group["group_id"]): group for group in direct_plan.get("groups", [])
    }
    for item in supplied:
        group = by_id[str(item["group_id"])]
        start = int(group["alignment_start"])
        end = int(group["alignment_end"])
        raw_breaks = item.get("line_breaks_after")
        if not isinstance(raw_breaks, list):
            raise SegmentationError("P2有限复议 line_breaks_after 必须是数组")
        group["line_breaks_after"] = sorted(
            {
                int(value)
                for value in raw_breaks
                if isinstance(value, int) and start <= value < end
            }
        )


WEAK_LEFT_TOKENS = {
    "a",
    "an",
    "the",
    "to",
    "of",
    "in",
    "on",
    "at",
    "for",
    "from",
    "with",
    "and",
    "or",
    "but",
    "if",
    "because",
    "is",
    "are",
    "was",
    "were",
    "has",
    "have",
    "had",
    "will",
    "would",
    "can",
    "could",
    "should",
}


def complete_hard_limits(
    direct_plan: dict[str, Any],
    units: list[Any],
    invalid_ids: list[str],
    *,
    hard_limit: int = 55,
    chinese_hard_limit: int = 24,
) -> list[dict[str, Any]]:
    """Add boundaries only inside invalid cues; never merge or touch valid regions."""
    unit_by_index = {int(unit.index): unit for unit in units}
    actions: list[dict[str, Any]] = []
    invalid = set(invalid_ids)
    for group in direct_plan.get("groups", []):
        if str(group.get("group_id")) not in invalid:
            continue
        group_start = int(group["alignment_start"])
        group_end = int(group["alignment_end"])
        breaks = sorted(
            {
                int(value)
                for value in group.get("line_breaks_after", [])
                if group_start <= int(value) < group_end
            }
        )
        hard_spans = [
            (int(span["alignment_start"]), int(span["alignment_end"]))
            for span in group.get("protected_spans", [])
            if str(span.get("protection_level")) == "hard"
        ]

        def text_between(start: int, end: int) -> str:
            raw = " ".join(
                str(unit_by_index[index].text)
                for index in range(start, end + 1)
                if index in unit_by_index
            )
            return display_normalize(
                raw,
                baseline_punctuation="preserve",
                raised_punctuation="preserve",
            )

        changed = True
        while changed:
            changed = False
            edges = [group_start - 1, *breaks, group_end]
            for left_edge, right_edge in zip(edges, edges[1:]):
                start = left_edge + 1
                end = right_edge
                segment_text = text_between(start, end)
                active_limit = (
                    chinese_hard_limit
                    if re.search(r"[\u3400-\u9fff]", segment_text)
                    else hard_limit
                )
                if len(segment_text) <= active_limit:
                    continue
                candidates: list[tuple[float, int, bool]] = []
                for boundary in range(start, end):
                    left = text_between(start, boundary)
                    if len(left) > active_limit:
                        continue
                    right = text_between(boundary + 1, end)
                    right_unit_count = end - boundary
                    cuts_hard = any(
                        span_start <= boundary < span_end
                        for span_start, span_end in hard_spans
                    )
                    last_token = str(unit_by_index[boundary].text).strip().lower()
                    score = float(len(left))
                    if last_token.strip(".,!?;:") in WEAK_LEFT_TOKENS:
                        score -= 35
                    if right_unit_count <= 2:
                        score -= 100
                    elif len(right) < 15:
                        score -= 50
                    if re.search(r"[.!?][\"'”’)]?$", left):
                        score += 20
                    candidates.append((score, boundary, cuts_hard))
                legal = [item for item in candidates if not item[2]]
                pool = legal or candidates
                if not pool:
                    raise SegmentationError(
                        f"无法为{group['group_id']}补足{active_limit}字符硬切"
                    )
                _, selected, cuts_hard = max(
                    pool, key=lambda item: (item[0], item[1])
                )
                breaks.append(selected)
                breaks = sorted(set(breaks))
                actions.append(
                    {
                        "group_id": group["group_id"],
                        "alignment_after": selected,
                        "reason": f"P2有限复议后仍超{active_limit}字符，仅补硬切",
                        "cuts_hard_protection": cuts_hard,
                    }
                )
                changed = True
                break
        group["line_breaks_after"] = breaks
    return actions


def review_markdown(review: dict[str, Any]) -> str:
    lines = [
        f"# P3 全片切分审阅",
        "",
        f"- 整体风险：{review.get('overall_risk', 'unknown')}",
        f"- 优先人工检查：{review.get('priority_review_count', 0)} 项",
        f"- 总结：{review.get('summary', '')}",
        "",
        "## 风险项",
        "",
    ]
    issues = review.get("issues", [])
    if not issues:
        lines.append("未报告具体风险。")
    for number, issue in enumerate(issues, start=1):
        location = (
            f"{issue.get('alignment_start')}-{issue.get('alignment_end')}"
        )
        if issue.get("after_alignment") is not None:
            location += f"，切点 after {issue['after_alignment']}"
        lines.extend(
            [
                f"{number}. **[{issue.get('severity')}] "
                f"{issue.get('group_id')} · {issue.get('category')}**",
                f"   - 位置：{location}",
                f"   - 观察：{issue.get('observation')}",
                f"   - 审阅方向：{issue.get('review_direction')}",
            ]
        )
    lines.extend(["", "## 表现良好的代表结构", ""])
    strengths = review.get("strengths", [])
    if not strengths:
        lines.append("未单列。")
    for item in strengths:
        lines.append(f"- **{item.get('group_id')}**：{item.get('observation')}")
    return "\n".join(lines) + "\n"


def run_chain(
    *,
    route_name: str,
    p1_model: str,
    p2_model: str,
    p3_model: str,
    material: str,
    output_root: Path,
    base_url: str,
    api_key: str,
    timeout: int,
    max_tokens: int,
    reasoning_effort: str,
) -> dict[str, Any]:
    model_dir = output_root / route_name
    model_dir.mkdir(parents=True, exist_ok=True)
    master = extract_master(material)
    units = extract_alignment(material)
    first_index = int(units[0].index)
    last_index = int(units[-1].index)

    if (model_dir / "p1_analysis.json").exists():
        p1 = json.loads((model_dir / "p1_analysis.json").read_text(encoding="utf-8"))
        p1_call = json.loads(
            (model_dir / "p1_api_call.json").read_text(encoding="utf-8")
        )
    else:
        p1, p1_call = call_streaming_model(
            base_url=base_url,
            api_key=api_key,
            model=p1_model,
            system=system_prompt("p1", material),
            user="# FULL_PROGRAM\n" + material,
            timeout=timeout,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
        write_json(model_dir / "p1_analysis.json", p1)
        write_json(model_dir / "p1_api_call.json", p1_call)
    p1_warnings = validate_p1(p1, first_index=first_index, last_index=last_index)
    write_json(model_dir / "p1_analysis.json", p1)
    write_json(
        model_dir / "p1_validation.json",
        {"valid_coverage": True, "warnings": p1_warnings},
    )

    if (model_dir / "p2_plan_raw.json").exists():
        p2 = json.loads((model_dir / "p2_plan_raw.json").read_text(encoding="utf-8"))
        p2_call = json.loads(
            (model_dir / "p2_api_call.json").read_text(encoding="utf-8")
        )
    else:
        p2, p2_call = call_streaming_model(
            base_url=base_url,
            api_key=api_key,
            model=p2_model,
            system=system_prompt("p2", material),
            user="\n\n".join(
                [
                    "# FULL_PROGRAM\n" + material,
                    "# P1_ANALYSIS\n" + json.dumps(p1, ensure_ascii=False),
                    "# PROGRAM_P1_WARNINGS\n"
                    + json.dumps(p1_warnings, ensure_ascii=False),
                ]
            ),
            timeout=timeout,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
        write_json(model_dir / "p2_plan_raw.json", p2)
        write_json(model_dir / "p2_api_call.json", p2_call)
    direct_plan = normalize_p2(p2, p1)
    evaluation_error: str | None = None

    def evaluate_current() -> tuple[Any, str, dict[str, Any]]:
        result = evaluate_direct_plan(
            master,
            units,
            direct_plan,
            review_confidence=0.72,
            **source_punctuation_kwargs(),
        )
        return result, result.draft, _direct_report(
            result, repaired=False, attempts=0
        )

    try:
        result, draft, validation = evaluate_current()
    except Exception as exc:
        evaluation_error = str(exc)
        result = None
        draft = ""
        validation = {
            "schema_version": "substar.stage1.direct-validation.v1",
            "valid": False,
            "repaired": False,
            "repair_attempts": 0,
            "plan_issues": [{"code": "evaluation_exception", "detail": str(exc)}],
            "review_notices": [],
            "draft_validation": {},
        }

    invalid_ids = invalid_plan_groups(validation, direct_plan)
    repair_call: dict[str, Any] | None = None
    repair_error: str | None = None
    if invalid_ids:
        repair_path = model_dir / "p2_hard_repair_raw.json"
        repair_call_path = model_dir / "p2_hard_repair_api_call.json"
        try:
            if repair_path.exists() and repair_call_path.exists():
                repair_value = json.loads(repair_path.read_text(encoding="utf-8"))
                repair_call = json.loads(
                    repair_call_path.read_text(encoding="utf-8")
                )
            else:
                repair_value, repair_call = call_streaming_model(
                    base_url=base_url,
                    api_key=api_key,
                    model=p2_model,
                    system="\n\n".join(
                        [
                            system_prompt("p2", material),
                            "# LIMITED_HARD_REPAIR",
                            "上一版存在硬字符上限或hard保护切入错误。本次只返回列出的违规组，"
                            "重新给出这些组的完整line_breaks_after。不得改变组边界，"
                            "不得返回未列出的组。每个英文cue含空格标点不得超过55字符。",
                            "# OUTPUT_SCHEMA",
                            json.dumps(
                                {
                                    "type": "object",
                                    "required": ["schema_version", "groups"],
                                    "properties": {
                                        "schema_version": {
                                            "const": "substar.stage1.p2-repair.v1"
                                        },
                                        "groups": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "required": [
                                                    "group_id",
                                                    "line_breaks_after",
                                                ],
                                                "properties": {
                                                    "group_id": {"type": "string"},
                                                    "line_breaks_after": {
                                                        "type": "array",
                                                        "items": {"type": "integer"},
                                                    },
                                                },
                                            },
                                        },
                                    },
                                },
                                ensure_ascii=False,
                            ),
                        ]
                    ),
                    user="\n\n".join(
                        [
                            "# FULL_PROGRAM\n" + material,
                            "# P1_ANALYSIS\n" + json.dumps(p1, ensure_ascii=False),
                            "# CURRENT_DIRECT_PLAN\n"
                            + json.dumps(direct_plan, ensure_ascii=False),
                            "# INVALID_GROUP_IDS\n"
                            + json.dumps(invalid_ids, ensure_ascii=False),
                            "# HARD_VALIDATION_ERRORS\n"
                            + json.dumps(
                                validation.get("plan_issues", []),
                                ensure_ascii=False,
                            ),
                        ]
                    ),
                    timeout=timeout,
                    max_tokens=max_tokens,
                    reasoning_effort="high",
                )
                write_json(repair_path, repair_value)
                write_json(repair_call_path, repair_call)
            apply_group_break_repairs(
                direct_plan, repair_value, invalid_ids
            )
            result, draft, validation = evaluate_current()
        except Exception as exc:
            repair_error = str(exc)

    remaining_hard_ids = invalid_hard_limit_groups(validation, direct_plan)
    hard_completion_actions: list[dict[str, Any]] = []
    if remaining_hard_ids:
        hard_completion_actions = complete_hard_limits(
            direct_plan,
            units,
            remaining_hard_ids,
            hard_limit=int(source_punctuation_kwargs()["english_hard_limit"]),
            chinese_hard_limit=int(
                source_punctuation_kwargs()["chinese_hard_limit"]
            ),
        )
        result, draft, validation = evaluate_current()
    validation["p2_hard_repair"] = {
        "requested_group_ids": invalid_ids,
        "api_call_used": repair_call is not None,
        "api_error": repair_error,
        "deterministic_completion_actions": hard_completion_actions,
        "scope": "add_breaks_inside_hard-invalid-cues-only",
    }
    write_json(model_dir / "p2_direct_plan.json", direct_plan)
    if result is not None:
        write_two_level_artifacts(model_dir, master, units, direct_plan)
    (model_dir / "p2_source_draft.txt").write_text(draft, encoding="utf-8")
    (model_dir / "stage03A_source_draft.txt").write_text(
        draft,
        encoding="utf-8",
    )
    write_json(model_dir / "stage1_direct_plan.json", direct_plan)
    write_json(model_dir / "p2_validation_report.json", validation)

    p3_fingerprint = sha256(
        json.dumps(
            {
                "p1": p1,
                "direct_plan": direct_plan,
                "validation": validation,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    p3_fingerprint_path = model_dir / "p3_input_fingerprint.txt"
    can_reuse_p3 = (
        (model_dir / "p3_review.json").exists()
        and (model_dir / "p3_api_call.json").exists()
        and p3_fingerprint_path.exists()
        and p3_fingerprint_path.read_text(encoding="utf-8").strip()
        == p3_fingerprint
    )
    if can_reuse_p3:
        p3 = json.loads((model_dir / "p3_review.json").read_text(encoding="utf-8"))
        p3_call = json.loads(
            (model_dir / "p3_api_call.json").read_text(encoding="utf-8")
        )
    else:
        p3, p3_call = call_streaming_model(
            base_url=base_url,
            api_key=api_key,
            model=p3_model,
            system=system_prompt("p3", material),
            user="\n\n".join(
                [
                    "# FULL_PROGRAM\n" + material,
                    "# P1_ANALYSIS\n" + json.dumps(p1, ensure_ascii=False),
                    "# PROGRAM_P1_WARNINGS\n"
                    + json.dumps(p1_warnings, ensure_ascii=False),
                    "# P2_PLAN\n" + json.dumps(direct_plan, ensure_ascii=False),
                    "# PROGRAM_VALIDATION\n"
                    + json.dumps(validation, ensure_ascii=False),
                ]
            ),
            timeout=timeout,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
    if p3.get("schema_version") != "substar.stage1.global-review.v1":
        raise SegmentationError("P3 schema_version 错误")
    write_json(model_dir / "p3_review.json", p3)
    write_json(model_dir / "p3_api_call.json", p3_call)
    p3_fingerprint_path.write_text(p3_fingerprint, encoding="utf-8")
    (model_dir / "p3_review.md").write_text(review_markdown(p3), encoding="utf-8")

    summary = {
        "model": route_name,
        "stage_models": {
            "P1": p1_model,
            "P2": p2_model,
            "P3": p3_model,
        },
        "p1_group_count": len(p1["groups"]),
        "p1_warning_count": len(p1_warnings),
        "p2_cue_count": sum(
            len(group.get("line_breaks_after", [])) + 1
            for group in direct_plan["groups"]
        ),
        "p2_valid": bool(validation.get("valid")),
        "p2_plan_issue_count": len(validation.get("plan_issues", [])),
        "p2_review_notice_count": len(validation.get("review_notices", [])),
        "p3_overall_risk": p3.get("overall_risk"),
        "p3_issue_count": len(p3.get("issues", [])),
        "p3_priority_review_count": p3.get("priority_review_count"),
        "evaluation_error": evaluation_error,
        "api_calls": 3 + int(repair_call is not None),
        "model_repairs": int(repair_call is not None),
        "boundary_changes_after_p2": len(hard_completion_actions),
        "durations_seconds": {
            "p1": p1_call.get("duration_seconds"),
            "p2": p2_call.get("duration_seconds"),
            "p3": p3_call.get("duration_seconds"),
            "p2_hard_repair": (
                repair_call.get("duration_seconds") if repair_call else None
            ),
        },
    }
    write_json(model_dir / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Flash/Pro 全片 P1 分析、P2 唯一切分、P3 只读审阅对照"
    )
    parser.add_argument("material", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--api-key-env", default="SUBSTAR_LLM_API_KEY")
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--route-name")
    parser.add_argument("--p1-model")
    parser.add_argument("--p2-model")
    parser.add_argument("--p3-model")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--max-tokens", type=int, default=128000)
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "max", "xhigh"), default="max")
    args = parser.parse_args()

    material = args.material.read_text(encoding="utf-8")
    api_key, key_source = resolve_api_key(args.api_key_env)
    if not api_key:
        raise SegmentationError("未找到 Substar LLM API 密钥")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        args.output_dir / "experiment_manifest.json",
        {
            "schema_version": "substar.experiment.global-staged-ab.v1",
            "models": args.models,
            "stage_models": {
                "P1": args.p1_model,
                "P2": args.p2_model,
                "P3": args.p3_model,
            },
            "material": str(args.material.resolve()),
            "material_sha256": sha256(material),
            "reasoning_effort": args.reasoning_effort,
            "calls_per_model": "3, plus at most 1 targeted P2 hard-limit repair",
            "request_attempts_per_stage": 1,
            "p3_policy": "review_only",
            "model_repairs": "0 or 1 targeted invalid-group repair",
            "boundary_changes_after_p2": (
                "0 when P2 is valid; otherwise only deterministic additions "
                "inside still-invalid over-limit cues"
            ),
            "key_source": key_source,
        },
    )

    summaries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    explicit_route = any((args.p1_model, args.p2_model, args.p3_model))
    if explicit_route and not all((args.p1_model, args.p2_model, args.p3_model)):
        parser.error("--p1-model/--p2-model/--p3-model 必须同时提供")
    routes = (
        [
            {
                "route_name": args.route_name or "selected",
                "p1_model": args.p1_model,
                "p2_model": args.p2_model,
                "p3_model": args.p3_model,
            }
        ]
        if explicit_route
        else [
            {
                "route_name": model,
                "p1_model": model,
                "p2_model": model,
                "p3_model": model,
            }
            for model in args.models
        ]
    )
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(routes)
    ) as executor:
        futures = {
            executor.submit(
                run_chain,
                **route,
                material=material,
                output_root=args.output_dir,
                base_url=args.base_url,
                api_key=api_key,
                timeout=args.timeout,
                max_tokens=args.max_tokens,
                reasoning_effort=args.reasoning_effort,
            ): route["route_name"]
            for route in routes
        }
        for future in concurrent.futures.as_completed(futures):
            model = futures[future]
            try:
                summaries.append(future.result())
            except Exception as exc:
                errors.append({"model": model, "error": str(exc)})
    summaries.sort(key=lambda item: str(item["model"]))
    write_json(
        args.output_dir / "comparison_summary.json",
        {
            "schema_version": "substar.experiment.global-staged-ab-result.v1",
            "summaries": summaries,
            "errors": errors,
        },
    )
    print(json.dumps({"summaries": summaries, "errors": errors}, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
