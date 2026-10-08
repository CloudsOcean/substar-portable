from __future__ import annotations

import json
import re
import uuid
import threading
from functools import wraps
from datetime import datetime, timezone
from typing import Any

from .artifacts import atomic_write_json
from .config import APP_DATA_DIR, GLOSSARY_FILE

ALLOWED_TYPES = {"person", "organization", "place", "program", "product", "technical", "other"}
GLOBAL_GLOSSARY_ID = "global"
GLOSSARY_SCHEMA_VERSION = "substar.glossary-library.v2"
INJECTION_STAGES = {"asr", "calibration", "translation"}
_LIBRARY_LOCK = threading.RLock()

def _locked(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        with _LIBRARY_LOCK:
            return fn(*args, **kwargs)
    return call

QWEN_HOTWORD_DEFAULT_WEIGHT = 4
QWEN_HOTWORD_MAX_COUNT = 2000


def _clean_text(value: Any, limit: int = 300) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or "")).strip()[:limit]


def normalize_collection(value: dict[str, Any]) -> dict[str, Any]:
    raw_permissions = value.get("injection_permissions", [])
    if not isinstance(raw_permissions, list) or any(stage not in INJECTION_STAGES for stage in raw_permissions if isinstance(stage, str)) or any(not isinstance(stage, str) for stage in raw_permissions):
        raise ValueError("词库注入权限无效")
    permissions = [stage for stage in ("asr", "translation") if stage in raw_permissions]
    kind = "global" if value.get("kind") == "global" else "project"
    if kind == "global":
        return {"id": GLOBAL_GLOSSARY_ID, "name": "未分配", "kind": "global", "injection_permissions": []}
    name = _clean_text(value.get("name"), 100)
    if not name:
        raise ValueError("项目词库名称不能为空")
    collection_id = _clean_text(value.get("id"), 80) or f"glossary_{uuid.uuid4().hex}"
    if collection_id == GLOBAL_GLOSSARY_ID:
        raise ValueError("项目词库 ID 不能使用 global")
    return {"id": collection_id, "name": name, "kind": "project", "injection_permissions": permissions}


def normalize_entry(value: dict[str, Any]) -> dict[str, Any]:
    source = _clean_text(value.get("source"))
    if not source:
        raise ValueError("术语原词不能为空")
    entry_type = _clean_text(value.get("type", "other"), 30)
    aliases = value.get("aliases", [])
    if isinstance(aliases, str):
        aliases = re.split(r"[,，;\n]+", aliases)
    if not isinstance(aliases, list):
        aliases = []
    try:
        hotword_weight = max(1, min(5, int(value.get("hotword_weight", 4))))
    except (TypeError, ValueError):
        hotword_weight = 4
    glossary_id = _clean_text(value.get("glossary_id"), 80) or GLOBAL_GLOSSARY_ID
    return {
        "id": _clean_text(value.get("id"), 80) or uuid.uuid4().hex,
        "source": source,
        "standard_source": _clean_text(value.get("standard_source")),
        "target": _clean_text(value.get("target")),
        "type": entry_type if entry_type in ALLOWED_TYPES else "other",
        "case_sensitive": bool(value.get("case_sensitive", False)),
        "do_not_translate": bool(value.get("do_not_translate", False)),
        "aliases": list(dict.fromkeys(_clean_text(item) for item in aliases if _clean_text(item))),
        "notes": _clean_text(value.get("notes"), 1000),
        "glossary_id": glossary_id,
        "scope": "global" if glossary_id == GLOBAL_GLOSSARY_ID else "project",
        "project": _clean_text(value.get("project"), 100),
        "enabled": bool(value.get("enabled", True)),
        "hotword_weight": hotword_weight,
    }


def _normalize_library(value: Any) -> dict[str, Any]:
    collections: list[dict[str, str]] = [normalize_collection({"kind": "global"})]
    if not isinstance(value, dict) or value.get("schema_version") != GLOSSARY_SCHEMA_VERSION:
        return {"schema_version": GLOSSARY_SCHEMA_VERSION, "collections": collections, "entries": []}
    entries_value: Any = value.get("entries", [])
    global_raw = next((c for c in value.get("collections", []) if isinstance(c, dict) and c.get("id") == GLOBAL_GLOSSARY_ID), {"kind":"global"})
    collections[0] = normalize_collection({**global_raw, "kind":"global"})
    raw_collections = value.get("collections", [])
    if isinstance(raw_collections, list):
        for item in raw_collections:
            if not isinstance(item, dict) or item.get("kind") == "global" or item.get("id") == GLOBAL_GLOSSARY_ID:
                continue
            try:
                collections.append(normalize_collection(item))
            except ValueError:
                continue

    known_ids = {item["id"] for item in collections}
    entries: list[dict[str, Any]] = []
    if not isinstance(entries_value, list):
        entries_value = []
    for raw in entries_value:
        if not isinstance(raw, dict):
            continue
        candidate = dict(raw)
        if not _clean_text(candidate.get("glossary_id"), 80):
            continue
        try:
            entry = normalize_entry(candidate)
        except ValueError:
            continue
        if entry["glossary_id"] not in known_ids:
            continue
        entries.append(entry)
    return {"schema_version": GLOSSARY_SCHEMA_VERSION, "collections": collections, "entries": entries, "candidates": [c for c in value.get("candidates", []) if isinstance(c, dict)]}


