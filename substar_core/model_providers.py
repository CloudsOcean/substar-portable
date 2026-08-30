from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


# Cloud/API-only LLM providers.  Local model installers and translation-only
# engines deliberately do not belong to this catalog: every entry here must be
# able to serve the shared LLM stages through Chat Completions.
MODEL_PROVIDER_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "description": "官方 API",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-v4-flash",
        "auth_mode": "bearer",
    },
    {
        "id": "glm",
        "label": "GLM · 智谱",
        "description": "智谱官方 API",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-5.3-flash",
        "auth_mode": "bearer",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "description": "官方 API",
        "base_url": "https://api.openai.com/v1",
        "default_model": "",
        "auth_mode": "bearer",
    },
    {
        "id": "azure_openai",
        "label": "Azure OpenAI",
        "description": "部署端点 · API Key",
        "base_url": "",
        "default_model": "",
        "auth_mode": "api-key",
        "base_url_hint": "填写包含 deployments/{deployment} 与 api-version 的部署 URL",
    },
    {
        "id": "deerapi",
        "label": "DeerAPI",
        "description": "OpenAI 兼容",
        "base_url": "https://api.deerapi.com/v1",
        "default_model": "",
        "auth_mode": "bearer",
    },
    {
        "id": "gemini",
        "label": "Gemini",
        "description": "Google OpenAI 兼容",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.0-flash",
        "auth_mode": "bearer",
    },
    {
        "id": "siliconflow",
        "label": "硅基流动",
        "description": "OpenAI 兼容",
        "base_url": "https://api.siliconflow.cn/v1",
        "default_model": "",
        "auth_mode": "bearer",
    },
    {
        "id": "qwen",
        "label": "通义千问 · 阿里云百炼",
        "description": "兼容模式",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "",
        "auth_mode": "bearer",
    },
    {
        "id": "custom",
        "label": "自定义服务",
        "description": "OpenAI 兼容端点",
        "base_url": "",
        "default_model": "",
        "auth_mode": "bearer",
    },
)

MODEL_PROVIDER_IDS = tuple(item["id"] for item in MODEL_PROVIDER_CATALOG)


def provider_catalog() -> list[dict[str, Any]]:
    return [dict(item) for item in MODEL_PROVIDER_CATALOG]


def canonical_provider_id(value: Any) -> str:
    provider_id = str(value or "").strip().lower().replace("-", "_")
    return provider_id if provider_id in MODEL_PROVIDER_IDS else "custom"


def infer_model_provider(base_url: Any) -> str:
    parsed = urlparse(str(base_url or ""))
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if "deepseek.com" in host:
        return "deepseek"
    if "bigmodel.cn" in host:
        return "glm"
    if "azure.com" in host or "openai.azure.com" in host or "/deployments/" in path:
        return "azure_openai"
    if "api.openai.com" in host:
        return "openai"
    if "deerapi.com" in host:
        return "deerapi"
    if "generativelanguage.googleapis.com" in host:
        return "gemini"
    if "siliconflow" in host:
        return "siliconflow"
    if "dashscope" in host or "aliyuncs" in host:
        return "qwen"
    return "custom"


def normalize_provider_profiles(raw_profiles: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_profiles, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for raw_id, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, dict):
            continue
        provider_id = str(raw_id or "").strip().lower()
        if provider_id not in MODEL_PROVIDER_IDS:
            continue
        auth_mode = str(raw_profile.get("auth_mode", "bearer")).strip().lower()
        if auth_mode not in {"bearer", "api-key"}:
            auth_mode = "bearer"
        try:
            timeout = max(30, min(600, int(raw_profile.get("timeout_seconds", 300))))
        except (TypeError, ValueError):
            timeout = 300
        normalized[provider_id] = {
            "base_url": str(raw_profile.get("base_url", "")).strip()[:1000],
            "model": str(raw_profile.get("model", "")).strip()[:300],
            "auth_mode": auth_mode,
            "timeout_seconds": timeout,
        }
    return normalized
