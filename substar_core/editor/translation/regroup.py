"""Optional boundary review for a single translation execution."""
from __future__ import annotations

import copy
import concurrent.futures
import json
import re
from typing import Any, Callable
from pathlib import Path

from substar_core.ai_block_cache import fingerprint, load_ai_block_cache, save_ai_block_cache

from substar_core.model_gateway import call_text_model
from substar_core.model_routing import resolve_stage_request
from substar_core.translation_layout import route_text, cue_language


BOUNDARY_PROMPT = """你是字幕请求的分块编辑。只优化请求块接缝，不修改字幕或词元。
上下各最多五条字幕用于判断。优先在完整表达、话题或说话人转折处结束请求，
避免切断主谓、动宾、条件与结果及需要后文补全的短语。不为整齐强行移动边界。
在给定 MIN_END 到 MAX_END 之间选择左请求最后一条字幕的编号。
若原边界自然则保持 CURRENT_END。只返回一个十进制整数，不翻译、不解释。"""


def translation_prompt_variant(
    settings: dict[str, Any], source: str, target: str,
    groups: list[dict[str, Any]] | None = None,
) -> str:
    from substar_core.prompt_registry import translation_variant
    layout = settings.get("translation_layout")
    if not layout:
        return translation_variant(source, target)
    # A one-direction English task still uses the accepted language prompt
    # when launched through the per-Cue routing UI.
    if source == "en" and layout.get("lower_target") == "zh-CN" and groups and all(
        cue_language(cue["source_text"]) == "en"
        and cue.get("target_language") == "zh-CN"
        for group in groups for cue in group["cues"]
    ):
        return "en_to_zh"
    return "generic"


