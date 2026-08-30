from __future__ import annotations

import json
import hashlib
import math
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from .openai_compat import auth_headers, endpoint_url

from substar_core.editor.tasks.repository import (
    EditorAiTaskCancelled,
    current_task_id,
    raise_if_task_cancelled,
    task_cancellation_requested,
)
from substar_core.process_command import python_script_command

from .asr_longform import lexical_tokens
from .policy import (
    SubtitlePolicy,
    base_language,
    classify_language,
    track_lines,
)
from .reasoning_capabilities import (
    reasoning_effort_for_request,
    resolve_thinking_mode,
)
from .segmentation.material import (
    display_normalize,
    illegal_lower_punctuation,
    project_annotations,
    split_groups,
)


class Stage2Error(RuntimeError):
    pass


class Stage2RequestError(Stage2Error):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status in {408, 425, 429} or (
            self.status is not None and self.status >= 500
        )


def _response_json_utf8(response: requests.Response) -> Any:
    """Decode provider JSON as UTF-8 even when a compatible API mislabels charset."""

    return json.loads(response.content.decode("utf-8-sig"))


def _response_text_utf8(response: requests.Response) -> str:
    return response.content.decode("utf-8-sig", errors="replace")


@dataclass
class Cue:
    cue_id: int
    group_id: str
    source_raw: str
    source: str
    alignment_start: int
    alignment_end: int
    start: float
    end: float
    target: str = ""


def classify_source_language(text: str) -> str:
    language = base_language(classify_language(text))
    return language if language in {"zh-CN", "en", "ja", "ko"} else "en"


def target_language_for(source_language: str) -> str:
    return "en" if source_language == "zh-CN" else "zh-CN"


def subtitle_visual_width(text: str) -> int:
    """Approximate one Han glyph as two Latin columns."""

    width = 0
    for char in text:
        if char.isspace():
            continue
        width += 2 if re.match(r"[\u3400-\u9fff]", char) else 1
    return width


def _proper_name_only(text: str) -> bool:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9'’.-]*", text)
    if len(tokens) != 1 or re.search(r"[\u3400-\u9fff]", text):
        return False
    token = tokens[0]
    return token.isupper() or token[:1].isupper()


def _numeric_expression_only(text: str) -> bool:
    number_words = {
        "zero", "one", "two", "three", "four", "five", "six", "seven",
        "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
        "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
        "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
        "hundred", "thousand", "million", "billion", "percent", "percentage",
    }
    words = [word.lower() for word in re.findall(r"[A-Za-z]+", text)]
    if words and all(word in number_words for word in words):
        return True
    compact = re.sub(r"[\s,，、。.:：%％+\-/]", "", text)
    return bool(compact) and bool(
        re.fullmatch(
            r"(?:\d+(?:[A-Za-z]{0,3})?|[零〇一二两三四五六七八九十百千万亿兆点]+)",
            compact,
        )
    )


def _endpoint(base_url: str) -> str:
    return endpoint_url(base_url, "/chat/completions")


