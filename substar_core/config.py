from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .artifacts import atomic_write_json
from .credential_store import (
    ALIGN_DEEPSEEK,
    ASR_QWEN,
    MODEL_PROVIDER_PREFIX,
    model_provider_credential_ref,
    resolve_model_provider_credential,
    clean_credential,
    load_store,
    write_envelope,
)
from .recognition.registry import DEFAULT_RECOGNITION_PROFILE, get_recognition_profile
from .model_providers import (
    MODEL_PROVIDER_IDS,
    canonical_provider_id,
    infer_model_provider,
    normalize_provider_profiles,
)
from .reasoning_capabilities import reasoning_capabilities


SOURCE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SOURCE_ROOT
INSTALL_ROOT = SOURCE_ROOT
PROJECT_MODELS_ROOT = (INSTALL_ROOT / "models").resolve()
DATA_ROOT = Path(
    os.environ.get("SUBSTAR_DATA_ROOT", INSTALL_ROOT / "data")
).resolve()
PROJECTS_ROOT = (DATA_ROOT / "projects-v2").resolve()
PRIMARY_APP_DATA_DIR = (
    Path(os.environ.get("LOCALAPPDATA", PROJECT_ROOT)) / "SubstarWorkbench"
)
# Prefer keeping portable settings and credentials beside the application.
PORTABLE_APP_DATA_DIR = (INSTALL_ROOT / "data" / ".substar-workbench").resolve()
FALLBACK_APP_DATA_DIR = DATA_ROOT / ".substar-workbench"


def _directory_is_writable(path: Path) -> bool:
    """Check whether the running app can persist configuration in ``path``."""

    probe: Path | None = None
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-test-{os.getpid()}"
        with probe.open("x", encoding="ascii"):
            pass
        probe.unlink()
        return True
    except OSError:
        if probe is not None:
            try:
                probe.unlink()
            except OSError:
                pass
        return False


# Prefer the software directory. Installed builds under a protected location
# fall back to the per-user directory, and finally to the configured data root.
if _directory_is_writable(PORTABLE_APP_DATA_DIR):
    APP_DATA_DIR = PORTABLE_APP_DATA_DIR
elif _directory_is_writable(PRIMARY_APP_DATA_DIR):
    APP_DATA_DIR = PRIMARY_APP_DATA_DIR
else:
    APP_DATA_DIR = FALLBACK_APP_DATA_DIR
USING_PORTABLE_APP_DATA = APP_DATA_DIR == PORTABLE_APP_DATA_DIR
USING_FALLBACK_APP_DATA = APP_DATA_DIR == FALLBACK_APP_DATA_DIR
SETTINGS_FILE = APP_DATA_DIR / "settings.json"
GLOSSARY_FILE = APP_DATA_DIR / "glossary.json"
CREDENTIALS_FILE = APP_DATA_DIR / "credentials.enc"
PRIMARY_SETTINGS_FILE = PRIMARY_APP_DATA_DIR / "settings.json"
PRIMARY_CREDENTIALS_FILE = PRIMARY_APP_DATA_DIR / "credentials.enc"

