"""Narrow, revision-bound subtitle organization and punctuation tasks.

Models propose IDs or text-only punctuation changes. They never author operations,
timestamps, source tokens, media edits, or project revisions.
"""
from __future__ import annotations

import concurrent.futures
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from substar_core.ai_block_cache import fingerprint, load_ai_block_cache, save_ai_block_cache
from substar_core.artifacts import atomic_write_json
from substar_core.domain import ChangeKind, ChangeProvenance, EntityState
from substar_core.document_operations import apply_document_operation
from substar_core.editor.application.publication import write_candidate
from substar_core.editor.translation.regroup import review_request_groups
from substar_core.language_layout import layout_tokens
from substar_core.model_gateway import call_text_model
from substar_core.model_routing import resolve_stage_request
from substar_core.prompt_registry import render_prompt
from substar_core.storage import ProjectStore

INPUT_SCHEMA = "substar.text-task-input.v1"
RESULT_SCHEMA = "substar.text-task-result.v1"
PROMPTS = {kind: render_prompt(f"subtitle_{kind}", variant="default").text for kind in ("organize", "punctuation")}

# Deliberately conservative: lexical/internal punctuation is immutable. Apostrophes,
# hyphens, math signs, URL symbols and whitespace never disappear in this validator.
EDGE_MARKS = '.,?!;:，。？！；：、…—–“”"「」『』《》〈〉（）【】〔〕¿¡()[]{}'


def punctuation_allowed(before: str, after: str) -> bool:
    if not after or after != after.strip() or len(after) > len(before) + 16:
        return False
    if re.search(r"(?:[A-Za-z]\.){2,}$", before) and not after.endswith("."):
        return False
    if re.search(r"https?://|www[.]|@|[A-Za-z0-9_-]+[.][A-Za-z]{2,}(?=[./:?#]|$)", before):
        return before == after
    if re.match(r"^[.][A-Za-z0-9]", before) and not after.lstrip('“”"').startswith("."):
        return False
    left, right = before.strip(EDGE_MARKS), after.strip(EDGE_MARKS)
    if not left:  # Standalone punctuation can change only into punctuation.
        return bool(after) and all(c in EDGE_MARKS for c in before + after)
    if re.match(r"^[—–][0-9]", before) and not after.startswith(before[0]):
        return False
    return len(left) == len(right) and all(a.lower() == b.lower() for a, b in zip(left, right))


def task_blocks(document, settings):
    tokens = {t.token_id: t for t in document.display_tokens}
    rows, segment, previous = [], 0, None
    for cue in document.cues:
        if cue.state is not EntityState.ACTIVE:
            previous = None
            continue
        ids = [i for i in cue.display_token_ids if tokens[i].state is EntityState.ACTIVE]
        if not ids:
            previous = None
            continue
        if previous is None or previous.speaker != cue.speaker:
            segment += 1
        rows.append({"cue_id": cue.cue_id, "source_text": layout_tokens(tokens[i].text for i in ids),
                     "token_ids": ids, "start": cue.start, "end": cue.end,
                     "speaker": cue.speaker, "segment": segment})
        previous = cue
    blocks = []
    # A whole Cue is indivisible here; token-budget ceilings also bound long speech.
    for row in rows:
        if not blocks or row["start"] - blocks[-1]["cues"][0]["start"] >= 90 or sum(len(c["source_text"]) for c in blocks[-1]["cues"]) + len(row["source_text"]) > 12000:
            key = f"block_{len(blocks)+1:04d}"
            blocks.append({"group_id": key, "execution_block_id": key, "cues": []})
        blocks[-1]["cues"].append(row)
    return blocks


