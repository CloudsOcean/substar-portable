from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .artifacts import atomic_write_text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
# A durable worker may bind one immutable prompt snapshot through its private
# process environment.  Normal application imports keep the packaged prompt
# root; only the isolated algorithm child receives this override.
PROMPT_ROOT = Path(
    os.environ.get("SUBSTAR_PROMPT_ROOT", str(PROJECT_ROOT / "prompts"))
).resolve()
REGISTRY_PATH = PROMPT_ROOT / "production" / "registry.json"
HAN_RE = re.compile(r"[\u3400-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")
KANA_RE = re.compile(r"[\u3040-\u30ff]")
HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
PROMPT_COMPONENT_MAX_CHARACTERS = 1_000_000
_PROMPT_WRITE_LOCK = threading.RLock()


class PromptRegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class RenderedPrompt:
    key: str
    variant: str
    version: str
    text: str
    files: tuple[str, ...]
    sha256: str

    def metadata(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "variant": self.variant,
            "version": self.version,
            "files": list(self.files),
            "sha256": self.sha256,
        }


def source_language_for_text(text: str) -> str:
    value = text or ""
    counts = {
        "zh-CN": len(HAN_RE.findall(value)),
        "en": len(LATIN_RE.findall(value)),
        "ja": len(KANA_RE.findall(value)),
        "ko": len(HANGUL_RE.findall(value)),
    }
    # Kana/Hangul are decisive; Japanese text commonly contains Han glyphs.
    if counts["ja"]:
        counts["ja"] += counts["zh-CN"]
        counts["zh-CN"] = 0
    if counts["zh-CN"] and counts["en"]:
        return "mixed"
    return max(counts, key=counts.get) if any(counts.values()) else "en"


def source_language_for_units(units: Iterable[Any]) -> str:
    return source_language_for_text(
        " ".join(str(getattr(unit, "text", "") or "") for unit in units)
    )


def normalize_source_language(value: str | None, units: Iterable[Any] | None = None) -> str:
    """Resolve the user-facing source-language choice used by segmentation.

    ``Auto`` is the only value that uses content inference.  ``mixed`` is an
    explicit policy choice and must not be collapsed into either Chinese or
    English merely because one happens to be dominant in a particular chunk.
    """
    raw = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "zh": "zh-CN",
        "zh-cn": "zh-CN",
        "chinese": "zh-CN",
        "en": "en",
        "english": "en",
        "ja": "ja",
        "japanese": "ja",
        "ko": "ko",
        "korean": "ko",
        "mixed": "mixed",
        "mixed-zh-en": "mixed",
        "zh-en": "mixed",
        "en-zh": "mixed",
    }
    if raw in {"", "auto", "automatic"}:
        return source_language_for_units(units or []) if units is not None else "Auto"
    return aliases.get(raw, str(value).strip() or "Auto")


def opposite_language(source_language: str) -> str:
    return "en" if str(source_language).lower().startswith("zh") else "zh-CN"


def translation_variant(source_language: str, target_language: str | None = None) -> str:
    aliases = {"zh": "zh", "zh-cn": "zh", "en": "en", "ja": "ja", "ko": "ko"}
    source = aliases.get(str(source_language or "en").lower())
    target = aliases.get(str(target_language or opposite_language(source_language)).lower())
    if source and target and source != target:
        return f"{source}_to_{target}"
    return "generic"


