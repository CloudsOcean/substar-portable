from __future__ import annotations

import argparse
import copy
import concurrent.futures
import hashlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from substar_core.openai_compat import auth_headers, endpoint_url


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.segmentation.material import (  # noqa: E402
    configured_length,
    extract_alignment,
    extract_master,
    han_count,
    is_english_dominant,
    load_json,
    validate_draft,
    write_validation_report,
)
from substar_core.segmentation.chunking import (  # noqa: E402
    SegmentationChunk,
    _unit_original_ranges,
    build_segmentation_chunks,
    render_chunk_material,
)
from substar_core.segmentation.validation import (  # noqa: E402
    evaluate_direct_plan,
    merge_direct_plans,
    render_direct_draft,
    structural_issues,
)
from substar_core.segmentation.optimizer import (  # noqa: E402
    OptimizationResult,
    build_deterministic_fallback_plan,
    optimize_direct_plan,
)
from substar_core.segmentation.hierarchy import (  # noqa: E402
    augment_structural_boundaries,
    candidate_coverage_report,
    cuts_fingerprint,
    derive_hierarchy,
    hierarchical_analysis_issues,
    normalize_analysis_v2,
)
from substar_core.config import load_settings  # noqa: E402
from substar_core.glossary import active_glossary, glossary_prompt  # noqa: E402
from substar_core.policy import classify_language  # noqa: E402
from substar_core.reasoning_capabilities import (  # noqa: E402
    reasoning_effort_for_request,
    resolve_thinking_mode,
)


PROMPTS = {
    "direct": PROJECT_ROOT / "prompts" / "03A_索引式直接判断.md",
    "repair": PROJECT_ROOT / "prompts" / "03A_R_索引计划局部复议.md",
    "seams": PROJECT_ROOT / "prompts" / "03A_S_全接缝统一裁决.md",
    "analysis": PROJECT_ROOT / "prompts" / "03A1_意义群与保护结构分析.md",
    "candidates": PROJECT_ROOT / "prompts" / "03A2_每组生成三种合法切分候选.md",
    "decision": PROJECT_ROOT / "prompts" / "03A3_盲评选择并生成源文草案.md",
    "gate": PROJECT_ROOT / "prompts" / "03A4_候选内边界完整性复核.md",
}
PROMPT_OVERRIDE_DIR = os.environ.get("SUBSTAR_STAGE1_PROMPT_DIR", "").strip()
SENTENCE_HINT_MODE = (
    os.environ.get("SUBSTAR_STAGE1_SENTENCE_HINT_MODE", "full").strip().lower()
)
SCHEMAS = {
    "direct": PROJECT_ROOT / "schemas" / "stage1_direct.schema.json",
    "repair": PROJECT_ROOT / "schemas" / "stage1_direct.schema.json",
    "analysis": PROJECT_ROOT / "schemas" / "stage1_analysis.schema.json",
    "candidates": PROJECT_ROOT / "schemas" / "stage1_candidates.schema.json",
    "decision": PROJECT_ROOT / "schemas" / "stage1_decision.schema.json",
    "gate": PROJECT_ROOT / "schemas" / "stage1_decision.schema.json",
}
SPEC = PROJECT_ROOT / "docs" / "Substar_公司字幕规范_v1.md"
CASES = PROJECT_ROOT / "prompts" / "references" / "公司字幕切分风格案例库.md"


class SegmentationError(RuntimeError):
    pass


class SegmentationRequestError(SegmentationError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status in {408, 425, 429} or (
            self.status is not None and self.status >= 500
        )


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def resolved_prompt(stage: str) -> Path:
    """Allow isolated prompt experiments without editing production prompts."""

    production = PROMPTS[stage]
    if not PROMPT_OVERRIDE_DIR:
        return production
    candidate = Path(PROMPT_OVERRIDE_DIR) / production.name
    return candidate if candidate.is_file() else production


def prompt_text(stage: str) -> str:
    value = read(resolved_prompt(stage))
    if not PROMPT_OVERRIDE_DIR:
        return value
    appendix = Path(PROMPT_OVERRIDE_DIR) / f"{stage}.append.md"
    if appendix.is_file():
        value += "\n\n" + read(appendix)
    return value


def chunk_material_for_model(chunk: SegmentationChunk) -> str:
    return render_chunk_material(chunk, sentence_hint_mode=SENTENCE_HINT_MODE)


def endpoint(base_url: str) -> str:
    return endpoint_url(base_url, "/chat/completions")


def extract_json(text: str) -> dict[str, Any]:
    value = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.DOTALL)
    if fenced:
        value = fenced.group(1)
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        # Some OpenAI-compatible models occasionally place a literal newline
        # or tab inside a JSON string even in json_object mode. Python's
        # non-strict parser accepts those control characters without changing
        # the object structure. This prevents one cosmetic serialization fault
        # from degrading an entire multi-minute block to mechanical splitting.
        try:
            result = json.loads(value, strict=False)
        except json.JSONDecodeError:
            raise SegmentationError(f"模型没有返回有效 JSON：{exc}") from exc
    if not isinstance(result, dict):
        raise SegmentationError("模型 JSON 顶层必须是对象")
    return result


def response_text(body: dict[str, Any]) -> str:
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SegmentationError("模型响应中找不到 choices[0].message.content") from exc
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        combined = "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
        if combined.strip():
            return combined
    if content not in (None, ""):
        return str(content)
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    raise SegmentationError("模型响应正文为空")


def resolve_api_key(env_name: str) -> tuple[str, str]:
    from_environment = os.environ.get(env_name, "")
    if from_environment:
        return from_environment, f"environment:{env_name}"
    return "", "missing"


def thinking_for_stage(profile: str, stage: str) -> str:
    if profile == "hybrid":
        return "disabled" if stage == "candidates" else "enabled"
    return profile