def validate_result(kind, value, block, document, settings):
    if not isinstance(value, dict):
        raise ValueError("模型结果必须是JSON对象")
    tokens = {t.token_id: t for t in document.display_tokens}
    owned = [i for c in block["cues"] for i in c["token_ids"]]
    if kind == "punctuation":
        if set(value) != {"changes"} or not isinstance(value["changes"], list):
            raise ValueError("标点结果字段不合法")
        seen = set()
        for change in value["changes"]:
            if not isinstance(change, dict) or set(change) - {"token_id", "expected_text", "text", "reason"} or not {"token_id", "expected_text", "text"} <= set(change):
                raise ValueError("标点修改字段不合法")
            tid = change["token_id"]
            if not isinstance(tid, str) or tid not in owned or tid in seen:
                raise ValueError("标点修改包含重复或越界ID")
            seen.add(tid)
            if change["expected_text"] != tokens[tid].text or not isinstance(change["text"], str) or not punctuation_allowed(tokens[tid].text, change["text"]):
                raise ValueError("标点修改改变了词汇、数字或受保护符号")
            if not tokens[tid].source_token_ids and change["text"] != tokens[tid].text:
                raise ValueError("无来源的人工插入词元为只读")
        return value
    if set(value) != {"delete_token_ids", "cues"} or not isinstance(value["delete_token_ids"], list) or not isinstance(value["cues"], list):
        raise ValueError("整理结果字段不合法")
    deleted = value["delete_token_ids"]
    remaining = []
    segment_by_id = {i: c["segment"] for c in block["cues"] for i in c["token_ids"]}
    from substar_core.policy import SubtitlePolicy
    policy = SubtitlePolicy.from_settings(settings)
    for row in value["cues"]:
        if not isinstance(row, dict) or set(row) != {"token_ids"} or not isinstance(row["token_ids"], list) or not row["token_ids"]:
            raise ValueError("整理字幕不能为空或含额外字段")
        ids = row["token_ids"]
        if any(not isinstance(i, str) or i not in segment_by_id for i in ids) or len({segment_by_id[i] for i in ids}) != 1:
            raise ValueError("整理跨越只读范围、已删除字幕或说话人")
        text = layout_tokens(tokens[i].text for i in ids)
        # Unchanged existing oversized Cues are allowed: uncertainty must be a no-op.
        unchanged = any(ids == c["token_ids"] for c in block["cues"])
        if policy.exceeds_hard_limit(text) and not unchanged:
            raise ValueError("整理后的字幕超过字符上限")
        remaining.extend(ids)
    all_ids = deleted + remaining
    if any(not isinstance(i, str) for i in all_ids) or len(all_ids) != len(set(all_ids)) or set(all_ids) != set(owned):
        raise ValueError("整理结果必须唯一覆盖全部OWN词元")
    if remaining != [i for i in owned if i not in set(deleted)]:
        raise ValueError("整理改变了词元顺序")
    return value


