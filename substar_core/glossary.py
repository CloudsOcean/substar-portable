from __future__ import annotations

import json
import re
import uuid
from typing import Any

from .artifacts import atomic_write_json
from .config import APP_DATA_DIR, GLOSSARY_FILE

ALLOWED_TYPES = {"person", "organization", "place", "program", "product", "technical", "other"}
GLOBAL_GLOSSARY_ID = "global"
GLOSSARY_SCHEMA_VERSION = "substar.glossary-library.v2"
QWEN_HOTWORD_DEFAULT_WEIGHT = 4
QWEN_HOTWORD_MAX_COUNT = 2000


def _clean_text(value: Any, limit: int = 300) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or "")).strip()[:limit]


def normalize_collection(value: dict[str, Any]) -> dict[str, str]:
    kind = "global" if value.get("kind") == "global" else "project"
    if kind == "global":
        return {"id": GLOBAL_GLOSSARY_ID, "name": "全局词库", "kind": "global"}
    name = _clean_text(value.get("name"), 100)
    if not name:
        raise ValueError("项目词库名称不能为空")
    collection_id = _clean_text(value.get("id"), 80) or f"glossary_{uuid.uuid4().hex}"
    if collection_id == GLOBAL_GLOSSARY_ID:
        raise ValueError("项目词库 ID 不能使用 global")
    return {"id": collection_id, "name": name, "kind": "project"}


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
    return {"schema_version": GLOSSARY_SCHEMA_VERSION, "collections": collections, "entries": entries}


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


def save_glossary_library(collections: list[dict[str, Any]], entries: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_collections = [normalize_collection({"kind": "global"})]
    collection_ids = {GLOBAL_GLOSSARY_ID}
    collection_names = {"全局词库".casefold()}
    for raw in collections:
        item = normalize_collection(raw)
        if item["kind"] == "global":
            continue
        if item["id"] in collection_ids or item["name"].casefold() in collection_names:
            raise ValueError(f"项目词库重复：{item['name']}")
        collection_ids.add(item["id"])
        collection_names.add(item["name"].casefold())
        normalized_collections.append(item)

    normalized_entries = [normalize_entry(item) for item in entries]
    seen: set[tuple[str, str]] = set()
    for item in normalized_entries:
        if item["glossary_id"] not in collection_ids:
            raise ValueError(f"术语所属词库不存在：{item['source']}")
        key = (item["glossary_id"], item["source"] if item["case_sensitive"] else item["source"].casefold())
        if key in seen:
            raise ValueError(f"术语重复：{item['source']}")
        seen.add(key)
    library = {"schema_version": GLOSSARY_SCHEMA_VERSION, "collections": normalized_collections, "entries": normalized_entries}
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(GLOSSARY_FILE, library)
    return library


def save_glossary(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    library = load_glossary_library()
    return save_glossary_library(library["collections"], entries)["entries"]


def glossary_collection_exists(glossary_id: str) -> bool:
    normalized_id = _clean_text(glossary_id, 80)
    return not normalized_id or any(item["id"] == normalized_id for item in load_glossary_collections())


def active_glossary(glossary_id: str = "") -> list[dict[str, Any]]:
    selected_id = _clean_text(glossary_id, 80)
    active = [item for item in load_glossary() if item["enabled"]]
    global_entries = [item for item in active if item["glossary_id"] == GLOBAL_GLOSSARY_ID]
    if not selected_id or selected_id == GLOBAL_GLOSSARY_ID:
        return global_entries
    project_entries = [item for item in active if item["glossary_id"] == selected_id]
    overrides = {item["source"] if item["case_sensitive"] else item["source"].casefold() for item in project_entries}
    return [item for item in global_entries if (item["source"] if item["case_sensitive"] else item["source"].casefold()) not in overrides] + project_entries


def glossary_prompt(entries: list[dict[str, Any]], *, include_target: bool = True) -> str:
    if not entries:
        return "# ACTIVE_GLOSSARY\n本次没有锁定术语。"
    rows = [{"source": item["source"], "standard_source": item["standard_source"], "aliases": item["aliases"]} for item in entries]
    if include_target:
        for row, item in zip(rows, entries):
            row.update({"target": item["target"], "do_not_translate": item["do_not_translate"]})
    rule = (
        "命中热词时采用指定译文；do_not_translate=true 时保留原词。"
        if include_target
        else "这是单语处理，只校正热词写法，不读取译文、不改变语言。"
    )
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