DEFAULTS: dict[str, Any] = {
    "workflow_mode": "subtitle_creation",
    "segmentation_enabled": True,
    "translation_enabled": False,
    "calibration_enabled": False,
    "appearance_mode": "dark",
    "accent_color": "purple",
    "surface_style": "standard",
    "ui_density": "comfortable",
    "motion_level": "full",
    "font_scale": "standard",
    "project_name": "默认项目",
    "recognition_profile_id": DEFAULT_RECOGNITION_PROFILE,
    "transcript_source": "qwen_cloud",
    "language": "Auto",
    "context": "",
    "qwen_cloud_region": "beijing",
    "qwen_cloud_base_url": "",
    "qwen_cloud_model": "qwen-audio-3.0-asr-flash-filetrans",
    "qwen_cloud_request_timeout_seconds": 120,
    "qwen_cloud_task_timeout_seconds": 7200,
    "qwen_cloud_poll_interval_seconds": 3.0,
    "qwen_cloud_temporary_upload": True,
    "alignment_api_provider": "openai_chat",
    "alignment_api_base_url": "https://api.deepseek.com",
    "alignment_api_model": "deepseek-v4-flash",
    "alignment_api_auth_mode": "bearer",
    "alignment_api_timeout_seconds": 120,
    "translation_api_provider": "openai_chat",
    "translation_api_base_url": "https://api.deepseek.com",
    "translation_api_model": "deepseek-v4-flash",
    "active_model_provider": "deepseek",
    "model_provider_profiles": {},
    "model_reasoning_capabilities": {},
    "stage_segmentation_model": "deepseek-v4-flash",
    "stage_translation_model": "deepseek-v4-flash",
    "stage_translation_repair_model": "deepseek-v4-flash",
    "stage_calibration_model": "deepseek-v4-flash",
    "calibration_protocol_version": 2,
    "stage_audit_repair_model": "deepseek-v4-flash",
    "stage_segmentation_thinking_mode": "enabled",
    "stage_segmentation_reasoning_effort": "low",
    "stage_segmentation_max_tokens": 131072,
    "stage_segmentation_temperature": 0.0,
    "stage_segmentation_repair_model": "deepseek-v4-flash",
    "stage_segmentation_repair_thinking_mode": "disabled",
    "stage_segmentation_repair_reasoning_effort": "low",
    "stage_segmentation_repair_max_tokens": 65536,
    "stage_segmentation_repair_temperature": 0.0,
    "stage_translation_thinking_mode": "enabled",
    "stage_translation_reasoning_effort": "low",
    "stage_translation_max_tokens": 131072,
    "stage_translation_temperature": 0.0,
    "stage_translation_repair_thinking_mode": "disabled",
    "stage_translation_repair_reasoning_effort": "low",
    "stage_translation_repair_max_tokens": 65536,
    "stage_translation_repair_temperature": 0.0,
    # Normal stages default to thinking at Low. Repair stages prefer
    # non-thinking and are promoted to thinking at Low only when required by
    # the selected model.
    "stage_calibration_thinking_mode": "enabled",
    "stage_calibration_reasoning_effort": "low",
    "stage_calibration_max_tokens": 65536,
    "stage_calibration_temperature": 0.0,
    "stage_audit_repair_thinking_mode": "disabled",
    "stage_audit_repair_reasoning_effort": "low",
    "stage_audit_repair_max_tokens": 65536,
    "stage_audit_repair_temperature": 0.0,
    # The browser UI is served by one foreground backend.  This is the port
    # used by the next launcher run; changing it never attempts a hot restart.
    "startup_port": 8769,
    "translation_api_auth_mode": "bearer",
    "translation_api_timeout_seconds": 300,
    "translation_thinking_mode": "enabled",
    "translation_reasoning_effort": "low",
    "translation_style": "corporate_broadcast",
    "target_language_mode": "auto_opposite",
    "display_order": "source_target",
    "top_raised_punctuation": "preserve",
    "top_baseline_punctuation": "preserve",
    "bottom_raised_punctuation": "preserve",
    "bottom_baseline_punctuation": "preserve",
    "english_hard_limit": 55,
    "english_count_spaces": True,
    "english_count_punctuation": True,
    "chinese_hard_limit": 25,
    "mixed_hard_limit": 25,
    "japanese_hard_limit": 25,
    "korean_hard_limit": 32,
    "target_visual_width_limit": 48,
    "minimum_cue_duration_ms": 400,
    "maximum_cue_duration_ms": 7000,
    "tail_padding_ms": 120,
    "snap_threshold_ms": 500,
    "maximum_cps_latin": 20.0,
    "maximum_cps_cjk": 12.0,
    "audio_denoise_mode": "off",
    "text_cleanup_mode": "mark_conservative",
    "segmentation_strategy": "semantic",
    "sentence_boundary_policy": "unpunctuated",
    "split_workflow_mode": "one_step",
    "translation_workflow_mode": "one_step",
    "segmentation_chunk_seconds": 90,
    "segmentation_overlap_seconds": 40,
    "segmentation_batch_groups": 4,
    "segmentation_candidates": 1,
    "translation_workers": 64,
    "runtime_worker_concurrency": 4,
    "runtime_cloud_concurrency": 4,
    "runtime_media_concurrency": 2,
    "runtime_gpu_concurrency": 1,
    "runtime_download_concurrency": 2,
    "http_retry_attempts": 2,
    "stage_timeout_seconds": 3600,
    # V2 deliberately uses an isolated data root. It never scans, migrates,
    # imports, or falls back to V1's data/projects directory.
    "output_dir": str(PROJECTS_ROOT),
    "shortcut_undo": "Ctrl+Z",
    "shortcut_redo": "Ctrl+Y",
    "shortcut_play_pause": "Space",
    "shortcut_hide_cue": "Backspace",
    "timeline_zoom_modifier": "Alt",
}