def apply_organize(document, block, value, task_id):
    """Use reversible native operations. Deletion alone never retimes a Cue."""
    result = document
    counter = 0
    def op(kind, payload):
        nonlocal result, counter
        counter += 1
        provenance = ChangeProvenance(kind=ChangeKind.AI, operation="ai_organize_apply", actor="ai-organize", metadata={"task_id": task_id})
        result = apply_document_operation(result, {"operation_id": f"{task_id}:{block['group_id']}:{counter}", "type": kind,
                                                   "payload": {**payload, "provenance": provenance.to_dict()}})
    if value["delete_token_ids"]:
        op("delete", {"token_ids": value["delete_token_ids"]})
    active_ids = {t.token_id for t in result.display_tokens if t.state is EntityState.ACTIVE}
    for segment in dict.fromkeys(c["segment"] for c in block["cues"]):
        original = [c for c in block["cues"] if c["segment"] == segment]
        segment_ids = {i for c in original for i in c["token_ids"]}
        desired = [r["token_ids"] for r in value["cues"] if r["token_ids"][0] in segment_ids]
        owned_cues = {c["cue_id"] for c in original}
        cues_by_id = {c.cue_id:c for c in result.cues}
        owner = {i:c.cue_id for c in result.cues if c.cue_id in owned_cues for i in c.display_token_ids}
        for number, ids in enumerate(desired):
            anchor, last = owner[ids[0]], owner[ids[-1]]
            # Preserve every existing boundary that still fits the desired plan.
            # Only the Cues touched by a moved boundary need native operations.
            while anchor != last:
                left = cues_by_id[anchor]
                neighbor = result.cues[result.cues.index(left)+1]
                if neighbor.cue_id not in owned_cues or neighbor.state is not EntityState.ACTIVE:
                    raise ValueError("整理不能越过已有删除字幕或只读范围")
                op("merge_cues", {"cue_ids":[anchor,neighbor.cue_id]})
                for tid in neighbor.display_token_ids:
                    owner[tid] = anchor
                if last == neighbor.cue_id:
                    last = anchor
                cues_by_id = {c.cue_id:c for c in result.cues}
            active = [i for i in cues_by_id[anchor].display_token_ids if i in active_ids]
            if active[:len(ids)] != ids:
                raise ValueError("整理结果与当前词元归属不一致")
            if len(active) > len(ids):
                right = f"cue_{fingerprint({'task':task_id,'block':block['group_id'],'segment':segment,'cut':number})[:24]}"
                op("split_cue", {"cue_id":anchor,"after_token_id":ids[-1],"right_cue_id":right})
                owned_cues.add(right)
                cues_by_id = {c.cue_id:c for c in result.cues}
                for tid in cues_by_id[right].display_token_ids:
                    owner[tid] = right
        for cue in tuple(result.cues):
            if cue.cue_id in owned_cues and cue.state is EntityState.ACTIVE and not any(i in active_ids for i in cue.display_token_ids):
                op("delete", {"cue_ids":[cue.cue_id]})
    return result


