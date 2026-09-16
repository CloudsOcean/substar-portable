"""Portable opinions; importing never writes a subtitle revision."""
from copy import deepcopy
import json
import math

from .review_notes import now, text_value

SCHEMA = "substar.review-opinions.v1"


def validate_note(value):
    if not isinstance(value, dict):
        raise ValueError("审阅意见格式无效")
    result = deepcopy(value)
    for key in ("id", "created_at"):
        if not isinstance(result.get(key), str) or not 0 < len(result[key]) <= 200:
            raise ValueError("审阅意见缺少有效标识或时间")
    result["text"] = text_value(result.get("text"))
    if result.get("status") not in {"open", "accepted", "rejected", "resolved", "deleted"}:
        raise ValueError("审阅意见状态无效")
    if not isinstance(result.get("author", ""), str):
        raise ValueError("审阅人员无效")
    a = result.get("anchor")
    if not isinstance(a, dict) or not isinstance(a.get("cue_id"), str) or not a["cue_id"] or a.get("track") not in {"source", "target"}:
        raise ValueError("审阅意见定位信息无效")
    if not isinstance(a.get("text_snapshot"), str):
        raise ValueError("缺少审阅原文快照")
    ids = a.get("token_ids", [])
    if not isinstance(ids, list) or len(ids) > 10000 or any(not isinstance(x, str) or not x for x in ids):
        raise ValueError("审阅词元无效")
    for key in ("start", "end"):
        if type(a.get(key)) not in {int, float} or not math.isfinite(a[key]) or a[key] < 0:
            raise ValueError("审阅时间无效")
    if a["end"] < a["start"]:
        raise ValueError("审阅时间范围无效")
    span = a.get("text_range")
    if span is not None and (not isinstance(span, list) or len(span) != 2 or any(type(x) is not int for x in span) or not 0 <= span[0] < span[1]):
        raise ValueError("审阅文字范围无效")
    replies = result.setdefault("replies", [])
    if not isinstance(replies, list) or any(not isinstance(r, dict) or not isinstance(r.get("text"), str) or not isinstance(r.get("author", ""), str) for r in replies):
        raise ValueError("审阅回复无效")
    for key in ("anchor_status", "current_start"):
        result.pop(key, None)
    conflicts = result.get("import_conflicts", [])
    if not isinstance(conflicts, list) or len(conflicts) > 1000:
        raise ValueError("审阅冲突信息无效")
    for conflict in conflicts:
        if not isinstance(conflict, dict) or "import_conflicts" in conflict:
            raise ValueError("审阅冲突信息无效")
        validate_note(conflict)
    return result


def signature(note):
    return json.dumps({k: note.get(k) for k in ("anchor", "text", "author", "status", "replies")}, sort_keys=True, ensure_ascii=False)


def export_opinions(project_id, revision_id, notes):
    return {"schema_version": SCHEMA, "project_id": project_id,
            "source_revision_id": revision_id, "exported_at": now(), "notes": deepcopy(notes)}


def validate_package(package, project_id):
    if not isinstance(package, dict) or package.get("schema_version") != SCHEMA:
        raise ValueError("请选择 Substar 审阅意见 JSON 文件")
    if package.get("project_id") != project_id:
        raise ValueError("审阅意见属于其他工程，未导入")
    values = package.get("notes")
    if not isinstance(values, list) or len(values) > 10000:
        raise ValueError("审阅意见数量无效")
    notes = [validate_note(note) for note in values]
    if len({n["id"] for n in notes}) != len(notes):
        raise ValueError("文件含重复的审阅意见标识")
    return notes


def overlaps(a, b):
    a, b = a["anchor"], b["anchor"]
    if a["track"] != b["track"] or a["cue_id"] != b["cue_id"]:
        return False
    if a.get("token_ids") and b.get("token_ids"):
        return bool(set(a["token_ids"]) & set(b["token_ids"]))
    x, y = a.get("text_range"), b.get("text_range")
    return not x or not y or max(x[0], y[0]) < min(x[1], y[1])


def merge_opinions(notes, incoming):
    counts = {"added": 0, "unchanged": 0, "conflicts": 0}
    for candidate in incoming:
        local = next((n for n in notes if n["id"] == candidate["id"]), None)
        if local is None and candidate["status"] != "deleted":
            local = next((n for n in notes if n["status"] != "deleted" and overlaps(n, candidate)), None)
        if local is None:
            notes.append(deepcopy(candidate))
            counts["added"] += 1
            continue
        variants = [candidate, *candidate.get("import_conflicts", [])]
        changed = False
        for variant in variants:
            if signature(local) == signature(variant):
                continue
            conflicts = local.setdefault("import_conflicts", [])
            if not any(signature(c) == signature(variant) for c in conflicts):
                copy = deepcopy(variant)
                copy.pop("import_conflicts", None)
                conflicts.append(copy)
                changed = True
        counts["conflicts" if changed else "unchanged"] += 1
    return counts