ALLOWED_KEYS = set(DEFAULTS)

def apply_declared_model_capabilities(settings: dict[str, Any]) -> dict[str, Any]:
    """Persist only Stage modes the selected model can actually execute.

    The settings document is a runnable task contract, not a wish list.  A
    provider adapter still validates the request immediately before sending,
    but known or live-probed capabilities must already be reflected in the UI,
    saved settings and frozen project snapshot.
    """

    base_url = str(settings.get("translation_api_base_url") or "").strip()
    connection_model = str(settings.get("translation_api_model") or "").strip()
    cached_capabilities = settings.get("model_reasoning_capabilities", {})
    if not isinstance(cached_capabilities, dict):
        cached_capabilities = {}
    for stage in (
        "segmentation", "segmentation_repair", "translation",
        "translation_repair", "calibration", "audit_repair",
    ):
        model = str(settings.get(f"stage_{stage}_model") or connection_model).strip()
        if not base_url or not model:
            continue
        capability = reasoning_capabilities(base_url, model)
        cache_key = "\n".join((base_url.lower(), model.lower()))
        cached = cached_capabilities.get(cache_key, {})
        modes = (
            cached.get("supported_thinking_modes")
            if isinstance(cached, dict)
            else None
        )
        if not isinstance(modes, list) or not modes:
            modes = capability.get("supported_thinking_modes", [])
        supported = [
            str(value) for value in modes
            if str(value) in {"enabled", "disabled"}
        ]
        # Unknown compatible APIs remain user-configurable until their live
        # connectivity probe records a concrete contract.
        if not supported:
            continue
        thinking_key = f"stage_{stage}_thinking_mode"
        effort_key = f"stage_{stage}_reasoning_effort"
        requested = str(settings.get(thinking_key) or "disabled")
        if requested in supported:
            continue
        effective = "enabled" if "enabled" in supported else supported[0]
        settings[thinking_key] = effective
        if effective == "enabled":
            # The explicit policy for a preferred non-thinking Stage whose
            # model cannot disable thinking is Thinking Low.
            settings[effort_key] = "low"
    return settings


def _canonical_shortcut(value: Any) -> str | None:
    parts = [part.strip() for part in str(value or "").split("+")]
    if len(parts) < 2 or not parts[-1] or any(not part for part in parts):
        return None
    modifier_names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "meta": "Meta"}
    modifiers: set[str] = set()
    for part in parts[:-1]:
        modifier = modifier_names.get(part.casefold())
        if modifier is None or modifier in modifiers:
            return None
        modifiers.add(modifier)
    if not modifiers.intersection({"Ctrl", "Alt", "Meta"}) or parts[-1].casefold() in modifier_names:
        return None
    key = parts[-1].upper() if len(parts[-1]) == 1 else parts[-1]
    return "+".join([name for name in ("Ctrl", "Alt", "Shift", "Meta") if name in modifiers] + [key])


