"""Non-destructive source-clock annotations and export projections (seconds)."""
from __future__ import annotations

import copy
import math
import uuid
from fractions import Fraction
from xml.etree import ElementTree as ET


def validate_timeline(value):
    if not isinstance(value, dict):
        raise ValueError("剪辑数据必须是对象")
    result = copy.deepcopy(value)
    result.setdefault("markers", [])
    result.setdefault("plans", [])
    result.setdefault("active_plan_id", None)
    def text(item, key):
        if not isinstance(item.get(key, ""), str) or len(item.get(key, "")) > 4000:
            raise ValueError(f"无效的 {key}")
    def ranges(items, point=False):
        if not isinstance(items, list) or len(items) > 10000:
            raise ValueError("片段列表过长或无效")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("片段必须是对象")
            start, end = item.get("start"), item.get("end")
            if type(start) not in (int, float) or not math.isfinite(start) or start < 0:
                raise ValueError("片段起点无效")
            if end is None and point:
                continue
            if type(end) not in (int, float) or not math.isfinite(end) or end <= start:
                raise ValueError("片段终点必须大于起点")
    def entities(items):
        if not isinstance(items, list) or len(items) > 10000:
            raise ValueError("对象列表过长或无效")
        ids = set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] or len(item["id"]) > 120 or item["id"] in ids:
                raise ValueError("对象 ID 必须非空且唯一")
            ids.add(item["id"])
            for key in ("name", "note", "reason", "goal"):
                text(item, key)
            if "enabled" in item and not isinstance(item["enabled"], bool):
                raise ValueError("启用状态必须是布尔值")
        return ids
    entities(result["markers"])
    ranges(result["markers"], True)
    ids = entities(result["plans"])
    for plan in result["plans"]:
        plan.setdefault("cuts", [])
        plan.setdefault("keep", None)
        entities(plan["cuts"])
        ranges(plan["cuts"])
        if plan["keep"] is not None:
            ranges(plan["keep"])
    if result["active_plan_id"] is not None and result["active_plan_id"] not in ids:
        raise ValueError("当前导出方案不存在")
    return result


def union_ranges(ranges, duration):
    merged = []
    for item in sorted(ranges, key=lambda r: r["start"]):
        a, b = max(0, item["start"]), min(duration, item["end"])
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def kept_intervals(plan, duration):
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("媒体时长无效")
    keep = union_ranges(plan.get("keep") if plan.get("keep") is not None else [{"start":0,"end":duration}], duration)
    cuts = union_ranges([c for c in plan.get("cuts", []) if c.get("enabled", True)], duration)
    out = []
    for a, b in keep:
        cursor = a
        for c, d in cuts:
            if d <= cursor or c >= b:
                continue
            if c > cursor:
                out.append((cursor, min(c, b)))
            cursor = min(b, max(cursor, d))
        if cursor < b:
            out.append((cursor, b))
    return out


def project_range(start, end, intervals):
    offset = 0.0
    for a, b in intervals:
        left, right = max(a, start), min(b, end)
        if right > left:
            yield offset + left - a, offset + right - a
        offset += b - a


def frame_intervals(intervals, fps):
    rate = Fraction(fps)
    if rate <= 0 or rate > 240:
        raise ValueError("无效帧率")
    cursor = Fraction(0)
    previous = 0
    result = []
    for a,b in intervals:
        cursor += Fraction(str(b)) - Fraction(str(a))
        boundary = round(cursor * rate)
        length = boundary - previous
        previous = boundary
        if length > 0:
            source = round(Fraction(str(a)) * rate)
            result.append((float(source/rate),float((source+length)/rate)))
    return result


def projected_subtitles(document, intervals):
    """A disposable export view; token text and translation are never changed."""
    from dataclasses import replace
    from substar_core.domain import EditorDocument, DocumentProperties, EntityState
    from substar_core.language_layout import layout_tokens
    tokens = {t.token_id:t for t in document.display_tokens}
    cues = []
    for cue in document.cues:
        if cue.state != EntityState.ACTIVE:
            continue
        text = cue.source_text if cue.source_text is not None else layout_tokens(
            tokens[t].text for t in cue.display_token_ids if tokens[t].state == EntityState.ACTIVE)
        if not text.strip() and cue.target is None:
            continue
        for start, end in project_range(cue.start, cue.end, intervals):
            cues.append(replace(cue, cue_id=f"export_{len(cues)}", index=len(cues), start=start, end=end,
                                display_token_ids=(), source_text=text, group_id=None))
    cues.sort(key=lambda c:(c.start,c.end,c.index))
    cues = tuple(replace(c, index=i) for i,c in enumerate(cues))
    return EditorDocument(document.document_id, (), (), cues, presentation=document.presentation,
                          properties=DocumentProperties(speaker_names=document.properties.speaker_names,
                                                        script_projection=document.properties.script_projection))


