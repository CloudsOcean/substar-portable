"""Revision-aware review records, independent of subtitle text mutations."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def anchor_for(document, cue_id, track="source", token_ids=(), text_range=None):
    if track not in {"source", "target"}:
        raise ValueError("无效的批注轨道")
    cue = next((c for c in document.cues if c.cue_id == cue_id and c.state.value == "active"), None)
    if cue is None:
        raise ValueError("字幕不存在或已删除")
    tokens = {t.token_id: t for t in document.display_tokens}
    if token_ids and (track != "source" or not set(token_ids).issubset(cue.display_token_ids)):
        raise ValueError("批注词元不属于该字幕原文")
    chosen = tuple(t for t in cue.display_token_ids if t in token_ids) if token_ids else cue.display_token_ids
    from substar_core.language_layout import layout_tokens
    text = (cue.target.target_text if cue.target else "") if track == "target" else layout_tokens(tokens[t].text for t in chosen)
    if track == "source" and cue.source_text is not None:
        text = cue.source_text
    if not text:
        raise ValueError("不能批注空文本")
    if text_range is not None:
        if token_ids or len(text_range) != 2 or any(type(x) is not int for x in text_range) or not 0 <= text_range[0] < text_range[1] <= len(text):
            raise ValueError("无效的文字批注范围")
        text = text[text_range[0]:text_range[1]]
    return {"cue_id": cue_id, "track": track, "token_ids": list(chosen) if token_ids else [],
            "text_range": text_range, "text_snapshot": text, "start": cue.start, "end": cue.end}


def text_value(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 10000:
        raise ValueError("批注内容应为 1–10000 个字符")
    return value.strip()


def add_note(document, *, cue_id, track, text, author="", token_ids=(), text_range=None):
    return {"id": uuid4().hex, "anchor": anchor_for(document, cue_id, track, token_ids, text_range),
            "text": text_value(text), "author": str(author)[:100], "created_at": now(),
            "status": "open", "replies": []}


def change_note(note, *, action, text=None, author=""):
    result = deepcopy(note)
    if action == "keep_local":
        result.pop("import_conflicts", None)
    elif action in {"resolve", "reopen"}:
        result["status"] = "resolved" if action == "resolve" else "open"
    elif action in {"accept", "reject"}:
        result["status"] = "accepted" if action == "accept" else "rejected"
    elif action == "delete":
        result["status"] = "deleted"
    elif action == "reply":
        result["replies"].append({"text": text_value(text), "author": str(author)[:100], "created_at": now()})
    else:
        raise ValueError("不支持的批注操作")
    result["updated_at"] = now()
    return result


def upsert_note(notes, candidate):
    """A token belongs to at most one active annotation, regardless of decision."""
    anchor = candidate["anchor"]
    token_ids = set(anchor.get("token_ids", []))
    matches = [note for note in notes if note.get("status") != "deleted"
               and note["anchor"]["track"] == anchor["track"]
               and note["anchor"]["cue_id"] == anchor["cue_id"]
               and (bool(token_ids.intersection(note["anchor"].get("token_ids", []))) if token_ids
                    else note["anchor"].get("text_range") == anchor.get("text_range")
                    and not note["anchor"].get("token_ids"))]
    if len(matches) > 1:
        raise ValueError("所选词元已有多条历史批注，请先删除多余批注")
    if matches:
        existing = matches[0]
        if token_ids and not token_ids.issubset(set(existing["anchor"].get("token_ids", []))):
            raise ValueError("所选词元与已有批注组重叠，请先删除整组批注再重新添加")
        if existing["text"] != candidate["text"]:
            existing["text"] = candidate["text"]
            existing["status"] = "open"
            existing["updated_at"] = now()
        return existing
    notes.append(candidate)
    return candidate


def project_note(document, note):
    result = deepcopy(note)
    anchor = note["anchor"]
    try:
        cue_id = anchor["cue_id"]
        ids = set(anchor.get("token_ids", []))
        if ids and anchor["track"] == "source":
            matches = [c for c in document.cues if c.state.value == "active" and ids.issubset(c.display_token_ids)]
            if len(matches) != 1:
                raise ValueError("原词元组无法唯一定位")
            cue_id = matches[0].cue_id
        current = anchor_for(document, cue_id, anchor["track"], anchor.get("token_ids", []), anchor.get("text_range"))
        result["anchor"]["cue_id"] = cue_id
        result["anchor_status"] = "current" if current["text_snapshot"] == anchor["text_snapshot"] else "text_changed"
        result["current_start"] = current["start"]
    except ValueError:
        result["anchor_status"] = "needs_relocation"
        result["current_start"] = anchor["start"]
    return result
