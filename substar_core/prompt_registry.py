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
    mode: str | None = None

    def metadata(self) -> dict[str, Any]:
        value = {
            "key": self.key,
            "variant": self.variant,
            "version": self.version,
            "files": list(self.files),
            "sha256": self.sha256,
        }
        if self.mode:
            value["mode"] = self.mode
        return value


def source_language_analysis(
    text: str, language_ratio_threshold_percent: int | float = 20
) -> dict[str, Any]:
    """Resolve one project language from all language-bearing characters.

    The threshold is the combined share of every language except the most-used
    language. Japanese keeps the established rule that Han characters belong
    to Japanese when Kana is present, because Kanji cannot be distinguished
    from Chinese by Unicode script alone.
    """
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
    threshold = max(0.0, min(100.0, float(language_ratio_threshold_percent)))
    total = sum(counts.values())
    primary = max(counts, key=counts.get) if total else "en"
    primary_count = counts[primary] if total else 0
    other_count = total - primary_count
    other_ratio = (other_count * 100.0 / total) if total else 0.0
    resolved = primary if other_ratio <= threshold else "mixed"
    return {
        "resolved_language": resolved,
        "primary_language": primary,
        "language_character_counts": counts,
        "language_character_count": total,
        "other_language_character_count": other_count,
        "other_language_ratio_percent": round(other_ratio, 4),
        "language_ratio_threshold_percent": threshold,
    }


def source_language_for_text(
    text: str, language_ratio_threshold_percent: int | float = 20
) -> str:
    return str(
        source_language_analysis(text, language_ratio_threshold_percent)[
            "resolved_language"
        ]
    )


def source_language_for_units(
    units: Iterable[Any], language_ratio_threshold_percent: int | float = 20
) -> str:
    return source_language_for_text(
        " ".join(str(getattr(unit, "text", "") or "") for unit in units),
        language_ratio_threshold_percent,
    )


def normalize_source_language(
    value: str | None,
    units: Iterable[Any] | None = None,
    *,
    language_ratio_threshold_percent: int | float = 20,
) -> str:
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
        return (
            source_language_for_units(
                units or [], language_ratio_threshold_percent
            )
            if units is not None
            else "Auto"
        )
    return aliases.get(raw, str(value).strip() or "Auto")


def opposite_language(source_language: str) -> str:
    return "en" if str(source_language).lower().startswith("zh") else "zh-CN"


def calibration_variant(source_language: str) -> str:
    """Return the dedicated calibration prompt route for one source track."""
    normalized = normalize_source_language(source_language)
    routes = {
        "zh-CN": "zh",
        "en": "en",
        "ja": "ja",
        "ko": "ko",
        "mixed": "mixed",
    }
    if normalized not in routes:
        raise PromptRegistryError(f"校准提示词不支持原文语言：{source_language}")
    return routes[normalized]


def translation_variant(source_language: str, target_language: str | None = None) -> str:
    aliases = {
        "zh": "zh", "zh-cn": "zh", "en": "en", "ja": "ja", "ko": "ko",
        "mixed": "mixed",
    }
    source = aliases.get(str(source_language or "en").lower())
    target = aliases.get(str(target_language or opposite_language(source_language)).lower())
    if source and target and target != "mixed" and source != target:
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


def render_prompt(
    key: str, *, variant: str = "default", mode: str | None = None
) -> RenderedPrompt:
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

    shared = entry.get("shared", [])
    modes = entry.get("modes", {})
    if not isinstance(shared, list):
        raise PromptRegistryError(f"提示词 {key} 的公共组件无效")
    selected_mode = str(mode or entry.get("default_mode") or "").strip() or None
    mode_files: list[str] = []
    if modes:
        if not isinstance(modes, dict) or selected_mode not in modes:
            raise PromptRegistryError(
                f"提示词 {key} 缺少模式：{selected_mode or '<未指定>'}"
            )
        raw_mode_files = modes[selected_mode]
        if not isinstance(raw_mode_files, list) or not raw_mode_files:
            raise PromptRegistryError(f"提示词 {key} 的模式组件无效：{selected_mode}")
        mode_files = [str(item) for item in raw_mode_files]
    mode_variants = entry.get("mode_variants", {})
    mode_variant_files: list[str] = []
    if isinstance(mode_variants, dict) and selected_mode:
        by_variant = mode_variants.get(selected_mode, {})
        if isinstance(by_variant, dict):
            raw_mode_variant_files = by_variant.get(variant, [])
            if isinstance(raw_mode_variant_files, list):
                mode_variant_files = [str(item) for item in raw_mode_variant_files]
    files = [
        *map(str, shared), *mode_files, *map(str, files), *mode_variant_files,
    ]

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
        mode=selected_mode,
    )


def _registered_component_paths(registry: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    entries = registry.get("prompts", {})
    if not isinstance(entries, dict):
        return paths
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        paths.update(str(item).replace("\\", "/") for item in entry.get("shared", []))
        modes = entry.get("modes", {})
        if isinstance(modes, dict):
            for files in modes.values():
                if isinstance(files, list):
                    paths.update(str(item).replace("\\", "/") for item in files)
        mode_variants = entry.get("mode_variants", {})
        if isinstance(mode_variants, dict):
            for variants in mode_variants.values():
                if not isinstance(variants, dict):
                    continue
                for files in variants.values():
                    if isinstance(files, list):
                        paths.update(str(item).replace("\\", "/") for item in files)
        variants = entry.get("variants", {})
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
        shared = [str(item).replace("\\", "/") for item in raw.get("shared", [])]
        modes = {
            str(mode): [str(item).replace("\\", "/") for item in files]
            for mode, files in raw.get("modes", {}).items()
            if isinstance(files, list)
        } if isinstance(raw.get("modes", {}), dict) else {}
        mode_variants = raw.get("mode_variants", {})
        for relative in shared:
            usage.setdefault(relative, []).append({"family": str(key), "variant": "shared"})
        for mode, files in modes.items():
            for relative in files:
                usage.setdefault(relative, []).append({"family": str(key), "variant": f"mode:{mode}"})
        if isinstance(mode_variants, dict):
            for mode, by_variant in mode_variants.items():
                if not isinstance(by_variant, dict):
                    continue
                for mode_variant, files in by_variant.items():
                    if not isinstance(files, list):
                        continue
                    for relative in files:
                        normalized = str(relative).replace("\\", "/")
                        usage.setdefault(normalized, []).append({
                            "family": str(key),
                            "variant": f"mode:{mode}/{mode_variant}",
                        })
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
            "shared": shared,
            "modes": modes,
            "mode_variants": mode_variants,
            "default_mode": raw.get("default_mode"),
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