def _registry() -> dict[str, Any]:
    try:
        value = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromptRegistryError(f"无法读取生产提示词清单：{REGISTRY_PATH}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != "substar.prompt-registry.v1":
        raise PromptRegistryError("生产提示词清单版本不匹配")
    return value


def render_prompt(key: str, *, variant: str = "default") -> RenderedPrompt:
    registry = _registry()
    entries = registry.get("prompts", {})
    entry = entries.get(key) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        raise PromptRegistryError(f"未注册提示词：{key}")
    variants = entry.get("variants", {})
    files = variants.get(variant) if isinstance(variants, dict) else None
    if files is None and variant != "default":
        files = variants.get("default") if isinstance(variants, dict) else None
    if files is None and variant != "generic":
        files = variants.get("generic") if isinstance(variants, dict) else None
    if not isinstance(files, list) or not files:
        raise PromptRegistryError(f"提示词 {key} 缺少变体：{variant}")

    resolved: list[str] = []
    parts: list[str] = []
    production_root = (PROMPT_ROOT / "production").resolve()
    for relative in files:
        path = (production_root / str(relative)).resolve()
        if path != production_root and production_root not in path.parents:
            raise PromptRegistryError(f"提示词路径越界：{relative}")
        try:
            parts.append(path.read_text(encoding="utf-8").strip())
        except OSError as exc:
            raise PromptRegistryError(f"无法读取提示词组件：{relative}") from exc
        resolved.append(path.relative_to(PROMPT_ROOT).as_posix())
    text = "\n\n".join(part for part in parts if part).strip()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return RenderedPrompt(
        key=key,
        variant=variant,
        version=str(entry.get("version") or "1"),
        text=text,
        files=tuple(resolved),
        sha256=digest,
    )


def _registered_component_paths(registry: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    entries = registry.get("prompts", {})
    if not isinstance(entries, dict):
        return paths
    for entry in entries.values():
        variants = entry.get("variants", {}) if isinstance(entry, dict) else {}
        if not isinstance(variants, dict):
            continue
        for files in variants.values():
            if isinstance(files, list):
                paths.update(str(item).replace("\\", "/") for item in files)
    return paths


def _component_kind(relative: str) -> str:
    if relative.startswith("cases/") or relative.endswith(".constructed.md"):
        return "case"
    if relative.endswith("/rules.md") or "/target/" in relative:
        return "rules"
    return "template"


def _component_title(relative: str, text: str) -> str:
    for line in text.splitlines():
        value = line.strip().lstrip("#").strip()
        if line.strip().startswith("#") and value:
            return value
    return Path(relative).stem.replace(".constructed", "").replace("_", " ")


def prompt_catalog() -> dict[str, Any]:
    """Project editable components and the immutable executable route registry."""
    registry = _registry()
    entries = registry.get("prompts", {})
    categories = registry.get("categories", [])
    if not isinstance(entries, dict) or not isinstance(categories, list):
        raise PromptRegistryError("生产提示词清单结构无效")
    usage: dict[str, list[dict[str, str]]] = {}
    families: list[dict[str, Any]] = []
    variant_count = 0
    for key, raw in entries.items():
        if not isinstance(raw, dict) or not isinstance(raw.get("variants"), dict):
            raise PromptRegistryError(f"提示词清单项无效：{key}")
        variants = []
        for variant, raw_files in raw["variants"].items():
            if not isinstance(raw_files, list) or not raw_files:
                raise PromptRegistryError(f"提示词 {key} 路由无效：{variant}")
            files = [str(item).replace("\\", "/") for item in raw_files]
            variants.append({"id": str(variant), "files": files})
            variant_count += 1
            for relative in files:
                usage.setdefault(relative, []).append({"family": str(key), "variant": str(variant)})
        families.append({
            "id": str(key),
            "title": str(raw.get("title") or key),
            "category": str(raw.get("category") or "other"),
            "description": str(raw.get("description") or ""),
            "version": str(raw.get("version") or "1"),
            "variants": variants,
        })
    production_root = (PROMPT_ROOT / "production").resolve()
    components = []
    for relative in sorted(usage):
        path = (production_root / relative).resolve()
        if production_root not in path.parents or not path.is_file():
            raise PromptRegistryError(f"注册提示词组件不存在：{relative}")
        content = path.read_text(encoding="utf-8")
        components.append({
            "path": relative,
            "name": path.stem.replace(".constructed", ""),
            "title": _component_title(relative, content),
            "kind": _component_kind(relative),
            "characters": len(content),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "used_by": usage[relative],
        })
    cases = sum(1 for item in components if item["kind"] == "case")
    return {
        "schema_version": "substar.prompt-catalog.v1",
        "routes_read_only": True,
        "components_editable": True,
        "categories": categories,
        "families": families,
        "components": components,
        "stats": {
            "families": len(families),
            "variants": variant_count,
            "components": len(components),
            "core_components": len(components) - cases,
            "cases": cases,
        },
    }


def prompt_component(relative: str) -> dict[str, Any]:
    """Read one registered production component; unregistered assets stay hidden."""
    normalized = str(relative or "").strip().replace("\\", "/")
    registry = _registry()
    if normalized not in _registered_component_paths(registry):
        raise PromptRegistryError(f"未注册生产提示词组件：{normalized}")
    production_root = (PROMPT_ROOT / "production").resolve()
    path = (production_root / normalized).resolve()
    if production_root not in path.parents or not path.is_file():
        raise PromptRegistryError(f"提示词组件不存在：{normalized}")
    text = path.read_text(encoding="utf-8")
    return {
        "schema_version": "substar.prompt-component.v1",
        "path": normalized,
        "title": _component_title(normalized, text),
        "kind": _component_kind(normalized),
        "characters": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
    }


def update_prompt_component(
    relative: str,
    text: str,
    *,
    expected_sha256: str,
) -> dict[str, Any]:
    """Atomically update one registered component using optimistic concurrency."""
    if not isinstance(text, str) or not text.strip():
        raise PromptRegistryError("提示词正文不能为空")
    if len(text) > PROMPT_COMPONENT_MAX_CHARACTERS:
        raise PromptRegistryError(
            f"提示词正文不能超过 {PROMPT_COMPONENT_MAX_CHARACTERS} 个字符"
        )
    expected = str(expected_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise PromptRegistryError("提示词版本指纹无效，请重新载入后再保存")

    normalized = str(relative or "").strip().replace("\\", "/")
    with _PROMPT_WRITE_LOCK:
        current = prompt_component(normalized)
        if current["sha256"] != expected:
            raise PromptRegistryError("提示词已在其他位置修改，请重新载入后再保存")
        production_root = (PROMPT_ROOT / "production").resolve()
        path = (production_root / normalized).resolve()
        if production_root not in path.parents or not path.is_file():
            raise PromptRegistryError(f"提示词组件不存在：{normalized}")
        atomic_write_text(path, text, encoding="utf-8")
        return prompt_component(normalized)