def _canonical_single_key(value: Any) -> str | None:
    key = str(value or "").strip()
    aliases = {" ": "Space", "spacebar": "Space", "space": "Space", "backspace": "Backspace"}
    return aliases.get(key.casefold())


def _canonical_modifier(value: Any) -> str | None:
    return {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "meta": "Meta"}.get(
        str(value or "").strip().casefold()
    )


def _unique_paths(paths: list[Path] | tuple[Path, ...]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve()).casefold()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return tuple(result)


def credential_file_candidates() -> tuple[Path, ...]:
    return (CREDENTIALS_FILE,)


def _write_credential_envelope(values: dict[str, str]) -> None:
    write_envelope(CREDENTIALS_FILE, values)


def load_credentials() -> dict[str, str]:
    return load_store(credential_file_candidates())


def save_credentials_from_settings(payload: dict[str, Any]) -> dict[str, str]:
    values = load_credentials()
    if payload.get("clear_api_key"):
        values.pop(ASR_QWEN, None)
    else:
        api_key = clean_credential(payload.get("api_key"))
        if api_key:
            values[ASR_QWEN] = api_key
    if payload.get("clear_alignment_api_key"):
        values.pop(ALIGN_DEEPSEEK, None)
    else:
        alignment_key = clean_credential(payload.get("alignment_api_key"))
        if alignment_key:
            values[ALIGN_DEEPSEEK] = alignment_key
    if any(
        key in payload
        for key in ("translation_api_base_url", "translation_api_key", "clear_translation_api_key")
    ):
        provider = canonical_provider_id(
            payload.get("active_model_provider")
            or infer_model_provider(payload.get("translation_api_base_url"))
        )
        provider_role = model_provider_credential_ref(provider)
        if payload.get("clear_translation_api_key"):
            values.pop(provider_role, None)
        else:
            translation_key = clean_credential(payload.get("translation_api_key"))
            if translation_key:
                values[provider_role] = translation_key
            else:
                existing_provider_key = values.get(provider_role)
                if existing_provider_key:
                    values[provider_role] = existing_provider_key
    _write_credential_envelope(values)
    return values


def load_settings(include_secret: bool = False) -> dict[str, Any]:
    settings = dict(DEFAULTS)
    stored: dict[str, Any] = {}
    settings_path = SETTINGS_FILE
    stored: dict[str, Any] = {}
    if settings_path.exists():
        try:
            stored = json.loads(settings_path.read_text(encoding="utf-8"))
            settings.update({k: v for k, v in stored.items() if k in ALLOWED_KEYS})
        except (OSError, json.JSONDecodeError):
            pass
    try:
        settings["startup_port"] = max(
            1024, min(65535, int(settings.get("startup_port", 8769)))
        )
    except (TypeError, ValueError):
        settings["startup_port"] = 8769
    if settings.get("appearance_mode") not in {"light", "dark"}:
        settings["appearance_mode"] = "dark"
    settings["output_dir"] = str(PROJECTS_ROOT)
    for name in ("shortcut_undo", "shortcut_redo"):
        settings[name] = _canonical_shortcut(settings.get(name)) or DEFAULTS[name]
    for name in ("shortcut_play_pause", "shortcut_hide_cue"):
        settings[name] = _canonical_single_key(settings.get(name)) or DEFAULTS[name]
    settings["timeline_zoom_modifier"] = (
        _canonical_modifier(settings.get("timeline_zoom_modifier")) or DEFAULTS["timeline_zoom_modifier"]
    )
    # v3 execution blocks are capped at about three minutes. Clamp persisted
    # 300-second defaults from earlier builds as soon as settings are loaded,
    # including non-UI callers that do not save settings again.
    settings["segmentation_chunk_seconds"] = max(
        30, min(180, int(settings.get("segmentation_chunk_seconds", 180)))
    )
    # Translation content is model-authored. A structurally invalid first
    # response is repaired by another model call, never by local text logic.
    settings["translation_api_timeout_seconds"] = max(
        30,
        min(600, int(settings.get("translation_api_timeout_seconds", 300))),
    )
    apply_declared_model_capabilities(settings)

    credentials = load_credentials()
    key = credentials.get(ASR_QWEN, "")
    alignment_key = credentials.get(ALIGN_DEEPSEEK, "")
    provider = canonical_provider_id(
        stored.get("active_model_provider")
        or infer_model_provider(settings.get("translation_api_base_url"))
    )
    settings["active_model_provider"] = provider
    translation_key = resolve_model_provider_credential(credentials, provider)
    settings["api_key_set"] = bool(key)
    settings["alignment_api_key_set"] = bool(alignment_key)
    settings["translation_api_key_set"] = bool(translation_key)
    settings["model_provider_key_set"] = {
        name: bool(resolve_model_provider_credential(credentials, name))
        for name in MODEL_PROVIDER_IDS
    }
    if include_secret:
        settings["api_key"] = key
        settings["alignment_api_key"] = alignment_key
        settings["translation_api_key"] = translation_key
    from .edition import constrain_settings

    return constrain_settings(settings)