def review_request_groups(
    groups: list[dict[str, Any]], settings: dict[str, Any], *,
    progress: Callable[[int, int], None] | None = None,
    cache_directory: Path | None = None,
) -> list[dict[str, Any]]:
    """Move existing boundaries only; preserve every Cue and route exactly.

    Results live in this execution's memory. There is no project plan, saved
    preference or plan-validity lifecycle. Translation transport retries reuse
    these groups and their normal content-addressed block cache.
    """
    if not settings.get("translation_regroup") or len(groups) < 2:
        return groups
    if settings.get("translation_mapping_mode") == "one_to_one":
        components: list[dict[str, Any]] = []
        for group in groups:
            block = group.get("execution_block_id") or group["group_id"]
            if not components or components[-1]["group_id"] != block:
                components.append({"group_id": block, "execution_block_id": block, "cues": []})
            components[-1]["cues"].extend(group["cues"])
        reviewed = review_request_groups(components, {**settings, "translation_mapping_mode": "many_to_many"}, progress=progress, cache_directory=cache_directory)
        return [
            {"group_id": f"line:{cue['cue_id']}", "execution_block_id": group["execution_block_id"], "cues": [cue]}
            for group in reviewed for cue in group["cues"]
        ]
    cues = [cue for group in groups for cue in group["cues"]]
    ends, cursor = [], 0
    for group in groups:
        cursor += len(group["cues"])
        ends.append(cursor)
    try:
        route = resolve_stage_request(settings, settings.get("_boundary_stage", "translation"))
    except Exception:
        return groups  # Optimization is optional; it must never block the task.
    layout = settings.get("translation_layout")

    def cue_route(cue):
        if layout:
            return {**route_text(cue["source_text"], layout), "speaker": cue.get("speaker")}
        return {"target_language": cue.get("target_language"), "speaker": cue.get("speaker")}

    requests = []
    for index, end in enumerate(ends[:-1]):
        previous = ends[index - 1] if index else 0
        following = ends[index + 1]
        # Disjoint movement windows ensure parallel decisions cannot cross.
        minimum = max(previous + 1, (previous + end) // 2 + 1, end - 4)
        maximum = min(following - 1, (end + following) // 2, end + 4)
        if cue_route(cues[end - 1]) != cue_route(cues[end]):
            continue
        # Segment boundaries include deleted Cue gaps and speaker changes.
        if cues[end - 1].get("segment") != cues[end].get("segment"):
            continue
        expected_route = cue_route(cues[end - 1])
        while minimum < end and cue_route(cues[minimum - 1]) != expected_route:
            minimum += 1
        while maximum > end and cue_route(cues[maximum]) != expected_route:
            maximum -= 1
        while minimum < end and cues[minimum - 1].get("segment") != cues[end - 1].get("segment"):
            minimum += 1
        while maximum > end and cues[maximum].get("segment") != cues[end].get("segment"):
            maximum -= 1
        if minimum == maximum:
            continue
        requests.append((index, end, minimum, maximum))
    if progress:
        progress(0, len(requests))

    def review(item):
        cancel_event = settings.get("_cancel_event")
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("请求边界优化已取消")
        index, end, minimum, maximum = item
        rows = [
            {"number": i + 1, "text": cue["source_text"],
             "start": cue.get("start"), "end": cue.get("end"), "speaker": cue.get("speaker")}
            for i, cue in enumerate(cues) if end - 5 <= i < end + 5
        ]
        user_text = json.dumps({"CURRENT_END": end, "MIN_END": minimum,
                               "MAX_END": maximum, "cues": rows}, ensure_ascii=False)
        prompt = settings.get("boundary_prompt", BOUNDARY_PROMPT)
        key = fingerprint({"prompt": prompt, "input": user_text,
                           "route": {k:v for k,v in route.items() if k != "api_key"}, "contract": 1})
        cached = load_ai_block_cache(cache_directory, key) if cache_directory else None
        if cached and isinstance(cached.get("end"), int) and minimum <= cached["end"] <= maximum:
            return index, cached["end"]
        raw, _ = call_text_model(
            **{key: route[key] for key in (
                "base_url", "api_key", "auth_mode", "model", "thinking_mode", "reasoning_effort",
            )},
            system_prompt=prompt,
            user_text=user_text,
            timeout=int(settings.get("translation_api_timeout_seconds", 300)),
            request_attempts=int(settings.get("http_retry_attempts", 2)),
            max_tokens=int(route["max_tokens"]), temperature=float(route["temperature"]),
            **({"cancel_event": cancel_event} if cancel_event is not None else {}),
        )
        raw = str(raw).strip()
        if not re.fullmatch(r"\d+", raw) or not minimum <= int(raw) <= maximum:
            return index, end
        # Recheck request size after movement. Optimization cannot expand an
        # already budgeted component beyond the configured ceiling.
        chosen = int(raw)
        cue_cap = int(settings.get("translation_execution_max_cues", 10000))
        left_start = ends[index-1] if index else 0
        right_end = ends[index+1]
        if chosen != end and (chosen - left_start > cue_cap or right_end - chosen > cue_cap
                or sum(len(c["source_text"]) for c in cues[left_start:chosen]) > max(12000,sum(len(c["source_text"]) for c in cues[left_start:end]))
                or sum(len(c["source_text"]) for c in cues[chosen:right_end]) > max(12000,sum(len(c["source_text"]) for c in cues[end:right_end]))):
            chosen = end
        if cache_directory:
            save_ai_block_cache(cache_directory, key, {"end": chosen})
        return index, chosen

    if requests:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(requests), 256)
        ) as pool:
            futures = {pool.submit(review, item): item for item in requests}
            for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
                try:
                    index, end = future.result()
                    ends[index] = end
                except Exception:
                    pass  # Keep the original seam on timeout, bad output or provider failure.
                if progress:
                    progress(done, len(requests))
    # Parallel adjacent decisions can together enlarge the middle component.
    # Reject both movements if their combination exceeds the request budget.
    original_ends, cursor = [], 0
    for group in groups:
        cursor += len(group["cues"])
        original_ends.append(cursor)
    for _ in range(len(ends)):
        reverted = False
        for i, stop in enumerate(ends):
            begin = ends[i-1] if i else 0
            old_begin = original_ends[i-1] if i else 0
            old_stop = original_ends[i]
            if (begin,stop) == (old_begin,old_stop):
                continue
            if stop-begin > int(settings.get("translation_execution_max_cues",10000)) or sum(len(c["source_text"]) for c in cues[begin:stop]) > max(12000,sum(len(c["source_text"]) for c in cues[old_begin:old_stop])):
                ends[i] = old_stop
                if i:
                    ends[i-1] = old_begin
                reverted = True
        if not reverted:
            break
    if ends == original_ends:
        return groups
    rebuilt = []
    start = 0
    for index, end in enumerate(ends):
        group = copy.deepcopy(groups[index])
        group.update(group_id=f"enhanced_{index + 1:04d}",
                     execution_block_id=f"enhanced_{index + 1:04d}",
                     cues=copy.deepcopy(cues[start:end]))
        rebuilt.append(group)
        start = end
    return rebuilt