def call_model(
    *,
    base_url: str,
    api_key: str,
    auth_mode: str = "bearer",
    model: str,
    system_prompt: str,
    user_payload: str,
    timeout: int,
    max_tokens: int,
    json_mode: bool,
    thinking_mode: str,
    reasoning_effort: str,
    temperature: float = 0.0,
    request_attempts: int = 2,
) -> tuple[dict[str, Any], dict[str, Any]]:
    requested_thinking_mode = str(thinking_mode or "disabled").strip().lower()
    effective_thinking_mode = resolve_thinking_mode(
        base_url, model, requested_thinking_mode
    )
    requested_effort = str(reasoning_effort or "low")
    effective_effort = reasoning_effort_for_request(base_url, model, requested_effort)
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_payload},
        ],
        "max_tokens": max_tokens,
        "stream": False,
    }
    if effective_thinking_mode == "enabled":
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = effective_effort
    else:
        payload["thinking"] = {"type": "disabled"}
        payload["temperature"] = float(temperature)
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    started = time.perf_counter()
    body: dict[str, Any] | None = None
    last_request_error: requests.RequestException | None = None
    total_attempts = max(1, min(3, int(request_attempts)))
    transport_attempt_count = 0
    idempotency_key = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    for attempt in range(1, total_attempts + 1):
        transport_attempt_count = attempt
        try:
            response = requests.post(
                endpoint(base_url),
                headers={
                    **auth_headers(api_key, auth_mode),
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotency_key,
                },
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            body = response.json()
            break
        except requests.RequestException as exc:
            last_request_error = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status is None or status in {408, 425, 429} or (
                status is not None and status >= 500
            )
            if retryable and attempt < total_attempts:
                time.sleep(min(6.0, attempt * 1.25))
                continue
            error_response = getattr(exc, "response", None)
            detail = (
                getattr(error_response, "text", "")[:1000]
                if error_response is not None
                else ""
            )
            raise SegmentationRequestError(
                f"Stage 1 LLM 请求失败：{exc} {detail}", status=status
            ) from exc
        except ValueError as exc:
            raise SegmentationError("Stage 1 LLM 返回的 HTTP 内容不是 JSON") from exc
    if body is None:
        raise SegmentationError(f"Stage 1 LLM 请求失败：{last_request_error}")
    try:
        finish_reason = body["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        finish_reason = None
    if finish_reason == "length":
        raise SegmentationError("模型输出因 max_tokens 或上下文上限被截断")
    if finish_reason in {"content_filter", "insufficient_system_resource"}:
        raise SegmentationError(f"模型未正常完成：finish_reason={finish_reason}")
    elapsed = time.perf_counter() - started
    result = extract_json(response_text(body))
    usage = body.get("usage", {})
    usage = usage if isinstance(usage, dict) else {}
    prompt_details = usage.get("prompt_tokens_details", {})
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    telemetry = {
        "schema_version": "substar.stage1.api-call.v1",
        "model": model,
        "thinking_mode": effective_thinking_mode,
        "requested_thinking_mode": requested_thinking_mode,
        "effective_thinking_mode": effective_thinking_mode,
        "reasoning_effort": effective_effort if effective_thinking_mode == "enabled" else None,
        "requested_reasoning_effort": requested_effort if effective_thinking_mode == "enabled" else None,
        "effective_reasoning_effort": effective_effort if effective_thinking_mode == "enabled" else None,
        "duration_seconds": round(elapsed, 3),
        "finish_reason": finish_reason,
        "transport_attempt_count": transport_attempt_count,
        "usage": usage,
        "cache_usage": {
            "hit_tokens": int(usage.get("prompt_cache_hit_tokens", 0) or 0),
            "miss_tokens": int(usage.get("prompt_cache_miss_tokens", 0) or 0),
            "cached_tokens": int(prompt_details.get("cached_tokens", 0) or 0),
        },
    }
    return result, telemetry


def select_style_cases(task_text: str, *, maximum: int = 6) -> str:
    """Retrieve a small, local few-shot set instead of injecting the full corpus."""

    source = read(CASES)
    sections = re.findall(
        r"(?ms)^###\s+([^\n]+)\n(.*?)(?=^###\s+|\Z)",
        source,
    )
    query_tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z'’-]{2,}", task_text)
        if len(token) >= 4
    }
    scored: list[tuple[float, str]] = []
    for title, body in sections:
        section = f"### {title}\n{body.strip()}"
        section_tokens = {
            token.lower()
            for token in re.findall(r"[A-Za-z][A-Za-z'’-]{2,}", section)
            if len(token) >= 4
        }
        overlap = query_tokens & section_tokens
        score = sum(1.0 + min(len(token), 10) / 10 for token in overlap)
        lowered = task_text.lower()
        if "from" in lowered and "to" in lowered and "from...to" in title:
            score += 8
        if re.search(r"\b(?:uh|um|er|erm)\b", lowered) and "填充音" in title:
            score += 8
        if any(mark in task_text for mark in "“”‘’?!？！") and "标点" in title:
            score += 5
        if score:
            scored.append((score, section))
    scored.sort(key=lambda item: item[0], reverse=True)
    selected = [section for _, section in scored[:maximum]]
    if not selected:
        # General structural examples only; no project-specific names are
        # promoted to universal rules.
        selected = [
            section
            for title, body in sections
            if title.startswith(("A1 ", "A3 ", "C1 ", "D5 ", "E1 ", "F1 "))
            for section in [f"### {title}\n{body.strip()}"]
        ][:maximum]
    return (
        "# RETRIEVED_COMPANY_CASES\n"
        "以下仅是与当前片段相关的少量参考；抽象规则优先，案例中的专名和具体措辞不构成硬规则。\n\n"
        + "\n\n".join(selected)
    )


def shared_context(task_text: str = "") -> str:
    settings = load_settings(include_secret=False)
    top_baseline = str(settings.get("top_baseline_punctuation", "preserve"))
    top_raised = str(settings.get("top_raised_punctuation", "preserve"))
    cleanup_mode = str(settings.get("text_cleanup_mode", "mark_conservative"))
    cleanup_contract = {
        "preserve": "文本降噪关闭：deletions 必须为空，不得标记或删除填充音、重复和口语成分。",
        "mark_conservative": "文本降噪为保守标记：只对高度确定、无词汇意义的填充音或即时口误使用 deletions；程序以 // 保留可追溯位置，最终显示隐藏。",
        "remove_conservative": "文本降噪为保守删除：适用范围与保守标记相同，所有删除仍必须通过 deletions 留下索引和理由，不得静默改稿。",
    }.get(cleanup_mode, "")
    punctuation_contract = "\n".join(
        [
            "# ACTIVE_OUTPUT_PROFILE",
            "当前 Stage 1 源语显示在上行。",
            f"上行上标点策略：{top_raised}。",
            f"上行下标点策略：{top_baseline}。",
            f"英文硬上限：{settings['english_hard_limit']}。",
            f"中文硬上限：{settings['chinese_hard_limit']}。",
            f"中英混合硬上限：{settings.get('mixed_hard_limit', 25)}。",
            f"日文硬上限：{settings.get('japanese_hard_limit', 25)}。",
            f"韩文硬上限：{settings.get('korean_hard_limit', 32)}。",
            f"最短显示时长：{settings['minimum_cue_duration_ms']}ms。",
            f"文本口语残留策略：{settings['text_cleanup_mode']}。",
            cleanup_contract,
            "任何最终保留的字符（包括空格、标点、数字、拉丁字母和符号）都必须计入硬上限；"
            "不得为了满足长度而静默删除标点或正文。",
        ]
    )
    return "\n\n".join(
        [
            punctuation_contract,
            glossary_prompt(
                active_glossary(str(settings.get("glossary_id", ""))),
                include_target=False,
            ),
            "# PRODUCT_SPEC\n" + read(SPEC),
            select_style_cases(task_text),
        ]
    )


def source_punctuation_kwargs() -> dict[str, Any]:
    settings = load_settings(include_secret=False)
    return {
        # Canonical punctuation is preserved through every processing stage.
        # Display/export projection belongs exclusively to the editor.
        "baseline_punctuation": "preserve",
        "raised_punctuation": "preserve",
        "english_hard_limit": int(settings.get("english_hard_limit", 55)),
        "chinese_hard_limit": int(settings.get("chinese_hard_limit", 24)),
        "mixed_hard_limit": int(settings.get("mixed_hard_limit", 25)),
        "japanese_hard_limit": int(settings.get("japanese_hard_limit", 25)),
        "korean_hard_limit": int(settings.get("korean_hard_limit", 32)),
        "english_count_spaces": True,
        "english_count_punctuation": True,
        "minimum_cue_duration_ms": int(
            settings.get("minimum_cue_duration_ms", 400)
        ),
    }


def apply_text_cleanup_policy(plan: dict[str, Any]) -> dict[str, Any]:
    if load_settings(include_secret=False).get("text_cleanup_mode") != "preserve":
        return plan
    cleaned = copy.deepcopy(plan)
    for group in cleaned.get("groups", []):
        group["deletions"] = []
    return cleaned


def optimize_valid_plan(
    master: str,
    units: list[Any],
    plan: dict[str, Any],
) -> OptimizationResult:
    """Never send structurally invalid model indexes into the optimizer."""

    cleaned = copy.deepcopy(plan)
    cleanup_actions: list[dict[str, Any]] = []
    valid_indexes = {int(unit.index) for unit in units}
    for number, group in enumerate(cleaned.get("groups", []), start=1):
        group["group_id"] = f"g{number:04d}"
        start = group.get("alignment_start")
        end = group.get("alignment_end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        original_breaks = group.get("line_breaks_after", [])
        valid_breaks = sorted(
            {
                int(value)
                for value in original_breaks
                if isinstance(value, int)
                and start <= int(value) < end
                and int(value) in valid_indexes
            }
        )
        if valid_breaks != original_breaks:
            cleanup_actions.append(
                {
                    "type": "sanitize_invalid_breaks",
                    "group_id": group["group_id"],
                    "before": original_breaks,
                    "after": valid_breaks,
                }
            )
        group["line_breaks_after"] = valid_breaks

        original_spans = group.get("protected_spans", [])
        valid_spans = [
            span
            for span in original_spans
            if isinstance(span, dict)
            and isinstance(span.get("alignment_start"), int)
            and isinstance(span.get("alignment_end"), int)
            and start <= int(span["alignment_start"])
            <= int(span["alignment_end"])
            <= end
            and int(span["alignment_start"]) in valid_indexes
            and int(span["alignment_end"]) in valid_indexes
        ]
        if valid_spans != original_spans:
            cleanup_actions.append(
                {
                    "type": "sanitize_invalid_protected_spans",
                    "group_id": group["group_id"],
                    "removed": len(original_spans) - len(valid_spans),
                }
            )
            group["needs_review"] = True
        group["protected_spans"] = valid_spans

        seen_edits: set[int] = set()
        for key in ("deletions", "corrections"):
            original_edits = group.get(key, [])
            valid_edits: list[dict[str, Any]] = []
            for edit in original_edits:
                if not isinstance(edit, dict):
                    continue
                index = edit.get("alignment_index")
                if (
                    not isinstance(index, int)
                    or not start <= index <= end
                    or index not in valid_indexes
                    or index in seen_edits
                ):
                    continue
                seen_edits.add(index)
                valid_edits.append(edit)
            if valid_edits != original_edits:
                cleanup_actions.append(
                    {
                        "type": "sanitize_invalid_edits",
                        "group_id": group["group_id"],
                        "edit_kind": key,
                        "removed": len(original_edits) - len(valid_edits),
                    }
                )
                group["needs_review"] = True
            group[key] = valid_edits

    # The optimizer is allowed to move a cut out of a model-declared protected
    # span. Invalid ranges, gaps or indexes are unsafe to dereference and must
    # go to the repair/fallback path instead.
    unsafe = [
        item
        for item in structural_issues(cleaned, units)
        if str(item.get("code", "")) != "protected_span_cut"
    ]
    if unsafe:
        return OptimizationResult(plan=cleaned, actions=cleanup_actions)
    optimized = optimize_direct_plan(
        master,
        units,
        cleaned,
        **source_punctuation_kwargs(),
    )
    return OptimizationResult(
        plan=optimized.plan,
        actions=cleanup_actions + optimized.actions,
    )


def stage_system(stage: str, task_text: str = "") -> str:
    return "\n\n".join(
        [
            prompt_text(stage),
            shared_context(task_text),
            "# OUTPUT_SCHEMA\n" + read(SCHEMAS[stage]),
        ]
    )


def assert_schema_version(stage: str, value: dict[str, Any]) -> None:
    expected = {
        "direct": "substar.stage1.direct.v1",
        "repair": "substar.stage1.direct.v1",
        "analysis": "substar.stage1.analysis.v2",
        "candidates": "substar.stage1.candidates.v2",
        "decision": "substar.stage1.decision.v2",
        "gate": "substar.stage1.decision.v2",
    }[stage]
    if value.get("schema_version") != expected:
        raise SegmentationError(
            f"{stage} schema_version 错误：{value.get('schema_version')!r}，应为 {expected!r}"
        )
    if not isinstance(value.get("groups"), list) or not value["groups"]:
        raise SegmentationError(f"{stage} 缺少非空 groups")


def blind_candidates(value: dict[str, Any], seed: int) -> dict[str, Any]:
    blinded = copy.deepcopy(value)
    rng = random.Random(seed)
    for group in blinded.get("groups", []):
        rng.shuffle(group.get("candidates", []))
        for position, candidate in enumerate(group.get("candidates", []), start=1):
            candidate.pop("strategy", None)
            candidate.pop("tradeoffs", None)
            candidate["candidate_id"] = f"{group.get('group_id', 'g')}_option_{position}"
    blinded["blinding"] = {
        "seed": seed,
        "candidate_order_randomized": True,
        "strategy_labels_removed": True,
        "generation_reasons_removed": True,
    }
    return blinded


def _candidate_fingerprint(candidate: dict[str, Any]) -> tuple[int, ...]:
    return tuple(int(value) for value in candidate.get("cut_after_alignment", []))


def _candidate_diversity_report(
    analysis: dict[str, Any],
    candidates: dict[str, Any],
) -> dict[str, Any]:
    candidate_groups = {
        str(group.get("group_id", "")): group
        for group in candidates.get("groups", [])
    }
    insufficient: list[dict[str, Any]] = []
    for analysis_group in analysis.get("groups", []):
        group_id = str(analysis_group.get("group_id", ""))
        group = candidate_groups.get(group_id, {})
        rows = list(group.get("candidates", []))
        unique = {_candidate_fingerprint(row) for row in rows}
        start = int(analysis_group.get("alignment_start", 0))
        end = int(analysis_group.get("alignment_end", start))
        splittable = end > start
        required = 2 if splittable else 1
        if len(unique) < required:
            insufficient.append(
                {
                    "group_id": group_id,
                    "candidate_count": len(rows),
                    "unique_boundary_plans": len(unique),
                    "required_unique_plans": required,
                }
            )
    coverage = candidate_coverage_report(
        analysis,
        candidates,
        hard_character_limit=int(
            source_punctuation_kwargs().get("english_hard_limit", 55)
        ),
    )
    return {
        "complete_group_coverage": len(candidate_groups)
        == len(analysis.get("groups", [])),
        "insufficient_groups": insufficient,
        "high_value_boundary_coverage": coverage,
        "needs_supplement": bool(insufficient) or coverage["needs_supplement"],
    }


def _merge_candidate_supplement(
    primary: dict[str, Any],
    supplement: dict[str, Any],
) -> dict[str, Any]:
    merged = copy.deepcopy(primary)
    supplemental_groups = {
        str(group.get("group_id", "")): group
        for group in supplement.get("groups", [])
    }
    existing_group_ids = {
        str(group.get("group_id", "")) for group in merged.get("groups", [])
    }
    primary_order = [
        str(group.get("group_id", "")) for group in primary.get("groups", [])
    ]
    for group_id, extra_group in supplemental_groups.items():
        if group_id not in existing_group_ids and group_id in primary_order:
            merged.setdefault("groups", []).append(copy.deepcopy(extra_group))
            existing_group_ids.add(group_id)
    for group in merged.get("groups", []):
        extra_group = supplemental_groups.get(str(group.get("group_id", "")))
        if not extra_group:
            continue
        existing = {
            _candidate_fingerprint(candidate)
            for candidate in group.get("candidates", [])
        }
        for candidate in extra_group.get("candidates", []):
            if len(group.get("candidates", [])) >= 3:
                break
            fingerprint = _candidate_fingerprint(candidate)
            if fingerprint in existing:
                continue
            group.setdefault("candidates", []).append(copy.deepcopy(candidate))
            existing.add(fingerprint)
    order = {group_id: position for position, group_id in enumerate(primary_order)}
    merged["groups"].sort(
        key=lambda group: order.get(str(group.get("group_id", "")), len(order))
    )
    return merged


def _direct_plan_from_blind_decision(
    analysis: dict[str, Any],
    blinded_candidates: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    candidate_groups = {
        str(group.get("group_id", "")): group
        for group in blinded_candidates.get("groups", [])
    }
    decision_groups = {
        str(group.get("group_id", "")): group
        for group in decision.get("groups", [])
    }
    direct_groups: list[dict[str, Any]] = []
    for analysis_group in analysis.get("groups", []):
        group_id = str(analysis_group.get("group_id", ""))
        candidate_group = candidate_groups.get(group_id)
        choice = decision_groups.get(group_id)
        if not candidate_group or not choice:
            raise SegmentationError(f"03A3 未完整决定 {group_id}")
        selected_id = str(choice.get("selected_candidate_id", ""))
        selected = next(
            (
                candidate
                for candidate in candidate_group.get("candidates", [])
                if str(candidate.get("candidate_id", "")) == selected_id
            ),
            None,
        )
        if selected is None:
            raise SegmentationError(
                f"03A3 为 {group_id} 选择了 03A2 未提供的候选 {selected_id!r}"
            )
        if list(choice.get("cues", [])) != list(selected.get("cues", [])):
            raise SegmentationError(f"03A3 改写了 {group_id} 的候选正文")
        protected: list[dict[str, Any]] = []
        derived_spans, span_issues = derive_hierarchy(analysis_group)
        if span_issues:
            raise SegmentationError(
                f"{group_id} 分层保护不合法："
                + json.dumps(span_issues[:5], ensure_ascii=False)
            )
        for span in derived_spans:
            protected.append(
                {
                    "span_id": str(span["span_id"]),
                    "alignment_start": int(span["alignment_start"]),
                    "alignment_end": int(span["alignment_end"]),
                    "category": str(span.get("category", "")),
                    "protection_level": str(span["protection_level"]),
                    "parent_span_id": span.get("parent_span_id"),
                    "child_span_ids": list(span.get("child_span_ids", [])),
                }
            )
        deletions = [
            {
                "alignment_index": int(edit["alignment_start"]),
                "confidence": float(edit["confidence"]),
                "reason": str(edit["reason"]),
            }
            for edit in analysis_group.get("deletion_candidates", [])
            if int(edit["alignment_start"]) == int(edit["alignment_end"])
            and float(edit["confidence"]) >= 0.98
        ]
        corrections = [
            {
                "alignment_index": int(edit["alignment_start"]),
                "proposal": str(edit["proposal"]),
                "confidence": float(edit["confidence"]),
                "reason": str(edit["reason"]),
            }
            for edit in analysis_group.get("correction_candidates", [])
            if int(edit["alignment_start"]) == int(edit["alignment_end"])
        ]
        score = sum(float(value) for value in choice.get("scores", {}).values())
        direct_groups.append(
            {
                "group_id": group_id,
                "alignment_start": int(analysis_group["alignment_start"]),
                "alignment_end": int(analysis_group["alignment_end"]),
                "line_breaks_after": [
                    int(value) for value in selected.get("cut_after_alignment", [])
                ],
                "alternative_breaks_after": [],
                "confidence": max(0.0, min(1.0, score / 100.0)),
                "needs_review": score < 75.0,
                "protected_spans": protected,
                "deletions": deletions,
                "corrections": corrections,
                "reason": "03A1 semantic analysis; 03A2 batched candidates; 03A3 blind selection",
            }
        )
    return {
        "schema_version": "substar.stage1.direct.v1",
        "source_language": str(analysis.get("source_language", "unknown")),
        "groups": direct_groups,
        "coverage_check": {"complete": True, "ordered": True},
    }


def _call_model_with_one_content_retry(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Retry one malformed/non-JSON model response without creating an open loop."""

    last_error: ValueError | None = None
    for content_attempt in range(1, 3):
        try:
            value, telemetry = call_model(**kwargs)
            telemetry["content_attempt"] = content_attempt
            return value, telemetry
        except ValueError as exc:
            last_error = exc
    raise SegmentationError(
        f"模型内容连续两次无法解析，停止重试：{last_error}"
    )


def _request_batched_blind_plan(
    *,
    chunk: SegmentationChunk,
    chunk_material: str,
    chunk_number: int,
    args: argparse.Namespace,
    api_key: str,
    chunk_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []

    analysis, analysis_telemetry = _call_model_with_one_content_retry(
        base_url=args.base_url,
        api_key=api_key,
        model=args.model,
        system_prompt=stage_system("analysis", chunk_material),
        user_payload="# INPUT_MATERIAL\n" + chunk_material,
        timeout=min(args.timeout, 300),
        max_tokens=args.max_tokens,
        json_mode=not args.no_json_mode,
        thinking_mode="enabled",
        reasoning_effort=args.reasoning_effort,
        request_attempts=args.http_attempts,
    )
    assert_schema_version("analysis", analysis)
    analysis = augment_structural_boundaries(analysis)
    hierarchy_issues = hierarchical_analysis_issues(analysis)
    if hierarchy_issues:
        raise SegmentationError(
            "03A1 分层保护未通过确定性验收："
            + json.dumps(hierarchy_issues[:8], ensure_ascii=False)
        )
    write_json(chunk_dir / "stage1_analysis.json", analysis)
    write_json(chunk_dir / "api_call_03A1.json", analysis_telemetry)
    rows.append({"chunk_id": chunk.chunk_id, "stage": "03A1", **analysis_telemetry})

    candidates, candidate_telemetry = _call_model_with_one_content_retry(
        base_url=args.base_url,
        api_key=api_key,
        model=args.model,
        system_prompt=stage_system("candidates", chunk_material),
        user_payload="\n\n".join(
            [
                "# INPUT_MATERIAL\n" + chunk_material,
                "# STAGE_03A1_ANALYSIS\n"
                + json.dumps(analysis, ensure_ascii=False),
            ]
        ),
        timeout=min(args.timeout, 300),
        max_tokens=args.max_tokens,
        json_mode=not args.no_json_mode,
        thinking_mode="enabled",
        reasoning_effort=args.reasoning_effort,
        request_attempts=args.http_attempts,
    )
    assert_schema_version("candidates", candidates)
    diversity = _candidate_diversity_report(analysis, candidates)
    write_json(chunk_dir / "api_call_03A2.json", candidate_telemetry)
    rows.append({"chunk_id": chunk.chunk_id, "stage": "03A2", **candidate_telemetry})

    supplement_telemetry: dict[str, Any] | None = None
    if diversity["needs_supplement"]:
        supplement, supplement_telemetry = _call_model_with_one_content_retry(
            base_url=args.base_url,
            api_key=api_key,
            model=args.model,
            system_prompt=stage_system("candidates", chunk_material),
            user_payload="\n\n".join(
                [
                    "# INPUT_MATERIAL\n" + chunk_material,
                    "# STAGE_03A1_ANALYSIS\n"
                    + json.dumps(analysis, ensure_ascii=False),
                    "# EXISTING_CANDIDATES\n"
                    + json.dumps(candidates, ensure_ascii=False),
                    "# SUPPLEMENT_TASK\n只为多样性或高价值边界覆盖报告中的组补充候选；"
                    "至少补齐缺失边界的采用/不采用一侧，不得重复已有 "
                    "cut_after_alignment。整块最多执行本次一次补充。",
                    "# DIVERSITY_REPORT\n"
                    + json.dumps(diversity, ensure_ascii=False),
                ]
            ),
            timeout=min(args.timeout, 300),
            max_tokens=args.max_tokens,
            json_mode=not args.no_json_mode,
            thinking_mode="enabled",
            reasoning_effort=args.reasoning_effort,
            request_attempts=args.http_attempts,
        )
        assert_schema_version("candidates", supplement)
        candidates = _merge_candidate_supplement(candidates, supplement)
        write_json(chunk_dir / "stage1_candidates_supplement.json", supplement)
        write_json(chunk_dir / "api_call_03A2_supplement.json", supplement_telemetry)
        rows.append(
            {
                "chunk_id": chunk.chunk_id,
                "stage": "03A2-supplement",
                **supplement_telemetry,
            }
        )
        diversity = _candidate_diversity_report(analysis, candidates)
    write_json(chunk_dir / "stage1_candidate_diversity.json", diversity)
    write_json(chunk_dir / "stage1_candidates.json", candidates)

    seed = 20260726 + chunk_number
    blinded = blind_candidates(candidates, seed)
    write_json(chunk_dir / "stage1_candidates_blinded.json", blinded)
    decision_payload_parts = [
        "# INPUT_MATERIAL\n" + chunk_material,
        "# STAGE_03A1_ANALYSIS\n" + json.dumps(analysis, ensure_ascii=False),
        "# BLINDED_CANDIDATES\n" + json.dumps(blinded, ensure_ascii=False),
    ]
    plan: dict[str, Any] | None = None
    decision: dict[str, Any] = {}
    decision_telemetry: dict[str, Any] = {}
    decision_error = ""
    for decision_attempt in range(1, 3):
        decision, decision_telemetry = _call_model_with_one_content_retry(
            base_url=args.base_url,
            api_key=api_key,
            model=args.model,
            system_prompt=stage_system("decision", chunk_material),
            user_payload="\n\n".join(decision_payload_parts),
            timeout=min(args.timeout, 300),
            max_tokens=args.max_tokens,
            json_mode=not args.no_json_mode,
            thinking_mode="enabled",
            reasoning_effort=args.reasoning_effort,
            request_attempts=args.http_attempts,
        )
        decision_telemetry["decision_attempt"] = decision_attempt
        write_json(
            chunk_dir / f"stage1_decision_raw_{decision_attempt}.json",
            decision,
        )
        write_json(
            chunk_dir / f"api_call_03A3_attempt_{decision_attempt}.json",
            decision_telemetry,
        )
        try:
            assert_schema_version("decision", decision)
            plan = _direct_plan_from_blind_decision(analysis, blinded, decision)
            selected_result = evaluate_direct_plan(
                chunk.master_text,
                chunk.units,
                plan,
                review_confidence=args.review_confidence,
                **source_punctuation_kwargs(),
            )
            if not selected_result.valid:
                raise SegmentationError(
                    "03A3 选择未通过硬验收："
                    + json.dumps(selected_result.issues[:8], ensure_ascii=False)
                )
            break
        except SegmentationError as exc:
            decision_error = str(exc)
            decision_payload_parts.append(
                "# PROGRAM_REJECTION\n"
                + decision_error
                + "\n请完整覆盖 03A1 的每个 group_id，并且只能选择对应组中已有的匿名 candidate_id。"
            )
    if plan is None:
        raise SegmentationError(
            "03A3 连续两次未能生成完整可转换决定：" + decision_error
        )
    write_json(chunk_dir / "stage1_decision_raw.json", decision)
    write_json(chunk_dir / "stage1_decision.json", decision)
    write_json(chunk_dir / "api_call_03A3.json", decision_telemetry)
    rows.append({"chunk_id": chunk.chunk_id, "stage": "03A3", **decision_telemetry})
    telemetry = {
        "selection": "03A1_analysis_03A2_batched_three_candidates_03A3_blind_judge",
        "post_a3_policy": "frozen",
        "candidate_diversity": diversity,
        "supplement_used": supplement_telemetry is not None,
        "model": args.model,
    }
    return plan, telemetry, rows


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_two_level_artifacts(
    output_dir: Path,
    master: str,
    units: list[Any],
    display_plan: dict[str, Any],
) -> None:
    """Persist the semantic-boundary and display-layout layers separately."""

    meaning_plan = copy.deepcopy(display_plan)
    for group in meaning_plan.get("groups", []):
        group["line_breaks_after"] = []
    write_json(output_dir / "stage1_meaning_group_plan.json", meaning_plan)
    (output_dir / "stage1_meaning_groups.txt").write_text(
        render_direct_draft(
            master,
            units,
            meaning_plan,
            baseline_punctuation=str(
                source_punctuation_kwargs()["baseline_punctuation"]
            ),
            raised_punctuation=str(
                source_punctuation_kwargs()["raised_punctuation"]
            ),
        ),
        encoding="utf-8",
    )
    write_json(output_dir / "stage1_display_layout_plan.json", display_plan)
    (output_dir / "stage1_display_cues.txt").write_text(
        render_direct_draft(
            master,
            units,
            display_plan,
            baseline_punctuation=str(
                source_punctuation_kwargs()["baseline_punctuation"]
            ),
            raised_punctuation=str(
                source_punctuation_kwargs()["raised_punctuation"]
            ),
        ),
        encoding="utf-8",
    )


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def reuse_json_if_valid(path: Path, stage: str, resume: bool) -> dict[str, Any] | None:
    if not resume or not path.exists():
        return None
    try:
        value = load_json(path)
        assert_schema_version(stage, value)
        return value
    except (OSError, ValueError, json.JSONDecodeError, SegmentationError):
        return None


def merge_chunk_results(
    results: list[tuple[SegmentationChunk, dict[str, Any], dict[str, Any], dict[str, Any]]]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    merged_analysis: dict[str, Any] = {
        "schema_version": "substar.stage1.analysis.v1",
        "source_language": ",".join(
            dict.fromkeys(str(analysis.get("source_language", "Auto")) for _, analysis, _, _ in results)
        ),
        "groups": [],
        "coverage_check": {"complete": True, "ordered": True, "notes": ["由已验证分块顺序合并"]},
    }
    merged_candidates: dict[str, Any] = {
        "schema_version": "substar.stage1.candidates.v1",
        "groups": [],
    }
    merged_decision: dict[str, Any] = {
        "schema_version": "substar.stage1.decision.v1",
        "groups": [],
        "final_draft": "",
    }
    draft_parts: list[str] = []
    global_group_number = 0

    for chunk, analysis, candidates, decision in results:
        candidate_by_group = {group["group_id"]: group for group in candidates["groups"]}
        decision_by_group = {group["group_id"]: group for group in decision["groups"]}
        for analysis_group in analysis["groups"]:
            old_group_id = str(analysis_group["group_id"])
            if old_group_id not in candidate_by_group or old_group_id not in decision_by_group:
                raise SegmentationError(
                    f"{chunk.chunk_id} 的三个阶段 group_id 不一致：{old_group_id}"
                )
            global_group_number += 1
            new_group_id = f"g{global_group_number:04d}"
            analysis_copy = copy.deepcopy(analysis_group)
            analysis_copy["group_id"] = new_group_id
            merged_analysis["groups"].append(analysis_copy)

            candidate_copy = copy.deepcopy(candidate_by_group[old_group_id])
            candidate_copy["group_id"] = new_group_id
            candidate_id_map: dict[str, str] = {}
            for candidate in candidate_copy["candidates"]:
                old_candidate_id = str(candidate["candidate_id"])
                new_candidate_id = f"{chunk.chunk_id}_{old_candidate_id}"
                candidate_id_map[old_candidate_id] = new_candidate_id
                candidate["candidate_id"] = new_candidate_id
            merged_candidates["groups"].append(candidate_copy)

            decision_copy = copy.deepcopy(decision_by_group[old_group_id])
            decision_copy["group_id"] = new_group_id
            selected = str(decision_copy["selected_candidate_id"])
            if selected not in candidate_id_map:
                raise SegmentationError(
                    f"{chunk.chunk_id} 的 03A3 选择不存在候选：{selected}"
                )
            decision_copy["selected_candidate_id"] = candidate_id_map[selected]
            for rejected in decision_copy.get("rejected", []):
                old_rejected = str(rejected.get("candidate_id", ""))
                if old_rejected in candidate_id_map:
                    rejected["candidate_id"] = candidate_id_map[old_rejected]
            merged_decision["groups"].append(decision_copy)

        draft_parts.append(str(decision["final_draft"]).strip())

    merged_decision["final_draft"] = "\n\n".join(draft_parts).strip()
    return merged_analysis, merged_candidates, merged_decision


def finalize(
    *,
    material_path: Path,
    output_dir: Path,
    analysis_path: Path,
    candidates_path: Path,
    decision_path: Path,
) -> bool:
    material = read(material_path)
    master = extract_master(material)
    alignment = extract_alignment(material)
    analysis = load_json(analysis_path)
    candidates = load_json(candidates_path)
    decision = load_json(decision_path)
    assert_schema_version("analysis", analysis)
    assert_schema_version("candidates", candidates)
    assert_schema_version("decision", decision)
    draft = str(decision.get("final_draft", "")).strip() + "\n"
    report = validate_draft(
        master,
        draft,
        analysis=analysis,
        candidates=candidates,
        decision=decision,
        alignment=alignment,
        **source_punctuation_kwargs(),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "stage03A_source_draft.txt").write_text(draft, encoding="utf-8")
    write_validation_report(output_dir / "stage03A_validation_report.json", report)
    return report.valid


def command_prepare(args: argparse.Namespace) -> int:
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir = output_dir / "prompt_snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for stage in ("analysis", "candidates", "decision", "repair", "seams"):
        (snapshot_dir / f"{stage}.md").write_text(
            prompt_text(stage),
            encoding="utf-8",
        )
    material = read(args.material)
    chunks = build_segmentation_chunks(material, args.chunk_seconds)
    prepared: list[str] = []
    for chunk in chunks:
        chunk_dir = output_dir / "chunks" / chunk.chunk_id
        chunk_dir.mkdir(parents=True, exist_ok=True)
        task = "\n\n".join(
            [
                stage_system("direct"),
                "# INPUT_MATERIAL\n" + chunk_material_for_model(chunk),
            ]
        )
        task_path = chunk_dir / "03A_index_task.md"
        task_path.write_text(task, encoding="utf-8")
        prepared.append(str(task_path))
    write_json(
        output_dir / "stage1_chunk_manifest.json",
        {
            "schema_version": "substar.stage1.chunks.v1",
            "chunk_seconds": args.chunk_seconds,
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "start_seconds": chunk.start_seconds,
                    "end_seconds": chunk.end_seconds,
                    "task": str(Path("chunks") / chunk.chunk_id / "03A_index_task.md"),
                }
                for chunk in chunks
            ],
        },
    )
    if len(chunks) == 1:
        (output_dir / "03A_index_task.md").write_text(
            (output_dir / "chunks" / chunks[0].chunk_id / "03A_index_task.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    (output_dir / "NEXT_STEPS.txt").write_text(
        f"本素材已按强边界拆成 {len(chunks)} 块，清单见 stage1_chunk_manifest.json。\n"
        "1. 每块的 03A_index_task.md 交给模型，只返回 alignment 索引计划。\n"
        "2. api 模式会自动程序重建、硬校验，并只在失败或低置信时执行定向复议。\n"
        "3. legacy-api 保留旧 03A1/03A2/03A3 三阶段用于回归比较。\n",
        encoding="utf-8",
    )
    print(f"prepared_chunks={len(prepared)}")
    for item in prepared:
        print(f"prepared={item}")
    return 0


def command_legacy_api(args: argparse.Namespace) -> int:
    api_key, key_source = resolve_api_key(args.api_key_env)
    if not api_key:
        raise SegmentationError(
            f"没有找到 LLM 密钥。正式任务必须由 Scheduler 通过 {args.api_key_env} 授权。"
        )
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    material = read(args.material)
    chunks = build_segmentation_chunks(material, args.chunk_seconds)
    total_chunks = len(chunks)
    if args.max_chunks is not None:
        chunks = chunks[: args.max_chunks]
    write_json(
        output_dir / "stage1_chunk_manifest.json",
        {
            "schema_version": "substar.stage1.chunks.v1",
            "chunk_seconds": args.chunk_seconds,
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "start_seconds": chunk.start_seconds,
                    "end_seconds": chunk.end_seconds,
                    "alignment_start": chunk.units[0].index,
                    "alignment_end": chunk.units[-1].index,
                    "master_characters": len(chunk.master_text),
                }
                for chunk in chunks
            ],
        },
    )
    print(
        f"chunks={len(chunks)}/{total_chunks} chunk_seconds={args.chunk_seconds} "
        f"thinking={args.thinking_mode} effort={args.reasoning_effort} key_source={key_source}"
    )
    results: list[tuple[SegmentationChunk, dict[str, Any], dict[str, Any], dict[str, Any]]] = []

    for chunk_number, chunk in enumerate(chunks, start=1):
        chunk_dir = output_dir / "chunks" / chunk.chunk_id
        chunk_dir.mkdir(parents=True, exist_ok=True)
        chunk_material = chunk_material_for_model(chunk)
        (chunk_dir / "chatbox_material.md").write_text(chunk_material, encoding="utf-8")
        print(f"chunk={chunk.chunk_id} progress={chunk_number}/{len(chunks)} stage=03A1")

        analysis_path = chunk_dir / "stage1_analysis.json"
        analysis = reuse_json_if_valid(analysis_path, "analysis", args.resume)
        if analysis is None:
            analysis, telemetry = call_model(
                base_url=args.base_url,
                api_key=api_key,
                model=args.model,
                system_prompt=stage_system("analysis", chunk_material),
                user_payload="# INPUT_MATERIAL\n" + chunk_material,
                timeout=args.timeout,
                max_tokens=args.max_tokens,
                json_mode=not args.no_json_mode,
                thinking_mode=thinking_for_stage(args.thinking_mode, "analysis"),
                reasoning_effort=args.reasoning_effort,
                request_attempts=args.http_attempts,
            )
            write_json(chunk_dir / "api_call_03A1.json", telemetry)
        assert_schema_version("analysis", analysis)
        write_json(analysis_path, analysis)

        print(f"chunk={chunk.chunk_id} progress={chunk_number}/{len(chunks)} stage=03A2")
        candidates_path = chunk_dir / "stage1_candidates.json"
        candidates = reuse_json_if_valid(candidates_path, "candidates", args.resume)
        if candidates is None:
            candidates, telemetry = call_model(
                base_url=args.base_url,
                api_key=api_key,
                model=args.model,
                system_prompt=stage_system("candidates", chunk_material),
                user_payload="\n\n".join(
                    [
                        "# INPUT_MATERIAL\n" + chunk_material,
                        "# STAGE1_ANALYSIS\n" + json.dumps(analysis, ensure_ascii=False),
                    ]
                ),
                timeout=args.timeout,
                max_tokens=args.max_tokens,
                json_mode=not args.no_json_mode,
                thinking_mode=thinking_for_stage(args.thinking_mode, "candidates"),
                reasoning_effort=args.reasoning_effort,
                request_attempts=args.http_attempts,
            )
            write_json(chunk_dir / "api_call_03A2.json", telemetry)
        assert_schema_version("candidates", candidates)
        write_json(candidates_path, candidates)

        blinded = blind_candidates(candidates, args.blind_seed + chunk_number)
        write_json(chunk_dir / "stage1_candidates_blinded.json", blinded)
        print(f"chunk={chunk.chunk_id} progress={chunk_number}/{len(chunks)} stage=03A3")
        decision_path = chunk_dir / "stage1_decision.json"
        decision = reuse_json_if_valid(decision_path, "decision", args.resume)
        if decision is None:
            decision, telemetry = call_model(
                base_url=args.base_url,
                api_key=api_key,
                model=args.model,
                system_prompt=stage_system("decision", chunk_material),
                user_payload="\n\n".join(
                    [
                        "# INPUT_MATERIAL\n" + chunk_material,
                        "# STAGE1_ANALYSIS\n" + json.dumps(analysis, ensure_ascii=False),
                        "# BLINDED_CANDIDATES\n" + json.dumps(blinded, ensure_ascii=False),
                    ]
                ),
                timeout=args.timeout,
                max_tokens=args.max_tokens,
                json_mode=not args.no_json_mode,
                thinking_mode=thinking_for_stage(args.thinking_mode, "decision"),
                reasoning_effort=args.reasoning_effort,
                request_attempts=args.http_attempts,
            )
            write_json(chunk_dir / "api_call_03A3.json", telemetry)
        assert_schema_version("decision", decision)
        write_json(decision_path, decision)
        chunk_draft = str(decision.get("final_draft", "")).strip() + "\n"
        chunk_report = validate_draft(
            chunk.master_text,
            chunk_draft,
            analysis=analysis,
            candidates=candidates,
            decision=decision,
            alignment=chunk.units,
            **source_punctuation_kwargs(),
        )
        (chunk_dir / "stage03A_source_draft.txt").write_text(chunk_draft, encoding="utf-8")
        write_validation_report(chunk_dir / "stage03A_validation_report.json", chunk_report)
        if not chunk_report.valid:
            raise SegmentationError(
                f"{chunk.chunk_id} 硬校验失败，见 {chunk_dir / 'stage03A_validation_report.json'}"
            )
        results.append((chunk, analysis, candidates, decision))

    analysis, candidates, decision = merge_chunk_results(results)
    analysis_path = output_dir / "stage1_analysis.json"
    candidates_path = output_dir / "stage1_candidates.json"
    decision_path = output_dir / "stage1_decision.json"
    write_json(analysis_path, analysis)
    write_json(candidates_path, candidates)
    write_json(decision_path, decision)
    if len(chunks) == total_chunks:
        valid = finalize(
            material_path=args.material,
            output_dir=output_dir,
            analysis_path=analysis_path,
            candidates_path=candidates_path,
            decision_path=decision_path,
        )
    else:
        partial_master = " ".join(chunk.master_text for chunk in chunks)
        partial_alignment = [unit for chunk in chunks for unit in chunk.units]
        partial_draft = str(decision["final_draft"]).strip() + "\n"
        partial_report = validate_draft(
            partial_master,
            partial_draft,
            analysis=analysis,
            candidates=candidates,
            decision=decision,
            alignment=partial_alignment,
            **source_punctuation_kwargs(),
        )
        (output_dir / "stage03A_source_draft.txt").write_text(partial_draft, encoding="utf-8")
        write_validation_report(output_dir / "stage03A_validation_report.json", partial_report)
        valid = partial_report.valid
    print(f"validation={'passed' if valid else 'failed'}")
    return 0 if valid else 2


def _direct_report(result: Any, *, repaired: bool, attempts: int) -> dict[str, Any]:
    validation = copy.deepcopy(result.validation)
    return {
        "schema_version": "substar.stage1.direct-validation.v1",
        "valid": result.valid,
        "repaired": repaired,
        "repair_attempts": attempts,
        "plan_issues": result.issues,
        "review_notices": result.review_notices,
        "draft_validation": validation,
    }


SEMANTIC_REPAIR_CODES = {
    "comparative_complement_cut",
    "copula_complement_cut",
    "dangling_function_phrase",
    "dangling_line_end",
    "cross_group_dangling_phrase",
    "crossed_sentence_boundary",
    "mergeable_short_cue",
    "multiple_independent_clause_centers",
    "orphan_short_object",
    "suspected_named_entity_apposition_cut",
    "sub_minimum_duration_cue",
}


def _semantic_repairs(result: Any) -> list[dict[str, Any]]:
    """Return only high-confidence, locally repairable boundary failures."""

    return [
        notice
        for notice in result.review_notices
        if str(notice.get("code", "")) in SEMANTIC_REPAIR_CODES
    ]


def _direct_candidate_score(
    *,
    master: str,
    units: list[Any],
    plan: dict[str, Any],
    review_confidence: float,
) -> tuple[tuple[float, ...], dict[str, Any]]:
    """Return a generic lexicographic quality score for blind candidates."""

    groups = list(plan.get("groups", []))
    model_cue_counts = [
        len(group.get("line_breaks_after", [])) + 1 for group in groups
    ]
    raw_ranges = _unit_original_ranges(master, units)
    ranges = {
        unit.index: raw_ranges[position] for position, unit in enumerate(units)
    }
    profile = source_punctuation_kwargs()
    estimated_cue_counts: list[int] = []
    for group, model_count in zip(groups, model_cue_counts):
        start = int(group["alignment_start"])
        end = int(group["alignment_end"])
        text = master[ranges[start][0] : ranges[end][1]]
        visible = configured_length(
            text,
            count_spaces=bool(profile["english_count_spaces"]),
            count_punctuation=bool(profile["english_count_punctuation"]),
        )
        language = classify_language(text)
        if language in {"mixed_zh", "mixed_en"}:
            hard_limit = int(profile["mixed_hard_limit"])
        elif language == "zh":
            hard_limit = int(profile["chinese_hard_limit"])
        elif language == "ja":
            hard_limit = int(profile["japanese_hard_limit"])
        elif language == "ko":
            hard_limit = int(profile["korean_hard_limit"])
        else:
            hard_limit = int(profile["english_hard_limit"])
        minimum_by_length = max(1, (visible + hard_limit - 1) // hard_limit)
        estimated_cue_counts.append(max(model_count, minimum_by_length))
    maximum_cues = max(estimated_cue_counts, default=0)
    optimized = optimize_valid_plan(master, units, plan)
    result = evaluate_direct_plan(
        master,
        units,
        optimized.plan,
        review_confidence=review_confidence,
        **source_punctuation_kwargs(),
    )
    semantic_repairs = _semantic_repairs(result)
    semantic_count = len(semantic_repairs)
    critical_codes = {
        "crossed_sentence_boundary",
        "dangling_line_end",
        "multiple_independent_clause_centers",
        "suspected_named_entity_apposition_cut",
    }
    critical_count = sum(
        str(notice.get("code", "")) in critical_codes
        for notice in semantic_repairs
    )
    review_count = sum(bool(group.get("needs_review")) for group in groups)
    final_groups = list(optimized.plan.get("groups", []))
    cue_total = sum(
        len(group.get("line_breaks_after", [])) + 1
        for group in final_groups
    )
    group_total = len(final_groups)
    score = (
        0.0 if result.valid else 1.0,
        float(critical_count),
        float(semantic_count),
        float(review_count),
    )
    report = {
        "valid": result.valid,
        "raw_group_count": len(groups),
        "raw_model_cue_count": sum(model_cue_counts),
        "estimated_maximum_group_cues": maximum_cues,
        "final_group_count": group_total,
        "final_cue_count": cue_total,
        "critical_boundary_notice_count": critical_count,
        "semantic_repair_notice_count": semantic_count,
        "model_review_group_count": review_count,
        "score": list(score),
    }
    return score, report


def _process_direct_chunk(
    *,
    chunk: SegmentationChunk,
    chunk_number: int,
    chunk_count: int,
    output_dir: Path,
    args: argparse.Namespace,
    api_key: str,
) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    chunk_dir = output_dir / "chunks" / chunk.chunk_id
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk_material = chunk_material_for_model(chunk)
    (chunk_dir / "chatbox_material.md").write_text(chunk_material, encoding="utf-8")
    print(
        f"chunk={chunk.chunk_id} progress={chunk_number}/{chunk_count} stage=03A",
        flush=True,
    )
    rows: list[dict[str, Any]] = []
    plan_path = chunk_dir / "stage1_direct_plan.json"
    final_plan_path = chunk_dir / "stage1_direct_plan_final.json"
    resumed_final = reuse_json_if_valid(final_plan_path, "direct", args.resume)
    plan = resumed_final or reuse_json_if_valid(plan_path, "direct", args.resume)
    frozen_a3_plan = False
    if plan is None and args.offline:
        plan = build_deterministic_fallback_plan(chunk.master_text, chunk.units)
        telemetry = {
            "fallback": "deterministic_offline",
            "reason": "offline requested or API key unavailable",
        }
        write_json(chunk_dir / "api_call_03A.json", telemetry)
        rows.append({"chunk_id": chunk.chunk_id, "stage": "03A", **telemetry})
    elif plan is None:
        try:
            plan, telemetry, stage_rows = _request_batched_blind_plan(
                chunk=chunk,
                chunk_material=chunk_material,
                chunk_number=chunk_number,
                args=args,
                api_key=api_key,
                chunk_dir=chunk_dir,
            )
            rows.extend(stage_rows)
            frozen_a3_plan = True
        except (OSError, ValueError, SegmentationError) as exc:
            plan = build_deterministic_fallback_plan(chunk.master_text, chunk.units)
            telemetry = {
                "fallback": "deterministic_delivery",
                "error": str(exc),
                "model": args.model,
            }
        write_json(chunk_dir / "api_call_03A.json", telemetry)
        rows.append({"chunk_id": chunk.chunk_id, "stage": "03A", **telemetry})
    assert_schema_version("direct", plan)
    plan = apply_text_cleanup_policy(plan)
    if frozen_a3_plan or (
        resumed_final is not None
        and (chunk_dir / "stage1_a3_frozen_plan.json").exists()
    ):
        write_json(chunk_dir / "stage1_a3_frozen_plan.json", plan)
        write_json(
            chunk_dir / "stage1_boundary_fingerprint.json",
            cuts_fingerprint(plan),
        )
    else:
        # Only offline/delivery fallbacks retain the legacy optimizer.
        optimized = optimize_valid_plan(chunk.master_text, chunk.units, plan)
        plan = optimized.plan
        if optimized.actions:
            write_json(
                chunk_dir / "stage1_deterministic_repairs.json",
                optimized.actions,
            )
    write_json(plan_path, plan)

    result = evaluate_direct_plan(
        chunk.master_text,
        chunk.units,
        plan,
        review_confidence=args.review_confidence,
        **source_punctuation_kwargs(),
    )
    attempts = 0
    if resumed_final is not None:
        prior_report_path = chunk_dir / "stage03A_validation_report.json"
        if prior_report_path.exists():
            try:
                attempts = int(load_json(prior_report_path).get("repair_attempts", 0))
            except (OSError, ValueError, TypeError):
                attempts = 0
    repaired = attempts > 0
    effective_repair_attempts = (
        0 if frozen_a3_plan else min(args.repair_attempts, 2)
    )
    while (
        not result.valid
        and attempts < effective_repair_attempts
    ):
        attempts += 1
        repaired = True
        print(
            f"chunk={chunk.chunk_id} stage=03A-R attempt={attempts} "
            f"issues={len(result.issues)} notices={len(result.review_notices)}",
            flush=True,
        )
        try:
            repaired_plan, telemetry = call_model(
                base_url=args.base_url,
                api_key=api_key,
                model=args.model,
                system_prompt=stage_system("repair", chunk_material),
                user_payload="\n\n".join(
                    [
                        "# INPUT_MATERIAL\n" + chunk_material,
                        "# INITIAL_PLAN\n" + json.dumps(plan, ensure_ascii=False),
                        "# PROGRAM_ISSUES\n"
                        + json.dumps(
                            result.issues + _semantic_repairs(result),
                            ensure_ascii=False,
                        ),
                    ]
                ),
                timeout=min(args.timeout, 300),
                max_tokens=args.max_tokens,
                json_mode=not args.no_json_mode,
                thinking_mode="disabled",
                reasoning_effort=args.reasoning_effort,
                request_attempts=args.http_attempts,
            )
            assert_schema_version("repair", repaired_plan)
        except (OSError, ValueError, SegmentationError) as exc:
            write_json(
                chunk_dir / f"api_call_03A_R_{attempts}.json",
                {
                    "fallback": "deterministic_delivery",
                    "error": str(exc),
                    "model": args.model,
                },
            )
            break
        repaired_plan = apply_text_cleanup_policy(repaired_plan)
        optimized = optimize_valid_plan(
            chunk.master_text,
            chunk.units,
            repaired_plan,
        )
        plan = optimized.plan
        if optimized.actions:
            write_json(
                chunk_dir / f"stage1_deterministic_repairs_after_llm_{attempts}.json",
                optimized.actions,
            )
        write_json(chunk_dir / f"stage1_direct_plan_repair_{attempts}.json", plan)
        write_json(chunk_dir / f"api_call_03A_R_{attempts}.json", telemetry)
        rows.append({"chunk_id": chunk.chunk_id, "stage": f"03A-R-{attempts}", **telemetry})
        result = evaluate_direct_plan(
            chunk.master_text,
            chunk.units,
            plan,
            review_confidence=args.review_confidence,
            **source_punctuation_kwargs(),
        )

    if not result.valid:
        original_issues = copy.deepcopy(result.issues)
        plan = build_deterministic_fallback_plan(chunk.master_text, chunk.units)
        optimized = optimize_direct_plan(
            chunk.master_text,
            chunk.units,
            plan,
            **source_punctuation_kwargs(),
        )
        plan = optimized.plan
        result = evaluate_direct_plan(
            chunk.master_text,
            chunk.units,
            plan,
            review_confidence=args.review_confidence,
            **source_punctuation_kwargs(),
        )
        write_json(
            chunk_dir / "stage1_deterministic_delivery_fallback.json",
            {
                "used": True,
                "valid": result.valid,
                "original_issues": original_issues,
                "actions": optimized.actions,
            },
        )

    write_json(final_plan_path, plan)
    if frozen_a3_plan:
        final_fingerprint = cuts_fingerprint(plan)
        frozen_fingerprint = load_json(
            chunk_dir / "stage1_boundary_fingerprint.json"
        )
        if (
            final_fingerprint["meaning_group_boundary_hash"]
            != frozen_fingerprint["meaning_group_boundary_hash"]
            or final_fingerprint["cue_boundary_hash"]
            != frozen_fingerprint["cue_boundary_hash"]
        ):
            raise SegmentationError(f"{chunk.chunk_id} A3 冻结切点被后处理修改")
    write_two_level_artifacts(
        chunk_dir,
        chunk.master_text,
        chunk.units,
        plan,
    )
    (chunk_dir / "stage03A_source_draft.txt").write_text(result.draft, encoding="utf-8")
    write_json(
        chunk_dir / "stage03A_validation_report.json",
        _direct_report(result, repaired=repaired, attempts=attempts),
    )
    if not result.valid:
        raise SegmentationError(
            f"{chunk.chunk_id} 索引计划复议后仍未通过，见 "
            f"{chunk_dir / 'stage03A_validation_report.json'}"
        )
    print(f"chunk={chunk.chunk_id} complete=true", flush=True)
    return chunk_number - 1, plan, rows


def _reconcile_all_seams(
    *,
    plans: list[dict[str, Any]],
    chunks: list[SegmentationChunk],
    master: str,
    units: list[Any],
    output_dir: Path,
    args: argparse.Namespace,
    api_key: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    merged = merge_direct_plans(plans)
    if len(plans) <= 1:
        return merged, None
    merged_groups = merged["groups"]
    windows: list[dict[str, Any]] = []
    group_cursor = 0
    for number in range(len(plans) - 1):
        left_count = len(plans[number]["groups"])
        right_count = len(plans[number + 1]["groups"])
        left_group = merged_groups[group_cursor + left_count - 1]
        right_group = merged_groups[group_cursor + left_count]
        left_position = group_cursor + left_count - 1
        right_position = left_position + 1
        window_start = int(left_group["alignment_start"])
        window_end = int(right_group["alignment_end"])
        window_units = [
            unit
            for unit in units
            if window_start <= int(unit.index) <= window_end
        ]
        windows.append(
            {
                "seam_id": f"s{number + 1:04d}",
                "left_chunk": chunks[number].chunk_id,
                "right_chunk": chunks[number + 1].chunk_id,
                "window_start": window_start,
                "window_end": window_end,
                "alignment": [
                    {
                        "index": int(unit.index),
                        "start": round(float(unit.start), 3),
                        "end": round(float(unit.end), 3),
                        "text": unit.text,
                    }
                    for unit in window_units
                ],
                "current_groups": [left_group, right_group],
                "readonly_before": (
                    [merged_groups[left_position - 1]]
                    if left_position > 0
                    else []
                ),
                "readonly_after": (
                    [merged_groups[right_position + 1]]
                    if right_position + 1 < len(merged_groups)
                    else []
                ),
            }
        )
        group_cursor += left_count

    seam_result_path = output_dir / "stage1_seam_decision.json"
    telemetry_path = output_dir / "api_call_03A_S.json"
    if args.offline:
        optimized = optimize_direct_plan(
            master,
            units,
            merged,
            **source_punctuation_kwargs(),
        )
        return optimized.plan, {
            "fallback": "deterministic_seam_offline",
            "actions": len(optimized.actions),
        }
    if args.resume and seam_result_path.exists():
        seam_result = load_json(seam_result_path)
        telemetry = load_json(telemetry_path) if telemetry_path.exists() else None
    else:
        seam_result, telemetry = call_model(
            base_url=args.base_url,
            api_key=api_key,
            model=args.model,
            system_prompt="\n\n".join(
                [
                    prompt_text("seams"),
                    shared_context(json.dumps(windows, ensure_ascii=False)),
                ]
            ),
            user_payload="# ALL_SEAM_WINDOWS\n"
            + json.dumps({"seams": windows}, ensure_ascii=False),
            timeout=min(args.timeout, 300),
            max_tokens=args.max_tokens,
            json_mode=not args.no_json_mode,
            thinking_mode="enabled" if args.thinking_mode == "hybrid" else args.thinking_mode,
            reasoning_effort=args.reasoning_effort,
            request_attempts=args.http_attempts,
        )
        write_json(seam_result_path, seam_result)
        write_json(telemetry_path, telemetry)

    if seam_result.get("schema_version") != "substar.stage1.seams.v1":
        raise SegmentationError("03A-S schema_version 错误")
    decisions = seam_result.get("seams")
    if not isinstance(decisions, list):
        raise SegmentationError("03A-S 缺少 seams")
    expected_ids = [window["seam_id"] for window in windows]
    if [item.get("seam_id") for item in decisions] != expected_ids:
        raise SegmentationError("03A-S 未按顺序完整返回全部接缝")

    replacements: dict[int, tuple[int, list[dict[str, Any]]]] = {}
    unit_by_index = {int(unit.index): unit for unit in units}
    for window, decision in zip(windows, decisions):
        start = int(window["window_start"])
        end = int(window["window_end"])
        if int(decision.get("window_start", -1)) != start or int(
            decision.get("window_end", -1)
        ) != end:
            raise SegmentationError(f"{window['seam_id']} 擅自修改了窗口范围")
        replacement_groups = decision.get("groups")
        if not isinstance(replacement_groups, list) or not replacement_groups:
            raise SegmentationError(f"{window['seam_id']} 没有返回替换 groups")
        normalized_groups: list[dict[str, Any]] = []
        for position, group in enumerate(replacement_groups, start=1):
            copied = copy.deepcopy(group)
            copied["group_id"] = f"g{position:04d}"
            normalized_groups.append(copied)
        local_plan = {
            "schema_version": "substar.stage1.direct.v1",
            "source_language": merged.get("source_language", "Auto"),
            "groups": normalized_groups,
            "coverage_check": {"complete": True, "ordered": True},
        }
        local_units = [unit_by_index[index] for index in range(start, end + 1)]
        issues = structural_issues(local_plan, local_units)
        if issues:
            raise SegmentationError(
                f"{window['seam_id']} 接缝计划未通过结构验收："
                + json.dumps(issues[:5], ensure_ascii=False)
            )
        replacements[start] = (end, normalized_groups)

    rebuilt_groups: list[dict[str, Any]] = []
    position = 0
    while position < len(merged_groups):
        group = merged_groups[position]
        start = int(group["alignment_start"])
        replacement = replacements.get(start)
        if replacement is None:
            rebuilt_groups.append(copy.deepcopy(group))
            position += 1
            continue
        window_end, replacement_groups = replacement
        rebuilt_groups.extend(copy.deepcopy(replacement_groups))
        position += 1
        while (
            position < len(merged_groups)
            and int(merged_groups[position]["alignment_end"]) <= window_end
        ):
            position += 1
    for number, group in enumerate(rebuilt_groups, start=1):
        group["group_id"] = f"g{number:04d}"
    reconciled = {
        "schema_version": "substar.stage1.direct.v1",
        "source_language": merged.get("source_language", "Auto"),
        "groups": rebuilt_groups,
        "coverage_check": {"complete": True, "ordered": True},
    }
    reconciled = apply_text_cleanup_policy(reconciled)
    optimized = optimize_direct_plan(
        master,
        units,
        reconciled,
        **source_punctuation_kwargs(),
    )
    reconciled = optimized.plan
    if optimized.actions:
        write_json(output_dir / "stage1_seam_deterministic_repairs.json", optimized.actions)
    final_check = evaluate_direct_plan(
        master,
        units,
        reconciled,
        review_confidence=args.review_confidence,
        **source_punctuation_kwargs(),
    )
    if not final_check.valid:
        raise SegmentationError(
            "03A-S 合并后未通过全片硬校验："
            + json.dumps(final_check.issues[:8], ensure_ascii=False)
        )
    return reconciled, telemetry


def command_api(args: argparse.Namespace) -> int:
    api_key, key_source = resolve_api_key(args.api_key_env)
    if args.offline:
        api_key = ""
        key_source = "deterministic_offline"
    elif not api_key:
        args.offline = True
        key_source = "deterministic_offline"
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir = output_dir / "prompt_snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for stage in ("analysis", "candidates", "decision", "repair", "seams"):
        (snapshot_dir / f"{stage}.md").write_text(
            prompt_text(stage),
            encoding="utf-8",
        )
    material = read(args.material)
    chunks = build_segmentation_chunks(material, args.chunk_seconds)
    total_chunks = len(chunks)
    if args.max_chunks is not None:
        chunks = chunks[: args.max_chunks]

    run_config = {
        "schema_version": "substar.stage1.direct-run.v1",
        "material_sha256": text_sha256(material),
        "base_url": args.base_url.rstrip("/"),
        "model": args.model,
        "thinking_mode": args.thinking_mode,
        "reasoning_effort": args.reasoning_effort,
        "chunk_seconds": args.chunk_seconds,
        "max_chunks": args.max_chunks,
        "direct_candidates": args.direct_candidates,
        "repair_attempts": args.repair_attempts,
        "review_confidence": args.review_confidence,
        "json_mode": not args.no_json_mode,
        "prompt_override_dir": (
            str(Path(PROMPT_OVERRIDE_DIR).resolve()) if PROMPT_OVERRIDE_DIR else None
        ),
        "sentence_hint_mode": SENTENCE_HINT_MODE,
        "prompt_sha256": text_sha256(
            "\n".join(
                value
                for value in (
                    prompt_text("analysis"),
                    prompt_text("candidates"),
                    prompt_text("decision"),
                    prompt_text("repair"),
                    read(SPEC),
                    read(CASES),
                )
            )
        ),
        "active_output_profile": source_punctuation_kwargs(),
        "glossary_sha256": text_sha256(
            json.dumps(
                active_glossary(
                    str(load_settings(include_secret=False).get("glossary_id", ""))
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
        ),
    }
    run_config_path = output_dir / "stage1_run_config.json"
    if args.resume and run_config_path.exists():
        previous_config = load_json(run_config_path)
        previous_comparable = dict(previous_config)
        current_comparable = dict(run_config)
        previous_comparable.pop("repair_attempts", None)
        current_comparable.pop("repair_attempts", None)
        if previous_comparable != current_comparable:
            raise SegmentationError(
                "--resume 的运行参数或素材与已有结果不同；请换输出目录或去掉 --resume。"
            )
    write_json(run_config_path, run_config)
    write_json(
        output_dir / "stage1_chunk_manifest.json",
        {
            "schema_version": "substar.stage1.chunks.v1",
            "chunk_seconds": args.chunk_seconds,
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "start_seconds": chunk.start_seconds,
                    "end_seconds": chunk.end_seconds,
                    "alignment_start": chunk.units[0].index,
                    "alignment_end": chunk.units[-1].index,
                    "master_characters": len(chunk.master_text),
                }
                for chunk in chunks
            ],
        },
    )
    print(
        f"mode=direct chunks={len(chunks)}/{total_chunks} "
        f"thinking={args.thinking_mode} effort={args.reasoning_effort} key_source={key_source}"
    )

    accepted_plans: list[dict[str, Any] | None] = [None] * len(chunks)
    telemetry_rows: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(
                _process_direct_chunk,
                chunk=chunk,
                chunk_number=chunk_number,
                chunk_count=len(chunks),
                output_dir=output_dir,
                args=args,
                api_key=api_key,
            )
            for chunk_number, chunk in enumerate(chunks, start=1)
        ]
        for future in concurrent.futures.as_completed(futures):
            position, plan, rows = future.result()
            accepted_plans[position] = plan
            telemetry_rows.extend(rows)

    completed_plans = [plan for plan in accepted_plans if plan is not None]
    full_master = extract_master(material)
    full_units = extract_alignment(material)
    try:
        merged_plan, seam_telemetry = _reconcile_all_seams(
            plans=completed_plans,
            chunks=chunks,
            master=full_master,
            units=full_units,
            output_dir=output_dir,
            args=args,
            api_key=api_key,
        )
    except (OSError, ValueError, SegmentationError) as exc:
        merged_plan = merge_direct_plans(completed_plans)
        optimized = optimize_direct_plan(
            full_master,
            full_units,
            merged_plan,
            **source_punctuation_kwargs(),
        )
        merged_plan = optimized.plan
        seam_telemetry = {
            "fallback": "deterministic_seam_delivery",
            "error": str(exc),
            "actions": len(optimized.actions),
        }
        write_json(output_dir / "stage1_seam_fallback.json", seam_telemetry)
    if seam_telemetry is not None:
        telemetry_rows.append({"chunk_id": "all", "stage": "03A-S", **seam_telemetry})
    write_json(output_dir / "stage1_direct_plan.json", merged_plan)
    if len(chunks) == total_chunks:
        validation_master = extract_master(material)
        validation_units = extract_alignment(material)
    else:
        validation_master = " ".join(chunk.master_text for chunk in chunks)
        validation_units = [unit for chunk in chunks for unit in chunk.units]
    merged_result = evaluate_direct_plan(
        validation_master,
        validation_units,
        merged_plan,
        review_confidence=args.review_confidence,
        **source_punctuation_kwargs(),
    )
    write_two_level_artifacts(
        output_dir,
        validation_master,
        validation_units,
        merged_plan,
    )
    (output_dir / "stage03A_source_draft.txt").write_text(
        merged_result.draft, encoding="utf-8"
    )
    write_json(
        output_dir / "stage03A_validation_report.json",
        _direct_report(merged_result, repaired=False, attempts=0),
    )
    if not telemetry_rows:
        for chunk in chunks:
            chunk_dir = output_dir / "chunks" / chunk.chunk_id
            for path in sorted(chunk_dir.glob("api_call_03A*.json")):
                row = load_json(path)
                telemetry_rows.append(
                    {
                        "chunk_id": chunk.chunk_id,
                        "stage": path.stem.removeprefix("api_call_"),
                        **row,
                    }
                )
    write_json(
        output_dir / "api_usage_summary.json",
        {
            "schema_version": "substar.stage1.api-usage.v1",
            "calls": telemetry_rows,
            "duration_seconds": round(
                sum(float(row.get("duration_seconds", 0)) for row in telemetry_rows), 3
            ),
            "total_tokens": sum(
                int(row.get("usage", {}).get("total_tokens", 0)) for row in telemetry_rows
            ),
            "reasoning_tokens": sum(
                int(
                    row.get("usage", {})
                    .get("completion_tokens_details", {})
                    .get("reasoning_tokens", 0)
                )
                for row in telemetry_rows
            ),
        },
    )
    print(f"validation={'passed' if merged_result.valid else 'failed'}")
    return 0 if merged_result.valid else 2


def command_finalize(args: argparse.Namespace) -> int:
    valid = finalize(
        material_path=args.material,
        output_dir=args.output_dir,
        analysis_path=args.analysis,
        candidates_path=args.candidates,
        decision_path=args.decision,
    )
    print(f"validation={'passed' if valid else 'failed'}")
    return 0 if valid else 2


def command_validate_draft(args: argparse.Namespace) -> int:
    material = read(args.material)
    draft = read(args.draft)
    report = validate_draft(
        extract_master(material),
        draft,
        **source_punctuation_kwargs(),
    )
    report_path: Path = args.report
    write_validation_report(report_path, report)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.valid else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Substar 03A 索引式源文切分入口")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="生成可交给 ChatBox 的索引式 03A 任务包")
    prepare.add_argument("material", type=Path)
    prepare.add_argument("--output-dir", required=True, type=Path)
    prepare.add_argument("--chunk-seconds", type=float, default=120.0)
    prepare.set_defaults(func=command_prepare)

    def add_api_arguments(command: argparse.ArgumentParser, *, legacy: bool = False) -> None:
        command.add_argument("material", type=Path)
        command.add_argument("--output-dir", required=True, type=Path)
        command.add_argument("--base-url", default="https://api.deepseek.com")
        command.add_argument("--model", default="deepseek-v4-flash")
        command.add_argument("--api-key-env", default="SUBSTAR_LLM_API_KEY")
        command.add_argument("--timeout", type=int, default=300)
        command.add_argument("--max-tokens", type=int, default=32768)
        command.add_argument("--chunk-seconds", type=float, default=120.0)
        command.add_argument("--max-chunks", type=int)
        command.add_argument("--workers", type=int, default=1, choices=range(1, 257))
        command.add_argument("--http-attempts", type=int, default=2, choices=range(1, 5))
        command.add_argument(
            "--thinking-mode",
            choices=["enabled", "disabled", "hybrid"],
            default="enabled",
        )
        command.add_argument("--reasoning-effort", choices=["low", "medium", "high", "max", "xhigh"], default="high")
        command.add_argument("--resume", action="store_true")
        command.add_argument(
            "--offline",
            action="store_true",
            help="不调用 LLM，直接使用确定性切分兜底",
        )
        command.add_argument("--no-json-mode", action="store_true")
        if legacy:
            command.add_argument("--blind-seed", type=int, default=20260722)

    api = subparsers.add_parser(
        "api",
        help="一次思考输出索引计划，程序重建；仅失败或低置信时定向复议",
    )
    add_api_arguments(api)
    api.add_argument("--repair-attempts", type=int, default=1, choices=range(0, 3))
    api.add_argument("--review-confidence", type=float, default=0.72)
    api.add_argument(
        "--direct-candidates",
        type=int,
        default=1,
        choices=(1, 2),
        help="每块并发生成的直接计划候选数；有限盲门择优且永不覆盖原候选",
    )
    api.set_defaults(func=command_api)

    legacy_api = subparsers.add_parser(
        "legacy-api",
        help="旧 03A1/03A2/03A3 三阶段回归入口",
    )
    add_api_arguments(legacy_api, legacy=True)
    legacy_api.set_defaults(func=command_legacy_api)

    final = subparsers.add_parser("finalize", help="从三份模型 JSON 生成并硬校验源文草案")
    final.add_argument("material", type=Path)
    final.add_argument("--output-dir", required=True, type=Path)
    final.add_argument("--analysis", required=True, type=Path)
    final.add_argument("--candidates", required=True, type=Path)
    final.add_argument("--decision", required=True, type=Path)
    final.set_defaults(func=command_finalize)

    validate = subparsers.add_parser("validate-draft", help="硬校验一份成稿式源文草案")
    validate.add_argument("material", type=Path)
    validate.add_argument("--draft", required=True, type=Path)
    validate.add_argument("--report", required=True, type=Path)
    validate.set_defaults(func=command_validate_draft)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except (OSError, ValueError, SegmentationError) as exc:
        print(f"SUBSTAR_STAGE1_ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