def _cancellable_editor_post(
    *, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int
) -> dict[str, Any]:
    """Run one editor-owned provider request in a killable child process."""
    raise_if_task_cancelled()
    process = subprocess.Popen(
        python_script_command("scripts/run_editor_model_request.py"),
        cwd=str(Path(__file__).resolve().parents[1]),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    request_text = json.dumps({
        "url": url,
        "headers": headers,
        "payload": payload,
        "timeout": timeout,
    }, ensure_ascii=True)
    # Finish the entire parent-to-child input transfer before polling. Reusing
    # communicate(input=...) after short Windows timeouts can corrupt larger
    # JSON payloads. ASCII escaping also keeps pipe transport byte-stable; the
    # child reconstructs Unicode before sending real UTF-8 to the provider.
    try:
        assert process.stdin is not None
        process.stdin.write(request_text)
        process.stdin.close()
        process.stdin = None
    except (BrokenPipeError, OSError) as exc:
        process.kill()
        process.communicate()
        raise Stage2Error("翻译 API 请求进程无法接收请求数据") from exc
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            if task_cancellation_requested():
                process.terminate()
                try:
                    process.communicate(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise EditorAiTaskCancelled("editor AI task was cancelled")
            continue
        except EditorAiTaskCancelled:
            process.terminate()
            try:
                process.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            raise
    if process.returncode:
        raise Stage2Error(
            f"翻译 API 请求进程失败 exit={process.returncode}: {stderr[-1000:]}"
        )
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise Stage2Error("翻译 API 请求进程没有返回有效 JSON") from exc
    if not result.get("ok"):
        raw_status = result.get("status")
        try:
            status = int(raw_status) if raw_status is not None else None
        except (TypeError, ValueError):
            status = None
        raise Stage2RequestError(
            f"翻译 API 请求失败：HTTP {raw_status or '-'} "
            f"{str(result.get('error') or '')[:1000]}",
            status=status,
        )
    body = result.get("body")
    if not isinstance(body, dict):
        raise Stage2Error("翻译 API 返回的 HTTP 内容不是 JSON 对象")
    return body


def _response_text(body: dict[str, Any]) -> str:
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise Stage2Error("翻译响应中找不到 choices[0].message.content") from exc
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
    raise Stage2Error("翻译响应正文为空")


def _extract_json(text: str) -> dict[str, Any]:
    value = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.DOTALL)
    if fenced:
        value = fenced.group(1)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise Stage2Error(f"翻译模型没有返回有效 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise Stage2Error("翻译 JSON 顶层必须是对象")
    return parsed


def call_translation_model(
    *,
    base_url: str,
    api_key: str,
    auth_mode: str = "bearer",
    model: str,
    system_prompt: str,
    groups: list[dict[str, Any]],
    timeout: int,
    thinking_mode: str,
    reasoning_effort: str,
    request_attempts: int = 2,
    max_tokens: int = 32768,
    temperature: float = 0.0,
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
            {
                "role": "user",
                "content": "# LOCKED_GROUPS\n"
                + json.dumps({"groups": groups}, ensure_ascii=False),
            },
        ],
        "max_tokens": int(max_tokens),
        "stream": False,
    }
    if effective_thinking_mode == "enabled":
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = effective_effort
    else:
        payload["thinking"] = {"type": "disabled"}
        payload["temperature"] = float(temperature)
    started = time.perf_counter()
    requested = max(1, int(max_tokens))
    attempted_budgets: list[int] = [requested]
    body: dict[str, Any] | None = None
    finish: str | None = None
    payload["max_tokens"] = requested
    idempotency_key = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    total_attempts = max(1, min(3, int(request_attempts)))
    last_error: Exception | None = None
    transport_attempt_count = 0
    for attempt in range(1, total_attempts + 1):
        transport_attempt_count = attempt
        try:
            headers = {
                **auth_headers(api_key, auth_mode),
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
            }
            if current_task_id():
                body = _cancellable_editor_post(
                    url=_endpoint(base_url), headers=headers, payload=payload, timeout=timeout,
                )
            else:
                response = requests.post(
                    _endpoint(base_url), headers=headers, json=payload, timeout=timeout,
                )
                response.raise_for_status()
                body = _response_json_utf8(response)
            break
        except Stage2RequestError as exc:
            last_error = exc
            if exc.retryable and attempt < total_attempts:
                time.sleep(min(3.0, attempt * 0.75))
                continue
            raise
        except requests.RequestException as exc:
            last_error = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status is None or status in {408, 425, 429} or (
                status is not None and status >= 500
            )
            if retryable and attempt < total_attempts:
                time.sleep(min(3.0, attempt * 0.75))
                continue
            detail = (
                _response_text_utf8(exc.response)[:1000]
                if getattr(exc, "response", None) else ""
            )
            raise Stage2Error(f"翻译 API 请求失败：{exc} {detail}") from exc
        except ValueError as exc:
            raise Stage2Error("翻译 API 返回的 HTTP 内容不是 JSON") from exc
    if body is None:
        raise Stage2Error(f"翻译 API 请求未完成：{last_error}")
    finish = body.get("choices", [{}])[0].get("finish_reason")
    if finish == "length":
        raise Stage2Error(f"翻译输出达到当前 {requested} token 上限；该单元将进入一次修复")
    parsed = _extract_json(_response_text(body))
    usage = body.get("usage", {})
    usage = usage if isinstance(usage, dict) else {}
    prompt_details = usage.get("prompt_tokens_details", {})
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    return parsed, {
        "model": model,
        "thinking_mode": effective_thinking_mode,
        "requested_thinking_mode": requested_thinking_mode,
        "effective_thinking_mode": effective_thinking_mode,
        "reasoning_effort": effective_effort if effective_thinking_mode == "enabled" else None,
        "requested_reasoning_effort": requested_effort if effective_thinking_mode == "enabled" else None,
        "effective_reasoning_effort": effective_effort if effective_thinking_mode == "enabled" else None,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "finish_reason": finish,
        "max_tokens": attempted_budgets[-1],
        "attempted_output_budgets": attempted_budgets,
        "transport_attempt_count": transport_attempt_count,
        "usage": usage,
        "cache_usage": {
            "hit_tokens": int(usage.get("prompt_cache_hit_tokens", 0) or 0),
            "miss_tokens": int(usage.get("prompt_cache_miss_tokens", 0) or 0),
            "cached_tokens": int(prompt_details.get("cached_tokens", 0) or 0),
        },
    }


def build_cues(
    draft: str,
    plan: dict[str, Any],
    alignment: dict[str, Any],
    *,
    source_baseline_punctuation: str = "preserve",
    source_raised_punctuation: str = "preserve",
    bottom_baseline_punctuation: str = "normalize",
    bottom_raised_punctuation: str = "preserve",
    display_order: str = "source_target",
    tail_padding_ms: int = 120,
    snap_threshold_ms: int = 500,
    timing_audit: list[dict[str, Any]] | None = None,
) -> tuple[list[Cue], list[dict[str, Any]]]:
    blocks = split_groups(draft)
    groups = plan.get("groups", [])
    if len(blocks) != len(groups):
        raise Stage2Error(
            f"03A 草案组数 {len(blocks)} 与索引计划组数 {len(groups)} 不一致"
        )
    unit_by_index = {int(item["index"]): item for item in alignment["units"]}
    fps = float(alignment.get("media", {}).get("fps", 0) or 30)
    media_end = float(alignment.get("media", {}).get("duration_seconds", 0) or 0)
    cues: list[Cue] = []
    model_groups: list[dict[str, Any]] = []
    cue_id = 0
    for block, group in zip(blocks, groups):
        start = int(group["alignment_start"])
        end = int(group["alignment_end"])
        ends = [int(value) for value in group.get("line_breaks_after", [])] + [end]
        if len(block) != len(ends):
            raise Stage2Error(f"{group['group_id']} 草案行数与索引切点数不一致")
        cursor = start
        model_cues: list[dict[str, Any]] = []
        for line, cue_end in zip(block, ends):
            if cursor not in unit_by_index or cue_end not in unit_by_index:
                raise Stage2Error(f"{group['group_id']} 使用了不存在的 alignment index")
            _, projected, annotation_issues = project_annotations(line)
            if annotation_issues:
                raise Stage2Error(
                    f"{group['group_id']} 删除/纠错标记无效：{annotation_issues}"
                )
            projected_language = classify_source_language(projected)
            source_is_bottom = (
                (display_order == "en_zh" and projected_language == "zh-CN")
                or (display_order == "zh_en" and projected_language == "en")
            )
            source = display_normalize(
                projected,
                baseline_punctuation=(
                    bottom_baseline_punctuation
                    if source_is_bottom
                    else source_baseline_punctuation
                ),
                raised_punctuation=(
                    bottom_raised_punctuation
                    if source_is_bottom
                    else source_raised_punctuation
                ),
            )
            # A deletion such as `买一一点//。` intentionally preserves the
            # spoken units for later timing projection but leaves no subtitle
            # content. Do not turn the orphan full stop into its own cue.
            if not lexical_tokens(source):
                cursor = cue_end + 1
                continue
            cue_id += 1
            start_sec = float(unit_by_index[cursor]["start"])
            end_sec = float(unit_by_index[cue_end]["end"])
            cue = Cue(
                cue_id=cue_id,
                group_id=str(group["group_id"]),
                source_raw=line,
                source=source,
                alignment_start=cursor,
                alignment_end=cue_end,
                start=start_sec,
                end=end_sec,
            )
            cues.append(cue)
            source_language = classify_source_language(source)
            model_cues.append(
                {
                    "cue_id": cue_id,
                    "source": source,
                    "source_language": source_language,
                    "target_language": target_language_for(source_language),
                }
            )
            cursor = cue_end + 1
        if model_cues:
            model_groups.append(
                {"group_id": str(group["group_id"]), "cues": model_cues}
            )

    alignment_engine = str(alignment.get("engines", {}).get("alignment", "")).lower()
    unit_timing_sources = {
        str(item.get("timing_source", "")).lower()
        for item in alignment.get("units", [])
        if isinstance(item, dict)
    }
    shared_envelope_source = (
        "forcedalign" in alignment_engine
        or "qwen" in alignment_engine
        or any("forced" in value or "qwen" in value for value in unit_timing_sources)
    )

    # Forced aligners can assign several adjacent units the same outer time
    # envelope. If a language boundary falls inside such an envelope, divide
    # that envelope deterministically by source-character weight so every cue
    # owns at least one video frame and the final track stays monotonic.
    run_start = 0
    while shared_envelope_source and run_start < len(cues) - 1:
        run_end = run_start
        envelope_end = cues[run_start].end
        while (
            run_end + 1 < len(cues)
            and cues[run_end + 1].start < envelope_end - 1e-6
        ):
            run_end += 1
            envelope_end = max(envelope_end, cues[run_end].end)
        if run_end > run_start:
            first_frame = math.floor(cues[run_start].start * fps + 1e-9)
            last_frame = math.ceil(envelope_end * fps - 1e-9)
            count = run_end - run_start + 1
            last_frame = max(last_frame, first_frame + count)
            weights = [
                max(1, len(re.sub(r"\s+", "", cues[position].source)))
                for position in range(run_start, run_end + 1)
            ]
            remaining_frames = last_frame - first_frame
            remaining_weight = sum(weights)
            cursor_frame = first_frame
            for local, position in enumerate(range(run_start, run_end + 1)):
                cues_left = count - local
                if cues_left == 1:
                    allocated = remaining_frames
                else:
                    ideal = round(remaining_frames * weights[local] / remaining_weight)
                    allocated = max(1, min(ideal, remaining_frames - (cues_left - 1)))
                cues[position].start = cursor_frame / fps
                cursor_frame += allocated
                cues[position].end = cursor_frame / fps
                remaining_frames -= allocated
                remaining_weight -= weights[local]
        run_start = run_end + 1

    # Engine labels are not a reliable proxy for timestamp quality. Whisper
    # word timestamps can also collapse repeated words onto the same instant.
    # Repair only an observed zero-duration run, borrowing its immediate left
    # anchor and the first right cue with a later end. Text and A3 boundaries
    # remain unchanged; only frames inside that local envelope are reassigned.
    zero_position = 0
    while zero_position < len(cues):
        if cues[zero_position].end > cues[zero_position].start + 1e-6:
            zero_position += 1
            continue
        anchor = cues[zero_position].start
        window_start = max(0, zero_position - 1)
        window_end = zero_position
        while (
            window_end + 1 < len(cues)
            and cues[window_end].end <= anchor + 1e-6
        ):
            window_end += 1
        while (
            window_end + 1 < len(cues)
            and cues[window_end].end <= cues[window_start].start + 1e-6
        ):
            window_end += 1
        envelope_start = cues[window_start].start
        envelope_end = cues[window_end].end
        count = window_end - window_start + 1
        first_frame = math.floor(envelope_start * fps + 1e-9)
        last_frame = math.ceil(envelope_end * fps - 1e-9)
        if last_frame - first_frame < count and window_end + 1 < len(cues):
            window_end += 1
            envelope_end = cues[window_end].end
            count = window_end - window_start + 1
            last_frame = math.ceil(envelope_end * fps - 1e-9)
        last_frame = max(last_frame, first_frame + count)
        weights = [
            max(1, len(re.sub(r"\s+", "", cues[position].source)))
            for position in range(window_start, window_end + 1)
        ]
        remaining_frames = last_frame - first_frame
        remaining_weight = sum(weights)
        cursor_frame = first_frame
        for local, position in enumerate(range(window_start, window_end + 1)):
            cues_left = count - local
            if cues_left == 1:
                allocated = remaining_frames
            else:
                ideal = round(
                    remaining_frames * weights[local] / remaining_weight
                )
                allocated = max(
                    1,
                    min(ideal, remaining_frames - (cues_left - 1)),
                )
            cues[position].start = cursor_frame / fps
            cursor_frame += allocated
            cues[position].end = cursor_frame / fps
            remaining_frames -= allocated
            remaining_weight -= weights[local]
        zero_position = window_end + 1

    group_by_id = {
        str(group.get("group_id", "")): group
        for group in groups
        if isinstance(group, dict)
    }

    def speaker_state(left_unit: dict[str, Any], right_unit: dict[str, Any]) -> str:
        left = str(left_unit.get("speaker_id", "")).strip()
        right = str(right_unit.get("speaker_id", "")).strip()
        left_confidence = float(left_unit.get("speaker_confidence", 0) or 0)
        right_confidence = float(right_unit.get("speaker_confidence", 0) or 0)
        if (
            left
            and right
            and left not in {"speaker_unknown", "unknown"}
            and right not in {"speaker_unknown", "unknown"}
            and min(left_confidence, right_confidence) >= 0.75
        ):
            return "same" if left == right else "change"
        return "unknown"

    # Display continuity is independent from semantic grouping.  Internal cuts
    # inside one authoritative source cue remain continuous.  Across source cues,
    # only continuous/related discourse within the configured small-gap window is
    # snapped; real pauses and high-confidence speaker changes remain visible.
    tail_padding = max(0.0, min(1.0, tail_padding_ms / 1000.0))
    snap_threshold = max(0.0, min(2.0, snap_threshold_ms / 1000.0))
    for index, cue in enumerate(cues):
        next_start = cues[index + 1].start if index + 1 < len(cues) else media_end
        action = "tail_padding"
        relation = "separate"
        relation_confidence = 1.0
        relation_reason = "terminal cue"
        same_source_cue = False
        speaker_transition = "unknown"
        original_gap = max(0.0, next_start - cue.end)
        if index + 1 < len(cues):
            right_cue = cues[index + 1]
            left_unit = unit_by_index.get(cue.alignment_end, {})
            right_unit = unit_by_index.get(right_cue.alignment_start, {})
            left_source_cue = left_unit.get("source_cue_id")
            right_source_cue = right_unit.get("source_cue_id")
            same_source_cue = (
                left_source_cue is not None
                and right_source_cue is not None
                and left_source_cue == right_source_cue
            )
            speaker_transition = speaker_state(left_unit, right_unit)
            if cue.group_id == right_cue.group_id:
                relation = "continuous"
                relation_confidence = 1.0
                relation_reason = "internal cue boundary in one meaning group"
            else:
                continuity = group_by_id.get(cue.group_id, {}).get(
                    "continuity_after", {}
                )
                relation = str(continuity.get("relation", "separate"))
                relation_confidence = float(
                    continuity.get("confidence", 0.5) or 0.5
                )
                relation_reason = str(
                    continuity.get(
                        "reason", "no cross-group continuity evidence"
                    )
                )
                declared_speaker = str(
                    continuity.get("speaker_transition", "unknown")
                )
                if declared_speaker == "change":
                    speaker_transition = "change"
                elif speaker_transition == "unknown" and declared_speaker == "same":
                    speaker_transition = "same"
            eligible_relation = (
                relation == "continuous"
                or (relation == "related" and relation_confidence >= 0.7)
            )
            if same_source_cue and speaker_transition != "change":
                cue.end = max(cue.end, next_start)
                action = "snap_same_source_cue"
            elif (
                original_gap <= snap_threshold + 1e-9
                and eligible_relation
                and speaker_transition != "change"
            ):
                cue.end = max(cue.end, next_start)
                action = "snap_related"
            elif next_start >= cue.end:
                ceiling = next_start if next_start > 0 else cue.end + tail_padding
                cue.end = min(cue.end + tail_padding, ceiling)
                action = "preserve_gap_with_tail_padding"
        elif (
            str(alignment.get("timing_policy", {}).get("last_output_cue_end", ""))
            == "source_cue_end"
        ):
            last_unit = unit_by_index.get(cue.alignment_end, {})
            cue.end = max(
                cue.end,
                float(last_unit.get("source_cue_end", cue.end)),
            )
            action = "extend_to_authoritative_source_end"
        elif next_start >= cue.end:
            ceiling = next_start if next_start > 0 else cue.end + tail_padding
            cue.end = min(cue.end + tail_padding, ceiling)
        if timing_audit is not None and index + 1 < len(cues):
            timing_audit.append(
                {
                    "left_cue": cue.cue_id,
                    "right_cue": cues[index + 1].cue_id,
                    "left_group": cue.group_id,
                    "right_group": cues[index + 1].group_id,
                    "original_gap_ms": round(original_gap * 1000),
                    "action": action,
                    "same_source_cue": same_source_cue,
                    "continuity": relation,
                    "continuity_confidence": round(relation_confidence, 3),
                    "continuity_reason": relation_reason,
                    "speaker_transition": speaker_transition,
                    "snap_threshold_ms": round(snap_threshold * 1000),
                }
            )
        cue.start = math.floor(cue.start * fps + 1e-9) / fps
        cue.end = math.ceil(cue.end * fps - 1e-9) / fps
        if cue.end - cue.start < 0.5 / fps:
            cue.end = cue.start + 1 / fps
    for index in range(1, len(cues)):
        if cues[index].start < cues[index - 1].end:
            frame = 1 / fps
            boundary = (cues[index - 1].end + cues[index].start) / 2
            boundary = max(cues[index - 1].start + frame, boundary)
            boundary = min(cues[index].end - frame, boundary)
            cues[index - 1].end = boundary
            cues[index].start = boundary
        if cues[index].end - cues[index].start < 0.5 / fps:
            cues[index].end = cues[index].start + 1 / fps
    return cues, model_groups


def chunk_groups(
    groups: list[dict[str, Any]],
    *,
    max_groups: int = 45,
    max_characters: int = 9000,
) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    characters = 0
    for group in groups:
        size = sum(len(str(cue["source"])) for cue in group["cues"])
        if current and (len(current) >= max_groups or characters + size > max_characters):
            chunks.append(current)
            current = []
            characters = 0
        current.append(group)
        characters += size
    if current:
        chunks.append(current)
    return chunks


def validate_translation(
    result: dict[str, Any],
    expected_groups: list[dict[str, Any]],
    *,
    target_baseline_punctuation: str = "normalize",
    target_raised_punctuation: str = "preserve",
    top_baseline_punctuation: str = "preserve",
    top_raised_punctuation: str = "preserve",
    display_order: str = "source_target",
) -> dict[int, str]:
    if result.get("schema_version") != "substar.stage2.translation.v1":
        raise Stage2Error("翻译 schema_version 错误")
    actual_groups = result.get("groups")
    if not isinstance(actual_groups, list):
        raise Stage2Error("翻译结果缺少 groups")
    expected_ids = [group["group_id"] for group in expected_groups]
    actual_ids = [group.get("group_id") for group in actual_groups]
    if actual_ids != expected_ids:
        raise Stage2Error("翻译 group_id 未完整按序覆盖输入")
    targets: dict[int, str] = {}
    for expected, actual in zip(expected_groups, actual_groups):
        expected_cues = [int(item["cue_id"]) for item in expected["cues"]]
        actual_targets = actual.get("targets")
        if not isinstance(actual_targets, list):
            raise Stage2Error(f"{expected['group_id']} 缺少 targets")
        actual_cues = [int(item.get("cue_id", -1)) for item in actual_targets]
        if actual_cues != expected_cues:
            raise Stage2Error(f"{expected['group_id']} cue_id 未完整按序覆盖")
        relation = str(actual.get("relation", "N:N"))
        mapped_sources: set[int] = set()
        for target in actual_targets:
            raw_source_ids = target.get("source_cue_ids")
            if raw_source_ids is None:
                if relation in {"N:1", "N:M"}:
                    raise Stage2Error(
                        f"{expected['group_id']} {relation} target 缺少 source_cue_ids"
                    )
                raw_source_ids = [int(target.get("cue_id", -1))]
            if not isinstance(raw_source_ids, list) or not raw_source_ids:
                raise Stage2Error(
                    f"{expected['group_id']} source_cue_ids 必须是非空数组"
                )
            try:
                source_ids = [int(value) for value in raw_source_ids]
            except (TypeError, ValueError) as exc:
                raise Stage2Error(
                    f"{expected['group_id']} source_cue_ids 含非法 ID"
                ) from exc
            if len(source_ids) != len(set(source_ids)) or any(
                cue_id not in expected_cues for cue_id in source_ids
            ):
                raise Stage2Error(
                    f"{expected['group_id']} source_cue_ids 重复或跨组"
                )
            if source_ids != sorted(source_ids, key=expected_cues.index):
                raise Stage2Error(
                    f"{expected['group_id']} source_cue_ids 必须保持源 cue 顺序"
                )
            if relation in {"1:1", "N:N"} and source_ids != [int(target["cue_id"])]:
                raise Stage2Error(
                    f"{expected['group_id']} {relation} 必须逐 cue 映射"
                )
            if relation == "N:1" and source_ids != expected_cues:
                raise Stage2Error(
                    f"{expected['group_id']} N:1 每个共享目标必须映射整组 source_cue_ids"
                )
            mapped_sources.update(source_ids)
        if mapped_sources != set(expected_cues):
            raise Stage2Error(
                f"{expected['group_id']} source_cue_ids 未完整覆盖本组源 cue"
            )
        for item in actual_targets:
            cue_id = int(item["cue_id"])
            source_item = next(
                value for value in expected["cues"] if int(value["cue_id"]) == cue_id
            )
            target_language = str(
                source_item.get(
                    "target_language",
                    target_language_for(
                        classify_source_language(str(source_item["source"]))
                    ),
                )
            )
            target_is_top = (
                (display_order == "en_zh" and target_language == "en")
                or (display_order == "zh_en" and target_language == "zh-CN")
            )
            text = display_normalize(
                str(item.get("text", "")).strip(),
                baseline_punctuation=(
                    top_baseline_punctuation
                    if target_is_top
                    else target_baseline_punctuation
                ),
                raised_punctuation=(
                    top_raised_punctuation
                    if target_is_top
                    else target_raised_punctuation
                ),
            )
            status = str(item.get("status", "ok"))
            if status == "failed":
                # A bounded API failure is a valid pipeline state, not a valid
                # translation. Preserve the cue and surface it in the report
                # instead of throwing away successful neighbouring groups.
                if not text:
                    text = (
                        "Translation failed review required"
                        if target_language == "en"
                        else "【翻译失败 待人工复核】"
                    )
                targets[cue_id] = text
                continue
            if not text:
                raise Stage2Error(f"cue {cue_id} 译文为空")
            if text in {
                "Translation failed review required",
                "【翻译失败 待人工复核】",
            }:
                raise Stage2Error(f"cue {cue_id} 仍为翻译失败占位")
            source = str(source_item["source"])
            target_han = len(re.findall(r"[\u3400-\u9fff]", text))
            target_latin = len(re.findall(r"[A-Za-z]", text))
            target_kana = len(re.findall(r"[\u3040-\u30ff]", text))
            target_hangul = len(re.findall(r"[\uac00-\ud7af]", text))
            if target_language == "ja" and target_kana == 0:
                raise Stage2Error(
                    f"cue {cue_id} target language must be Japanese: {text}"
                )
            if target_language == "ko" and target_hangul == 0:
                raise Stage2Error(
                    f"cue {cue_id} target language must be Korean: {text}"
                )
            if (
                target_language == "zh-CN"
                and target_han == 0
                and not _proper_name_only(source)
                and not (
                    _numeric_expression_only(source)
                    and bool(re.fullmatch(r"[\d\s%％:：/+\-.]+", text))
                )
            ):
                raise Stage2Error(
                    f"cue {cue_id} 目标语言必须为简体中文，模型疑似返回了英文改写：{text}"
                )
            if (
                target_language == "en"
                and target_latin == 0
                and not (
                    _numeric_expression_only(source)
                    and bool(re.fullmatch(r"[\d\s%％:：/+\-.]+", text))
                )
            ):
                raise Stage2Error(
                    f"cue {cue_id} 目标语言必须为英文，模型疑似返回了中文改写：{text}"
                )
            targets[cue_id] = text
    return targets


def _srt_time(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def render_srt(cues: list[Cue], *, display_order: str = "source_target") -> str:
    def lines(cue: Cue) -> tuple[str, str]:
        return track_lines(
            source=cue.source,
            target=cue.target,
            display_order=display_order,
            source_language=classify_source_language(cue.source),
        )

    blocks = [
        f"{cue.cue_id}\n{_srt_time(cue.start)} --> {_srt_time(cue.end)}\n"
        f"{lines(cue)[0]}\n{lines(cue)[1]}"
        for cue in cues
    ]
    return "\n\n".join(blocks) + "\n"


def validate_final(
    cues: list[Cue],
    *,
    source_baseline_punctuation: str = "preserve",
    target_baseline_punctuation: str = "normalize",
    target_language_mode: str = "auto_opposite",
    display_order: str = "source_target",
    english_hard_limit: int = 55,
    english_count_spaces: bool = True,
    english_count_punctuation: bool = True,
    chinese_hard_limit: int = 24,
    mixed_hard_limit: int = 25,
    japanese_hard_limit: int = 25,
    korean_hard_limit: int = 32,
    visual_width_limit: int = 48,
    minimum_cue_duration_ms: int = 400,
    maximum_cue_duration_ms: int = 7000,
    maximum_cps_latin: float = 20.0,
    maximum_cps_cjk: float = 12.0,
) -> dict[str, Any]:
    policy = SubtitlePolicy(
        display_order=display_order,
        english_hard_limit=english_hard_limit,
        english_count_spaces=english_count_spaces,
        english_count_punctuation=english_count_punctuation,
        chinese_hard_limit=chinese_hard_limit,
        mixed_hard_limit=mixed_hard_limit,
        japanese_hard_limit=japanese_hard_limit,
        korean_hard_limit=korean_hard_limit,
        target_visual_width_limit=visual_width_limit,
        minimum_cue_duration_ms=minimum_cue_duration_ms,
        maximum_cue_duration_ms=maximum_cue_duration_ms,
        maximum_cps_latin=maximum_cps_latin,
        maximum_cps_cjk=maximum_cps_cjk,
    )
    review: list[dict[str, Any]] = []
    for index, cue in enumerate(cues):
        if index and cue.start < cues[index - 1].end - 0.0005:
            review.append(
                {"cue_ids": [cue.cue_id - 1, cue.cue_id], "type": "timing", "reason": "时间重叠"}
            )
        duration = max(0.001, cue.end - cue.start)
        if duration * 1000 < minimum_cue_duration_ms:
            review.append(
                {
                    "cue_ids": [cue.cue_id],
                    "type": "timing",
                    "reason": f"显示时长低于{minimum_cue_duration_ms}ms 可能存在共享或退化词级时间",
                }
            )
        if duration * 1000 > maximum_cue_duration_ms:
            review.append(
                {
                    "cue_ids": [cue.cue_id],
                    "type": "timing",
                    "reason": f"显示时长超过{maximum_cue_duration_ms}ms",
                }
            )
        source_han = len(re.findall(r"[\u3400-\u9fff]", cue.source))
        target_han = len(re.findall(r"[\u3400-\u9fff]", cue.target))
        source_language_class = classify_language(cue.source)
        target_language_class = classify_language(cue.target)
        source_visible = policy.line_length(cue.source, source_language_class)
        target_visible = policy.line_length(cue.target, target_language_class)
        source_language = classify_source_language(cue.source)
        target_language = (
            target_language_mode
            if target_language_mode in {"zh-CN", "en", "ja", "ko"}
            else target_language_for(source_language)
        )
        target_latin = len(re.findall(r"[A-Za-z]", cue.target))
        target_kana = len(re.findall(r"[\u3040-\u30ff]", cue.target))
        target_hangul = len(re.findall(r"[\uac00-\ud7af]", cue.target))
        source_is_top = (
            display_order == "source_target"
            or (display_order == "en_zh" and source_language != "zh-CN")
            or (display_order == "zh_en" and source_language == "zh-CN")
        )
        source_punctuation_policy = (
            source_baseline_punctuation
            if source_is_top
            else target_baseline_punctuation
        )
        target_punctuation_policy = (
            target_baseline_punctuation
            if source_is_top
            else source_baseline_punctuation
        )
        source_over_limit = source_visible > policy.hard_limit(
            cue.source, source_language_class
        )
        if source_over_limit:
            review.append({"cue_ids": [cue.cue_id], "type": "line_length", "reason": "源文超过硬上限"})
        target_over_limit = target_visible > policy.hard_limit(
            cue.target, target_language_class
        )
        if target_over_limit:
            review.append({"cue_ids": [cue.cue_id], "type": "line_length", "reason": "译文超过硬上限"})
        if (
            source_punctuation_policy != "preserve"
            and illegal_lower_punctuation(cue.source)
        ):
            review.append(
                {
                    "cue_ids": [cue.cue_id],
                    "type": "punctuation",
                    "reason": "上行残留当前配置禁止的下标点",
                }
            )
        if (
            target_punctuation_policy != "preserve"
            and illegal_lower_punctuation(cue.target)
        ):
            review.append(
                {
                    "cue_ids": [cue.cue_id],
                    "type": "punctuation",
                    "reason": "下行残留当前配置禁止的下标点",
                }
            )
        if subtitle_visual_width(cue.target) > visual_width_limit:
            review.append(
                {
                    "cue_ids": [cue.cue_id],
                    "type": "visual_width",
                    "reason": f"译文视觉宽度超过{visual_width_limit}列",
                }
            )
        if (
            target_language == "zh-CN"
            and target_han == 0
            and not _proper_name_only(cue.source)
            and not (
                _numeric_expression_only(cue.source)
                and bool(re.fullmatch(r"[\d\s%％:：/+\-.]+", cue.target))
            )
        ):
            review.append(
                {
                    "cue_ids": [cue.cue_id],
                    "type": "language_direction",
                    "reason": "英文源文未生成简体中文译文",
                }
            )
        if (
            target_language == "en"
            and target_latin == 0
            and not (
                _numeric_expression_only(cue.source)
                and bool(re.fullmatch(r"[\d\s%％:：/+\-.]+", cue.target))
            )
        ):
            review.append(
                {
                    "cue_ids": [cue.cue_id],
                    "type": "language_direction",
                    "reason": "中文源文未生成英文译文",
                }
            )
        if target_language == "ja" and target_kana == 0:
            review.append(
                {
                    "cue_ids": [cue.cue_id],
                    "type": "language_direction",
                    "reason": "源文未生成日文译文",
                }
            )
        if target_language == "ko" and target_hangul == 0:
            review.append(
                {
                    "cue_ids": [cue.cue_id],
                    "type": "language_direction",
                    "reason": "源文未生成韩文译文",
                }
            )
    return {
        "schema_version": "substar.translation-report.v1",
        "summary": {
            "cue_count": len(cues),
            "source_coverage": 1.0,
            "review_required_count": len(review),
        },
        "review_items": review,
    }