def compute(kind, project, artifacts, payload, task_id, progress, cancel_event=None):
    settings = payload["settings"]
    revision = ProjectStore.open(project / "project").load_latest()
    if revision is None or revision.revision_id != payload["expected_revision_id"]:
        raise ValueError("文稿版本已变化，请重新运行")
    document = revision.document
    cache_root = artifacts.parent.parent.parent / "text-cache" if artifacts.parent.parent.name == "attempts" else artifacts
    def check_cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("任务已取消")
    blocks = task_blocks(document, settings)
    progress("planning", 0, len(blocks))
    boundary_settings = {**settings, "translation_layout": {}, "translation_mapping_mode": "many_to_many",
                         "translation_regroup": bool(payload.get("optimize_boundaries")), "_boundary_stage": "calibration",
                         "_cancel_event": cancel_event}
    blocks = review_request_groups(blocks, boundary_settings, progress=lambda n, total: progress("planning", n, total),
                                  cache_directory=cache_root / "boundary-cache")
    check_cancelled()
    tokens = {t.token_id: t for t in document.display_tokens}
    route = resolve_stage_request(settings, "calibration")
    prompt = settings["text_task_prompt"]
    all_cues = [c for b in blocks for c in b["cues"]]
    cue_positions = {c["cue_id"]:i for i,c in enumerate(all_cues)}
    baseline_hash = revision.document_hash or document.content_hash()
    def run(block):
        check_cancelled()
        start = cue_positions[block["cues"][0]["cue_id"]]
        end = start + len(block["cues"])
        def render(cues):
            return [{**c, "tokens": [{"token_id": i, "text": tokens[i].text,
                                      "editable": kind == "organize" or bool(tokens[i].source_token_ids)} for i in c["token_ids"]]} for c in cues]
        material = {"OWN": render(block["cues"]), "CONTEXT": render(all_cues[max(0,start-5):start] + all_cues[end:end+5]),
                    "limits": {k: v for k, v in settings.items() if k.endswith("_hard_limit")},
                    "count_rules": {"count_spaces": settings.get("english_count_spaces", True), "count_punctuation": settings.get("english_count_punctuation", True)},
                    "language": settings.get("language", "Auto")}
        key = fingerprint({"stage": kind, "baseline": baseline_hash, "material": material,
                           "prompt": prompt, "model": {k:v for k,v in route.items() if k not in {"api_key"}}, "contract": 1})
        cached = load_ai_block_cache(cache_root / "block-cache", key)
        if cached is not None:
            return validate_result(kind, cached, block, document, settings)
        raw, _ = call_text_model(**{k: route[k] for k in ("base_url", "api_key", "auth_mode", "model", "thinking_mode", "reasoning_effort")},
                                system_prompt=prompt, user_text=json.dumps(material, ensure_ascii=False),
                                max_tokens=int(route["max_tokens"]), temperature=float(route["temperature"]),
                                timeout=int(settings.get("translation_api_timeout_seconds",300)),
                                request_attempts=int(settings.get("http_retry_attempts",2)), cancel_event=cancel_event)
        if len(raw) > 2_000_000:
            raise ValueError("模型输出过长")
        def unique_object(pairs):
            value = {}
            for key, child in pairs:
                if key in value:
                    raise ValueError("模型JSON包含重复字段")
                value[key] = child
            return value
        value = validate_result(kind, json.loads(raw, object_pairs_hook=unique_object), block, document, settings)
        save_ai_block_cache(cache_root / "block-cache", key, value)
        return value
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,min(len(blocks),50))) as pool:
        futures = {pool.submit(run,b): i for i,b in enumerate(blocks)}
        for future in concurrent.futures.as_completed(futures):
            results[futures[future]] = future.result()
            progress("executing",len(results),len(blocks))
    progress("validating",len(blocks),len(blocks))
    check_cancelled()
    result = document
    count = 0
    punctuation_changes = {}
    for index, block in enumerate(blocks):
        value = results[index]
        if kind == "organize":
            result = apply_organize(result, block, value, task_id)
            count += len(value["delete_token_ids"])
        else:
            punctuation_changes.update({c["token_id"]:c for c in value["changes"] if c["text"] != c["expected_text"]})
    if punctuation_changes:
        changed, updated = [], []
        for token in result.display_tokens:
            if token.token_id in punctuation_changes:
                c = punctuation_changes[token.token_id]
                provenance = ChangeProvenance(kind=ChangeKind.AI, operation="ai_calibration_apply", actor="ai-punctuation",
                    metadata={"stage":"punctuation", "task_id":task_id, "affects_translation":False,
                              "ai_calibration":{"applied":True,"before_text":token.text,"after_text":c["text"],"affects_translation":False}})
                token = replace(token, text=c["text"], original_text=token.text, provenance=provenance)
                changed.append(provenance)
            updated.append(token)
        result = replace(result, display_tokens=tuple(updated), changes=(*result.changes,*changed))
        count += len(changed)
    before = {t.token_id:t for t in document.display_tokens}
    if result.source_tokens != document.source_tokens or result.properties != document.properties:
        raise ValueError("AI任务改变了源词或媒体属性")
    if kind == "organize" and any(t.text != before[t.token_id].text for t in result.display_tokens):
        raise ValueError("AI整理改变了保留词元文字")
    result.validate()
    provenance = ChangeProvenance(kind=ChangeKind.AI,operation=f"ai_{kind}_apply",actor=f"ai-{kind}",
                                 metadata={"task_id":task_id,"changed_count":count,"optimize_boundaries":payload.get("optimize_boundaries",False)})
    progress("materializing",len(blocks),len(blocks))
    check_cancelled()
    candidate = write_candidate(artifacts,revision,result,provenance)
    summary = {"result_revision_id":candidate.revision_id,"changed_count":count,"blocks":len(blocks),
               "ai_progress":{"kind":kind,"phase":"completed","unit_kind":f"{kind}_block","unit_label":"块",
                              "units":{"planned":len(blocks),"completed":len(blocks),"accepted":len(blocks)}}}
    atomic_write_json(artifacts / "result.json",summary)
    return summary