def load_glossary_library() -> dict[str, Any]:
    if not GLOSSARY_FILE.exists():
        return _normalize_library({"schema_version": GLOSSARY_SCHEMA_VERSION})
    try:
        value = json.loads(GLOSSARY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _normalize_library({"schema_version": GLOSSARY_SCHEMA_VERSION})
    return _normalize_library(value)


def load_glossary() -> list[dict[str, Any]]:
    return load_glossary_library()["entries"]


def load_glossary_collections() -> list[dict[str, str]]:
    return load_glossary_library()["collections"]


@_locked
def save_glossary_library(collections: list[dict[str, Any]], entries: list[dict[str, Any]], *, candidates: list[dict[str, Any]] | None = None, imported_candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    global_raw = next((c for c in collections if c.get("id") == GLOBAL_GLOSSARY_ID), {})
    normalized_collections = [normalize_collection({**global_raw, "kind": "global"})]
    collection_ids = {GLOBAL_GLOSSARY_ID}
    collection_names = {"未分配".casefold()}
    for raw in collections:
        item = normalize_collection(raw)
        if item["kind"] == "global":
            continue
        if item["id"] in collection_ids or item["name"].casefold() in collection_names:
            raise ValueError(f"项目词库重复：{item['name']}")
        collection_ids.add(item["id"])
        collection_names.add(item["name"].casefold())
        normalized_collections.append(item)

    normalized_entries = []
    seen = {}
    targets = {}
    for raw in entries:
        item = normalize_entry(raw)
        if item["glossary_id"] not in collection_ids:
            raise ValueError(f"术语所属词库不存在：{item['source']}")
        word = item["source"].casefold()
        target = item["target"]
        if target and word in targets and targets[word] != target:
            raise ValueError(f"术语译词冲突：{item['source']}（{targets[word]} / {target}）")
        if target:
            targets[word] = target
        key = (item["glossary_id"], word)
        if key in seen:
            seen[key]["enabled"] = seen[key]["enabled"] or item["enabled"]
            if target:
                seen[key]["target"] = target
            continue
        seen[key] = item
        normalized_entries.append(item)
    library = {"schema_version": GLOSSARY_SCHEMA_VERSION, "collections": normalized_collections, "entries": normalized_entries, "candidates": load_glossary_library().get("candidates", []) if candidates is None else candidates}
    # Merge imports while holding the library lock; background ASR additions and
    # terminal review states must survive a browser's older draft.
    by_word = {c["source"].strip().casefold(): c for c in library["candidates"]}
    candidate_ids = {c.get("id") for c in library["candidates"]}
    for raw in imported_candidates or []:
        source = _clean_text(raw.get("source"))
        status = raw.get("status", "pending")
        if not source or status not in {"pending", "approved", "ignored"}:
            raise ValueError("导入候选词或状态无效")
        existing = by_word.get(source.casefold())
        if existing:
            if existing.get("status") == "pending":
                existing["target"] = _clean_text(raw.get("target"))
            continue
        item = {**raw, "source": source, "target": _clean_text(raw.get("target")), "status": status}
        if not item.get("id") or item["id"] in candidate_ids:
            item["id"] = uuid.uuid4().hex
        candidate_ids.add(item["id"])
        library["candidates"].append(item)
        by_word[source.casefold()] = item
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(GLOSSARY_FILE, library)
    return library


def save_glossary(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    library = load_glossary_library()
    return save_glossary_library(library["collections"], entries)["entries"]


def glossary_collection_exists(glossary_id: str) -> bool:
    normalized_id = _clean_text(glossary_id, 80)
    return not normalized_id or any(item["id"] == normalized_id for item in load_glossary_collections())


def active_glossary(glossary_id: str = "", *, stage: str = "translation", collection_ids: list[str] | None = None) -> list[dict[str, Any]]:
    if stage not in INJECTION_STAGES:
        raise ValueError("未知词库注入环节")
    if stage == "calibration":
        return []
    library = load_glossary_library()
    permitted = {c["id"] for c in library["collections"] if stage in c.get("injection_permissions", [])}
    if collection_ids is not None:
        if not isinstance(collection_ids, list) or any(not isinstance(i, str) for i in collection_ids):
            raise ValueError("请选择有效词库")
        denied = set(collection_ids) - permitted
        if denied:
            raise ValueError("所选词库不存在或没有该环节的注入权限，请刷新选择")
        permitted &= set(collection_ids)
    unique = {}
    for entry in library["entries"]:
        if not entry["enabled"] or entry["glossary_id"] not in permitted:
            continue
        key = entry["source"].casefold()
        prior = unique.get(key)
        if stage == "translation" and prior and prior["target"] and entry["target"] and prior["target"] != entry["target"]:
            raise ValueError(f"术语译词冲突：{entry['source']}（{prior['target']} / {entry['target']}）")
        if prior is None or (not prior["target"] and entry["target"]):
            unique[key] = entry
    return list(unique.values())


def injection_preview(collection_ids: list[str] | None, temporary: list[dict[str, Any]]) -> dict[str, Any]:
    from .qwen_enhancement import normalize_qwen_hotwords
    entries = active_glossary(stage="asr", collection_ids=collection_ids)
    merged = {r["text"].casefold(): dict(r) for r in normalize_qwen_hotwords(temporary)}
    glossary_only = []
    for entry in entries:
        text = entry["source"]
        if not _qwen_hotword_is_valid(text):
            raise ValueError(f"词库热词超出长度限制：{text}")
        if text.casefold() not in merged:
            word = {"text": text, "weight": 5}
            merged[text.casefold()] = word
            glossary_only.append(word)
    # Use the exact recognizer contract, with explicit errors instead of truncation.
    hotwords = normalize_qwen_hotwords(list(merged.values()))
    return {"hotwords": hotwords, "glossary_hotwords": glossary_only, "entries": entries, "collection_ids": list(dict.fromkeys(collection_ids)) if collection_ids is not None else [c["id"] for c in load_glossary_library()["collections"] if "asr" in c.get("injection_permissions", [])]}


@_locked
def collect_candidates(rows: list[dict[str, Any]], source: str, project: str = "") -> dict[str, Any]:
    library = load_glossary_library()
    if not rows:
        return library
    candidates = library.get("candidates", [])
    known = {e["source"].casefold() for e in library["entries"]}
    by_word = {c["source"].casefold(): c for c in candidates}
    for raw in rows:
        word = _clean_text(raw.get("text") or raw.get("source"))
        if not word or word.casefold() in known:
            continue
        item = by_word.get(word.casefold())
        if item is None:
            item = {"id": uuid.uuid4().hex, "source": word, "target": _clean_text(raw.get("target")), "status":"pending", "sources":[], "created_at":datetime.now(timezone.utc).isoformat()}
            candidates.append(item); by_word[word.casefold()] = item
        origin = {"kind":_clean_text(source,80), "project":_clean_text(project,100)}
        if origin not in item["sources"]:
            item["sources"].append(origin)
    return save_glossary_library(library["collections"], library["entries"], candidates=candidates)


@_locked
def review_candidates(ids: list[str], action: str, glossary_id: str) -> dict[str, Any]:
    library = load_glossary_library()
    if action not in {"approve", "ignore"}:
        raise ValueError("候选操作无效")
    if action == "approve" and glossary_id not in {c["id"] for c in library["collections"]}:
        raise ValueError("目标词库不存在")
    entries = library["entries"]
    candidates = library.get("candidates", [])
    known = {(e["glossary_id"], e["source"].casefold()) for e in entries}
    for c in candidates:
        if c["id"] not in ids or c["status"] != "pending":
            continue
        if action == "approve":
            entries.append(normalize_entry({"source":c["source"], "target":c.get("target", ""), "glossary_id":glossary_id}))
            known.add((glossary_id, c["source"].casefold()))
        c["status"] = "approved" if action == "approve" else "ignored"
    return save_glossary_library(library["collections"], entries, candidates=candidates)


def glossary_prompt(entries: list[dict[str, Any]], *, include_target: bool = True) -> str:
    if not include_target:
        return "# ACTIVE_GLOSSARY\n单语处理不注入词库。"
    if not entries:
        return "# ACTIVE_GLOSSARY\n本次没有锁定术语。"
    rows = [{"source": item["source"], "target": item.get("target", "")} for item in entries]
    rule = "原文命中原词时采用对应译词；译词为空时正常翻译。按完整词语或术语匹配，不将其他词中的相同片段当作命中。"
    return f"# ACTIVE_GLOSSARY\n以下是人工锁定的术语。{rule}\n" + json.dumps(rows, ensure_ascii=False)


def _qwen_hotword_is_valid(text: str) -> bool:
    if not text:
        return False
    if any(ord(char) > 127 for char in text):
        return len(text) <= 15
    return len(text.split()) <= 7


def glossary_hotwords(entries: list[dict[str, Any]], *, weight: int = QWEN_HOTWORD_DEFAULT_WEIGHT, limit: int = QWEN_HOTWORD_MAX_COUNT) -> dict[str, int]:
    """Build ASR vocabulary from source forms; translations are excluded."""
    safe_weight = max(1, min(5, int(weight)))
    safe_limit = max(0, min(QWEN_HOTWORD_MAX_COUNT, int(limit)))
    if safe_limit == 0:
        return {}
    result: dict[str, int] = {}
    seen: set[str] = set()
    for entry in entries:
        candidates = [entry.get("source", ""), entry.get("standard_source", ""), *(entry.get("aliases", []) or [])]
        for candidate in candidates:
            text = str(candidate or "").strip()
            key = text.casefold()
            if not _qwen_hotword_is_valid(text) or key in seen:
                continue
            seen.add(key)
            try:
                entry_weight = int(entry.get("hotword_weight", safe_weight))
            except (TypeError, ValueError):
                entry_weight = safe_weight
            result[text] = max(1, min(5, entry_weight))
            if len(result) >= safe_limit:
                return result
    return result