def export_xml(path, intervals, fps="25/1", width=1920, height=1080, has_video=True, has_audio=True):
    """FCP7 media edit list. Use one cumulative frame grid for both streams."""
    rate = Fraction(fps)
    if rate <= 0 or rate > 240:
        raise ValueError("不支持的帧率")
    root = ET.Element("xmeml", version="5")
    seq = ET.SubElement(root, "sequence", id="substar-cut")
    def tag(parent, key, value):
        ET.SubElement(parent, key).text = str(value)
    def add_rate(parent):
        r = ET.SubElement(parent,"rate")
        tag(r,"timebase",round(rate));tag(r,"ntsc","TRUE" if rate.denominator == 1001 else "FALSE")
    cursor = Fraction(0)
    boundaries = [0]
    for a,b in intervals:
        cursor += Fraction(str(b)) - Fraction(str(a))
        boundaries.append(round(cursor*rate))
    tag(seq,"name","Substar 剪辑方案");tag(seq,"duration",boundaries[-1]);add_rate(seq)
    media = ET.SubElement(seq,"media")
    for kind, enabled in (("video",has_video),("audio",has_audio)):
        if not enabled:
            continue
        stream = ET.SubElement(media,kind)
        if kind == "video":
            fmt = ET.SubElement(ET.SubElement(stream,"format"),"samplecharacteristics")
            add_rate(fmt);tag(fmt,"width",width);tag(fmt,"height",height);tag(fmt,"pixelaspectratio","square")
        track = ET.SubElement(stream,"track")
        for i,(a,b) in enumerate(intervals):
            length = boundaries[i+1]-boundaries[i]
            if length <= 0:
                continue
            clip = ET.SubElement(track,"clipitem",id=f"{kind}-{i}")
            source = round(Fraction(str(a))*rate)
            for key,value in (("name",path.name),("start",boundaries[i]),("end",boundaries[i+1]),("in",source),("out",source+length),("duration",length)):
                tag(clip,key,value)
            add_rate(clip)
            file = ET.SubElement(clip,"file",id="source-media")
            if i == 0:
                tag(file,"name",path.name);tag(file,"pathurl",path.resolve().as_uri());add_rate(file)
            st = ET.SubElement(clip,"sourcetrack");tag(st,"mediatype",kind);tag(st,"trackindex",1)
    return ET.tostring(root,encoding="unicode",xml_declaration=True)


def ai_plan_from_ids(document, proposal, goal):
    """The model chooses stable cue IDs; it cannot invent arbitrary cut times."""
    cues = {c.cue_id:c for c in document.cues if c.state.value == "active"}
    keep_ids = proposal.get("keep_cue_ids")
    if not isinstance(keep_ids,list) or not keep_ids or any(i not in cues for i in keep_ids):
        raise ValueError("AI 返回的保留字幕 ID 无效")
    cuts = []
    for item in proposal.get("cuts",[]):
        ids = item.get("cue_ids",[])
        if not ids or any(i not in cues for i in ids):
            raise ValueError("AI 返回的剪除字幕 ID 无效")
        if any(i in keep_ids for i in ids):
            raise ValueError("同一条字幕不能同时保留和剪除")
        if any(cues[i].start < cues[k].end and cues[i].end > cues[k].start for i in ids for k in keep_ids):
            raise ValueError("剪除范围与保留字幕重叠，请保留内容并添加标记")
        for i in ids:
            cuts.append({"id":uuid.uuid4().hex,"start":cues[i].start,"end":cues[i].end,
                         "enabled":True,"source":"ai","reason":str(item.get("reason",""))[:4000]})
    # Keep pauses between chosen neighbouring sentences inside each selected run.
    ordered = sorted(cues.values(),key=lambda c:(c.start,c.index))
    pause_ids = proposal.get("remove_pauses_after", [])
    if not isinstance(pause_ids, list) or any(i not in cues for i in pause_ids):
        raise ValueError("AI 返回的空隙 ID 无效")
    for index, cue in enumerate(ordered[:-1]):
        if cue.cue_id in pause_ids:
            # Earlier overlapping speech may still occupy this gap.
            end = max(c.end for c in ordered[:index+1])
            following = ordered[index+1].start
            if following > end:
                cuts.append({"id":uuid.uuid4().hex,"start":end,"end":following,"source":"ai",
                             "enabled":True,"reason":"AI 建议移除句间空隙"})
    runs=[]; selected=set(keep_ids)
    for cue in ordered:
        if cue.cue_id in selected:
            if runs and runs[-1]["open"]:
                runs[-1]["end"]=max(runs[-1]["end"],cue.end)
            else:
                runs.append({"start":cue.start,"end":cue.end,"open":True})
        elif runs:
            runs[-1]["open"]=False
    return {"id":uuid.uuid4().hex,"name":str(proposal.get("name") or "AI 方案")[:120],
            "goal":goal,"keep":[{"start":r["start"],"end":r["end"]} for r in runs],"cuts":cuts}


