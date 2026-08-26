from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from .artifacts import atomic_write_json
from .config import APP_DATA_DIR, GLOSSARY_FILE


ALLOWED_TYPES = {
    "person",
    "organization",
    "place",
    "program",
    "product",
    "technical",
    "other",
}
ALLOWED_SCOPES = {"global", "project"}
QWEN_HOTWORD_DEFAULT_WEIGHT = 4
QWEN_HOTWORD_MAX_COUNT = 2000


def _clean_text(value: Any, limit: int = 300) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or "")).strip()[:limit]


def normalize_entry(value: dict[str, Any]) -> dict[str, Any]:
    source = _clean_text(value.get("source"))
    if not source:
        raise ValueError("术语原词不能为空")
    entry_type = _clean_text(value.get("type", "other"), 30)
    scope = _clean_text(value.get("scope", "global"), 30)
    aliases = value.get("aliases", [])
    if isinstance(aliases, str):
        aliases = re.split(r"[,，;\n]+", aliases)
    if not isinstance(aliases, list):
        aliases = []
    try:
        hotword_weight = max(1, min(5, int(value.get("hotword_weight", 4))))
    except (TypeError, ValueError):
        hotword_weight = 4
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
        "scope": scope if scope in ALLOWED_SCOPES else "global",
        "project": _clean_text(value.get("project"), 100),
        "enabled": bool(value.get("enabled", True)),
        "hotword_weight": hotword_weight,
    }


def load_glossary() -> list[dict[str, Any]]:
    if not GLOSSARY_FILE.exists():
        return []
    try:
        value = json.loads(GLOSSARY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            entries.append(normalize_entry(item))
        except ValueError:
            continue
    return entries


def save_glossary(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [normalize_entry(item) for item in entries]
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in normalized:
        key = (
            item["scope"],
            item["project"].casefold(),
            item["source"] if item["case_sensitive"] else item["source"].casefold(),
        )
        if key in seen:
            raise ValueError(f"术语重复：{item['source']}")
        seen.add(key)
        unique.append(item)
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(GLOSSARY_FILE, unique)
    return unique


def active_glossary(project_name: str = "") -> list[dict[str, Any]]:
    project = project_name.casefold().strip()
    return [
        item
        for item in load_glossary()
        if item["enabled"]
        and (
            item["scope"] == "global"
            or (item["scope"] == "project" and item["project"].casefold() == project)
        )
    ]


def glossary_prompt(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "# ACTIVE_GLOSSARY\n本次没有锁定术语。"
    rows = [
        {
            "source": item["source"],
            "standard_source": item["standard_source"],
            "target": item["target"],
            "do_not_translate": item["do_not_translate"],
            "aliases": item["aliases"],
            "type": item["type"],
            "notes": item["notes"],
        }
        for item in entries
    ]
    return (
        "# ACTIVE_GLOSSARY\n"
        "以下是人工锁定的术语。命中原词或别名时必须采用 standard_source/target；"
        "do_not_translate=true 时目标行保留 standard_source 或 source。不得擅自创造冲突译名。\n"
        + json.dumps(rows, ensure_ascii=False)
    )


def _qwen_hotword_is_valid(text: str) -> bool:
    """Apply the Qwen-Audio inline hotword length limits."""

    if not text:
        return False
    if any(ord(char) > 127 for char in text):
        return len(text) <= 15
    return len(text.split()) <= 7


def glossary_hotwords(
    entries: list[dict[str, Any]],
    *,
    weight: int = QWEN_HOTWORD_DEFAULT_WEIGHT,
    limit: int = QWEN_HOTWORD_MAX_COUNT,
) -> dict[str, int]:
    """Build Qwen-Audio's inline vocabulary from active glossary entries.

    Recognition should be biased toward the spoken/source forms.  Target
    translations are deliberately excluded because they are not necessarily
    present in the audio and can make ASR over-correct the transcript.
    """

    safe_weight = max(1, min(5, int(weight)))
    safe_limit = max(0, min(QWEN_HOTWORD_MAX_COUNT, int(limit)))
    if safe_limit == 0:
        return {}
    result: dict[str, int] = {}
    seen: set[str] = set()
    for entry in entries:
        candidates = [
            entry.get("source", ""),
            entry.get("standard_source", ""),
            *(entry.get("aliases", []) or []),
        ]
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

