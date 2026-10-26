"""Optional boundary review for a single translation execution."""
from __future__ import annotations

import copy
import concurrent.futures
import json
import re
from typing import Any, Callable

from substar_core.model_gateway import call_text_model
from substar_core.model_routing import resolve_stage_request
from substar_core.translation_layout import route_text, cue_language


BOUNDARY_PROMPT = """你是字幕翻译请求的分块编辑。只调整发送给翻译模型的请求边界，不修改字幕。
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
        reviewed = review_request_groups(components, {**settings, "translation_mapping_mode": "many_to_many"}, progress=progress)
        return [
            {"group_id": f"line:{cue['cue_id']}", "execution_block_id": group["execution_block_id"], "cues": [cue]}
            for group in reviewed for cue in group["cues"]
        ]
    cues = [cue for group in groups for cue in group["cues"]]
    ends, cursor = [], 0
    for group in groups:
        cursor += len(group["cues"])
        ends.append(cursor)
    route = resolve_stage_request(settings, "translation")
    layout = settings.get("translation_layout")

    def cue_route(cue):
        if layout:
            return route_text(cue["source_text"], layout)
        return {"target_language": cue.get("target_language")}

    requests = []
    for index, end in enumerate(ends[:-1]):
        previous = ends[index - 1] if index else 0
        following = ends[index + 1]
        # Disjoint movement windows ensure parallel decisions cannot cross.
        minimum = max(previous + 1, (previous + end) // 2 + 1, end - 4)
        maximum = min(following - 1, (end + following) // 2, end + 4)
        if cue_route(cues[end - 1]) != cue_route(cues[end]):
            continue
        expected_route = cue_route(cues[end - 1])
        while minimum < end and cue_route(cues[minimum - 1]) != expected_route:
            minimum += 1
        while maximum > end and cue_route(cues[maximum]) != expected_route:
            maximum -= 1
        if minimum == maximum:
            continue
        requests.append((index, end, minimum, maximum))
    if progress:
        progress(0, len(requests))

    def review(item):
        index, end, minimum, maximum = item
        rows = [
            {"number": i + 1, "text": cue["source_text"],
             "start": cue.get("start"), "end": cue.get("end")}
            for i, cue in enumerate(cues) if end - 5 <= i < end + 5
        ]
        raw, _ = call_text_model(
            **{key: route[key] for key in (
                "base_url", "api_key", "auth_mode", "model", "thinking_mode", "reasoning_effort",
            )},
            system_prompt=BOUNDARY_PROMPT,
            user_text=json.dumps({"CURRENT_END": end, "MIN_END": minimum,
                                  "MAX_END": maximum, "cues": rows}, ensure_ascii=False),
            timeout=int(settings.get("translation_api_timeout_seconds", 300)),
            request_attempts=int(settings.get("http_retry_attempts", 2)),
            max_tokens=int(route["max_tokens"]), temperature=float(route["temperature"]),
        )
        raw = str(raw).strip()
        if not re.fullmatch(r"\d+", raw) or not minimum <= int(raw) <= maximum:
            raise ValueError(f"翻译增强分组返回无效边界（原边界 {end}）")
        return index, int(raw)

    if requests:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(requests), 256)
        ) as pool:
            futures = [pool.submit(review, item) for item in requests]
            for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
                index, end = future.result()
                ends[index] = end
                if progress:
                    progress(done, len(requests))
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
