from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Mapping

from substar_core.artifacts import atomic_write_json
from substar_core.model_providers import MODEL_PROVIDER_IDS
TASK_INFO_SCHEMA = "substar.task-info.v2"
TASK_INFO_FILENAME = "task_info.json"
SOURCE_LANGUAGES = {"Auto", "mixed", "zh", "zh-CN", "en", "ja", "ko"}
TARGET_LANGUAGES = {"auto_opposite", "zh-CN", "en", "ja", "ko"}


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _validate(value: Mapping[str, Any], project_id: str) -> dict[str, Any]:
    display_name = str(value.get("display_name", "")).strip()
    if not display_name:
        raise ValueError("任务名称不能为空")
    if len(display_name) > 120 or any(ord(char) < 32 for char in display_name) or re.search(r'[\\/:*?"<>|]', display_name):
        raise ValueError("任务名称包含不允许的字符")
    language = str(value.get("language", "Auto"))
    target = str(value.get("target_language_mode", "auto_opposite"))
    if language not in SOURCE_LANGUAGES:
        raise ValueError("原文语言无效")
    if target not in TARGET_LANGUAGES:
        raise ValueError("目标语言无效")
    source_limit = int(value.get("source_hard_limit", 25))
    target_limit = int(value.get("target_hard_limit", 25))
    if not 1 <= source_limit <= 500 or not 1 <= target_limit <= 500:
        raise ValueError("行长必须是 1–500 的整数")
    llm_provider_id = str(value.get("llm_provider_id") or "inherit").strip()
    if llm_provider_id != "inherit" and llm_provider_id not in MODEL_PROVIDER_IDS:
        raise ValueError("本任务使用的模型服务商无效")
    return {
        "schema_version": TASK_INFO_SCHEMA,
        "project_id": project_id,
        "display_name": display_name,
        "language": language,
        "target_language_mode": target,
        "glossary_id": str(value.get("glossary_id") or "").strip()[:80],
        "llm_provider_id": llm_provider_id,
        "source_hard_limit": source_limit,
        "target_hard_limit": target_limit,
        "updated_at": str(value.get("updated_at") or datetime.now(timezone.utc).isoformat()),
    }


def load_task_info(job_dir: Path, project_id: str) -> dict[str, Any]:
    """Load the required v2 project metadata authority."""
    path = job_dir / TASK_INFO_FILENAME
    current = _read_mapping(path)
    if not current:
        raise ValueError("项目缺少 v2 任务信息；旧项目不受支持")
    if current.get("schema_version") != TASK_INFO_SCHEMA:
        raise ValueError("任务信息版本不受支持")
    return _validate(current, project_id)


def save_task_info(job_dir: Path, project_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _validate({**value, "updated_at": datetime.now(timezone.utc).isoformat()}, project_id)
    atomic_write_json(job_dir / TASK_INFO_FILENAME, normalized)
    return normalized


def task_info_settings(info: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt canonical track settings for existing downstream policy consumers."""
    source = str(info["language"])
    target = str(info["target_language_mode"])
    result = {
        "language": source,
        "target_language_mode": target,
        "glossary_id": str(info.get("glossary_id") or ""),
        "llm_provider_id": str(info.get("llm_provider_id") or "inherit"),
        "source_hard_limit": int(info["source_hard_limit"]),
        "target_hard_limit": int(info["target_hard_limit"]),
    }
    source_key = {
        "en": "english_hard_limit", "zh": "chinese_hard_limit", "zh-CN": "chinese_hard_limit",
        "ja": "japanese_hard_limit", "ko": "korean_hard_limit", "mixed": "mixed_hard_limit",
    }.get(source)
    target_key = {
        "en": "english_hard_limit", "zh-CN": "chinese_hard_limit",
        "ja": "japanese_hard_limit", "ko": "korean_hard_limit",
    }.get(target)
    if source_key:
        result[source_key] = int(info["source_hard_limit"])
    if target_key:
        result[target_key] = int(info["target_hard_limit"])
    return result