def settings_for_model_provider(
    provider_id: str,
    *,
    include_secret: bool = False,
    base_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project one saved provider profile onto every shared LLM stage."""
    requested = str(provider_id or "").strip()
    if requested not in MODEL_PROVIDER_IDS:
        raise ValueError("模型服务商无效")
    settings = dict(base_settings or load_settings(include_secret=False))
    profiles = normalize_provider_profiles(settings.get("model_provider_profiles", {}))
    profile = profiles.get(requested)
    if not profile and requested == str(settings.get("active_model_provider") or ""):
        profile = {
            "base_url": settings.get("translation_api_base_url", ""),
            "model": settings.get("translation_api_model", ""),
            "auth_mode": settings.get("translation_api_auth_mode", "bearer"),
            "timeout_seconds": settings.get("translation_api_timeout_seconds", 300),
        }
    if not profile:
        raise ValueError("所选模型服务商尚未配置")
    base_url = str(profile.get("base_url") or "").strip()
    model = str(profile.get("model") or "").strip()
    if not base_url or not model:
        raise ValueError("所选模型服务商的地址或模型尚未配置")
    settings.update(
        active_model_provider=requested,
        translation_api_base_url=base_url,
        translation_api_model=model,
        translation_api_auth_mode=str(profile.get("auth_mode") or "bearer"),
        translation_api_timeout_seconds=int(profile.get("timeout_seconds") or 300),
    )
    for stage in (
        "segmentation", "segmentation_repair", "translation",
        "translation_repair", "calibration", "audit_repair",
    ):
        settings[f"stage_{stage}_model"] = model
    credentials = load_credentials()
    key = resolve_model_provider_credential(credentials, requested)
    settings["translation_api_key_set"] = bool(key)
    if include_secret:
        settings["translation_api_key"] = key
    else:
        settings.pop("translation_api_key", None)
    apply_declared_model_capabilities(settings)
    from .edition import constrain_settings

    return constrain_settings(settings)


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    current = load_settings(include_secret=True)
    current_provider = canonical_provider_id(
        current.get("active_model_provider")
        or infer_model_provider(current.get("translation_api_base_url"))
    )
    current_model = str(current.get("translation_api_model", "")).strip()
    merged = {k: current.get(k, DEFAULTS[k]) for k in ALLOWED_KEYS}
    merged.update({k: payload[k] for k in ALLOWED_KEYS if k in payload})
    from .edition import constrain_settings

    merged = constrain_settings(merged)
    raw_profiles = normalize_provider_profiles(merged.get("model_provider_profiles", {}))
    provider = canonical_provider_id(
        merged.get("active_model_provider")
        or infer_model_provider(merged.get("translation_api_base_url"))
    )
    merged["active_model_provider"] = provider
    next_model = str(merged.get("translation_api_model", "")).strip()
    for stage in (
        "segmentation", "segmentation_repair", "translation",
        "translation_repair", "calibration", "audit_repair",
    ):
        model_key = f"stage_{stage}_model"
        stage_model = str(merged.get(model_key, "")).strip()
        # Provider changes invalidate every previous provider model ID. Within
        # one provider, only inherited Stage values follow a model change.
        if provider != current_provider or not stage_model or stage_model == current_model:
            merged[model_key] = next_model
    base_url = str(merged.get("translation_api_base_url", "")).strip()
    parsed_base_url = urlparse(base_url)
    if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.netloc:
        raise ValueError("translation_api_base_url 必须是有效的 HTTP(S) 地址")
    model_id = str(merged.get("translation_api_model", "")).strip()
    if not model_id:
        raise ValueError("translation_api_model 不能为空")
    auth_mode = str(merged.get("translation_api_auth_mode", "bearer")).strip().lower()
    if auth_mode not in {"bearer", "api-key"}:
        raise ValueError("translation_api_auth_mode 无效")
    merged["translation_api_base_url"] = base_url
    merged["translation_api_model"] = model_id
    merged["translation_api_auth_mode"] = auth_mode
    raw_profiles[provider] = {
        "base_url": base_url,
        "model": model_id,
        "auth_mode": auth_mode,
        "timeout_seconds": int(merged.get("translation_api_timeout_seconds", 300)),
    }
    merged["model_provider_profiles"] = raw_profiles
    raw_capabilities = merged.get("model_reasoning_capabilities", {})
    if not isinstance(raw_capabilities, dict):
        raw_capabilities = {}
    merged["model_reasoning_capabilities"] = {
        str(key)[:1000]: value
        for key, value in list(raw_capabilities.items())[-100:]
        if isinstance(value, dict)
    }
    merged["output_dir"] = str(PROJECTS_ROOT)
    merged["startup_port"] = max(1024, min(65535, int(merged["startup_port"])))
    choices = {
        "appearance_mode": {"light", "dark"},
        "accent_color": {"purple", "blue", "cyan", "green", "orange", "pink"},
        "surface_style": {"standard", "glass"},
        "ui_density": {"comfortable", "compact"},
        "motion_level": {"full", "reduced", "none"},
        "font_scale": {"small", "standard", "large"},
    }
    for name, allowed in choices.items():
        if merged.get(name) not in allowed:
            merged[name] = DEFAULTS[name]

    merged["alignment_api_timeout_seconds"] = max(
        10, min(3600, int(merged["alignment_api_timeout_seconds"]))
    )
    merged["translation_api_timeout_seconds"] = max(
        30, min(600, int(merged["translation_api_timeout_seconds"]))
    )
    if merged["workflow_mode"] != "subtitle_creation":
        merged["workflow_mode"] = DEFAULTS["workflow_mode"]
    if merged["translation_style"] not in {
        "corporate_broadcast",
        "literal_review",
        "concise_web",
    }:
        merged["translation_style"] = DEFAULTS["translation_style"]
    merged["transcript_source"] = "qwen_cloud"
    if merged.get("qwen_cloud_region") not in {"beijing", "singapore"}:
        merged["qwen_cloud_region"] = DEFAULTS["qwen_cloud_region"]
    merged["qwen_cloud_base_url"] = str(merged.get("qwen_cloud_base_url", "")).strip().rstrip("/")
    if merged["qwen_cloud_base_url"] and not merged["qwen_cloud_base_url"].startswith("https://"):
        merged["qwen_cloud_base_url"] = ""
    merged["qwen_cloud_model"] = (
        str(merged.get("qwen_cloud_model", "")).strip()
        or DEFAULTS["qwen_cloud_model"]
    )
    merged["qwen_cloud_request_timeout_seconds"] = max(
        10, min(600, int(merged["qwen_cloud_request_timeout_seconds"]))
    )
    merged["qwen_cloud_task_timeout_seconds"] = max(
        60, min(43200, int(merged["qwen_cloud_task_timeout_seconds"]))
    )
    merged["qwen_cloud_poll_interval_seconds"] = max(
        1.0, min(30.0, float(merged["qwen_cloud_poll_interval_seconds"]))
    )
    merged["qwen_cloud_temporary_upload"] = True
    try:
        get_recognition_profile(str(merged.get("recognition_profile_id", "")))
    except ValueError:
        merged["recognition_profile_id"] = DEFAULT_RECOGNITION_PROFILE
    if merged["target_language_mode"] not in {
        "auto_opposite",
        "zh-CN",
        "en",
        "ja",
        "ko",
    }:
        merged["target_language_mode"] = DEFAULTS["target_language_mode"]
    if merged.get("display_order") not in {"en_zh", "zh_en", "source_target", "target_source"}:
        merged["display_order"] = DEFAULTS["display_order"]
    for key in ("top_raised_punctuation", "bottom_raised_punctuation"):
        if merged[key] not in {"preserve", "remove"}:
            merged[key] = DEFAULTS[key]
    for key in ("top_baseline_punctuation", "bottom_baseline_punctuation"):
        if merged[key] not in {"preserve", "normalize"}:
            merged[key] = DEFAULTS[key]
    merged["english_hard_limit"] = max(1, min(200, int(merged["english_hard_limit"])))
    merged["english_count_spaces"] = bool(merged["english_count_spaces"])
    merged["english_count_punctuation"] = bool(merged["english_count_punctuation"])
    merged["chinese_hard_limit"] = max(1, min(100, int(merged["chinese_hard_limit"])))
    merged["mixed_hard_limit"] = max(1, min(200, int(merged.get("mixed_hard_limit", 25))))
    merged["japanese_hard_limit"] = max(1, min(100, int(merged["japanese_hard_limit"])))
    merged["korean_hard_limit"] = max(1, min(120, int(merged["korean_hard_limit"])))
    merged["target_visual_width_limit"] = max(
        8, min(200, int(merged["target_visual_width_limit"]))
    )
    merged["minimum_cue_duration_ms"] = max(
        0, min(5000, int(merged["minimum_cue_duration_ms"]))
    )
    merged["maximum_cue_duration_ms"] = max(
        merged["minimum_cue_duration_ms"] + 1,
        min(60000, int(merged["maximum_cue_duration_ms"])),
    )
    merged["tail_padding_ms"] = max(0, min(1000, int(merged["tail_padding_ms"])))
    merged["snap_threshold_ms"] = max(
        0, min(2000, int(merged["snap_threshold_ms"]))
    )
    merged["maximum_cps_latin"] = max(
        1.0, min(100.0, float(merged["maximum_cps_latin"]))
    )
    merged["maximum_cps_cjk"] = max(
        1.0, min(100.0, float(merged["maximum_cps_cjk"]))
    )
    if merged["audio_denoise_mode"] not in {"off", "light"}:
        merged["audio_denoise_mode"] = DEFAULTS["audio_denoise_mode"]
    if merged["text_cleanup_mode"] not in {
        "preserve",
        "mark_conservative",
        "remove_conservative",
    }:
        merged["text_cleanup_mode"] = DEFAULTS["text_cleanup_mode"]
    if merged["translation_thinking_mode"] not in {"enabled", "disabled"}:
        merged["translation_thinking_mode"] = DEFAULTS[
            "translation_thinking_mode"
        ]
    if merged["translation_reasoning_effort"] not in {
        "low",
        "medium",
        "high",
        "max",
        "xhigh",
    }:
        merged["translation_reasoning_effort"] = DEFAULTS[
            "translation_reasoning_effort"
        ]
    for stage in (
        "segmentation",
        "segmentation_repair",
        "translation",
        "translation_repair",
        "calibration",
        "audit_repair",
    ):
        thinking_key = f"stage_{stage}_thinking_mode"
        effort_key = f"stage_{stage}_reasoning_effort"
        tokens_key = f"stage_{stage}_max_tokens"
        temperature_key = f"stage_{stage}_temperature"
        if merged[thinking_key] not in {"enabled", "disabled"}:
            merged[thinking_key] = DEFAULTS[thinking_key]
        if merged[effort_key] not in {"low", "medium", "high", "max", "xhigh"}:
            merged[effort_key] = DEFAULTS[effort_key]
        merged[tokens_key] = max(256, min(393216, int(merged[tokens_key])))
        merged[temperature_key] = max(
            0.0, min(2.0, float(merged[temperature_key]))
        )
    apply_declared_model_capabilities(merged)
    for key in (
        "segmentation_enabled",
        "translation_enabled",
        "calibration_enabled",
    ):
        merged[key] = bool(merged[key])
    merged["segmentation_strategy"] = "semantic"
    merged["sentence_boundary_policy"] = "unpunctuated"
    merged["split_workflow_mode"] = "one_step"
    merged["translation_workflow_mode"] = "one_step"
    merged["segmentation_chunk_seconds"] = max(
        75, min(100, int(merged["segmentation_chunk_seconds"]))
    )
    merged["segmentation_overlap_seconds"] = max(
        5, min(120, int(merged["segmentation_overlap_seconds"]))
    )
    merged["segmentation_batch_groups"] = max(
        1, min(50, int(merged["segmentation_batch_groups"]))
    )
    merged["segmentation_candidates"] = max(
        1, min(2, int(merged["segmentation_candidates"]))
    )
    merged["translation_workers"] = max(
        1, min(256, int(merged["translation_workers"]))
    )
    for key, maximum in (
        ("runtime_worker_concurrency", 16),
        ("runtime_cloud_concurrency", 16),
        ("runtime_media_concurrency", 8),
        ("runtime_gpu_concurrency", 4),
        ("runtime_download_concurrency", 8),
    ):
        merged[key] = max(1, min(maximum, int(merged[key])))
    merged["http_retry_attempts"] = max(
        1, min(3, int(merged["http_retry_attempts"]))
    )
    merged["stage_timeout_seconds"] = max(
        60, min(21600, int(merged["stage_timeout_seconds"]))
    )
    merged["project_name"] = str(merged["project_name"]).strip()[:100] or "默认项目"
    merged["output_dir"] = str(Path(str(merged["output_dir"])).expanduser().resolve())
    for name in ("shortcut_undo", "shortcut_redo"):
        value = _canonical_shortcut(merged.get(name))
        if value is None:
            raise ValueError(f"{name} 不是有效的组合快捷键")
        merged[name] = value
    for name in ("shortcut_play_pause", "shortcut_hide_cue"):
        value = _canonical_single_key(merged.get(name))
        if value is None:
            raise ValueError(f"{name} 只能使用 Space 或 Backspace")
        merged[name] = value
    modifier = _canonical_modifier(merged.get("timeline_zoom_modifier"))
    if modifier is None:
        raise ValueError("时间轴缩放修饰键只能使用 Ctrl、Alt、Shift 或 Meta")
    merged["timeline_zoom_modifier"] = modifier
    shortcut_values = {
        name: str(merged[name]).casefold()
        for name in ("shortcut_undo", "shortcut_redo", "shortcut_play_pause", "shortcut_hide_cue")
    }
    if len(set(shortcut_values.values())) != len(shortcut_values):
        raise ValueError("快捷键不能分配给多个命令")
    atomic_write_json(SETTINGS_FILE, merged)

    save_credentials_from_settings(payload)
    return load_settings(include_secret=False)