def ai_review_markers(document, proposal):
    """Review suggestions can only refer to actual cues, never arbitrary model times."""
    cues = {c.cue_id: c for c in document.cues if c.state.value == "active"}
    entries = proposal.get("markers", [])
    if not isinstance(entries, list) or len(entries) > 1000:
        raise ValueError("AI 标记列表无效")
    markers = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("cue_id") not in cues:
            raise ValueError("AI 标记必须引用已有字幕")
        cue = cues[entry["cue_id"]]
        kind = entry.get("kind", "region")
        if kind not in ("point", "region"):
            raise ValueError("AI 标记类型无效")
        marker = {"id": uuid.uuid4().hex, "start": cue.start, "name": "剪辑待确认",
                  "note": str(entry.get("note", ""))[:4000], "color": "#d6ad65",
                  "source": "ai_cut", "cue_id": cue.cue_id}
        if kind == "region":
            marker["end"] = cue.end
        markers.append(marker)
    return markers


def ai_plan_from_ranges(proposal, goal, duration, base_plan=None):
    """Apply explicit source-clock edits to a baseline; never infer cuts from cues."""
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("媒体时长无效")
    operations = proposal.get("operations")
    if not isinstance(operations, list) or len(operations) > 1000:
        raise ValueError("AI 必须返回 operations 时间范围列表")
    cuts = []
    if base_plan is not None:
        cursor = 0
        for a, b in kept_intervals(base_plan, duration):
            if a > cursor:
                cuts.append({"start": cursor, "end": a, "reason": "延续当前方案"})
            cursor = b
        if cursor < duration:
            cuts.append({"start": cursor, "end": duration, "reason": "延续当前方案"})
    applied = []
    for op in operations:
        if not isinstance(op, dict) or op.get("action") not in ("remove", "restore"):
            raise ValueError("剪辑操作必须是 remove 或 restore")
        a, b = op.get("start"), op.get("end")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (a, b)) or not 0 <= a < b <= duration:
            raise ValueError("剪辑范围必须位于原片时长内，且终点大于起点")
        reason = str(op.get("reason", ""))[:4000]
        if op["action"] == "remove":
            cuts.append({"start": a, "end": b, "reason": reason})
        else:
            remaining = []
            for cut in cuts:
                if cut["end"] <= a or cut["start"] >= b:
                    remaining.append(cut)
                else:
                    if cut["start"] < a:
                        remaining.append({**cut, "end": a})
                    if cut["end"] > b:
                        remaining.append({**cut, "start": b})
            cuts = remaining
        applied.append({"action": op["action"], "start": a, "end": b, "reason": reason})
    merged = []
    for cut in sorted(cuts, key=lambda c: c["start"]):
        if merged and cut["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], cut["end"])
            if cut["reason"] and cut["reason"] not in merged[-1]["reason"]:
                merged[-1]["reason"] += "；" + cut["reason"]
        else:
            merged.append(dict(cut))
    plan = {"id": uuid.uuid4().hex, "name": str(proposal.get("name") or "AI 方案")[:120],
            "goal": goal, "keep": None, "operations": applied,
            "cuts": [{**c, "id": uuid.uuid4().hex, "enabled": True, "source": "ai"} for c in merged]}
    if not kept_intervals(plan, duration):
        raise ValueError("生成方案没有保留内容")
    return plan
