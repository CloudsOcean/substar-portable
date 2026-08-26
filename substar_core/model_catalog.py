from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import requests

from .http_client import get

class ModelCatalogError(RuntimeError):
    pass


def _catalog_urls(base_url: str) -> list[str]:
    clean = base_url.strip().rstrip("/")
    if clean.endswith("/chat/completions"):
        clean = clean[: -len("/chat/completions")]
    parsed = urlparse(clean)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ModelCatalogError("模型 Base URL 必须是有效的 HTTP(S) 地址")
    candidates = [f"{clean}/models"]
    if not clean.endswith("/v1"):
        candidates.append(f"{clean}/v1/models")
    return list(dict.fromkeys(candidates))


def _extract_models(payload: Any) -> list[str]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("data")
        if not isinstance(rows, list):
            rows = payload.get("models")
    else:
        rows = None
    if not isinstance(rows, list):
        return []
    models: list[str] = []
    for row in rows:
        if isinstance(row, str):
            value = row
        elif isinstance(row, dict):
            value = row.get("id") or row.get("name") or row.get("model")
        else:
            value = None
        if isinstance(value, str) and value.strip():
            models.append(value.strip())
    return sorted(set(models), key=str.casefold)


def discover_models(
    *,
    base_url: str,
    api_key: str,
    auth_mode: str = "bearer",
    timeout: int = 20,
) -> dict[str, Any]:
    if not api_key.strip():
        raise ModelCatalogError("未找到已保存的翻译模型 API Key")
    headers = (
        {"api-key": api_key.strip()}
        if auth_mode == "api-key"
        else {"Authorization": f"Bearer {api_key.strip()}"}
    )
    errors: list[str] = []
    for url in _catalog_urls(base_url):
        try:
            response = get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            errors.append(f"{url}: {exc}")
            continue
        if response.status_code >= 400:
            errors.append(f"{url}: HTTP {response.status_code}")
            continue
        try:
            models = _extract_models(response.json())
        except ValueError:
            errors.append(f"{url}: 返回内容不是 JSON")
            continue
        if models:
            return {"models": models, "endpoint": url}
        errors.append(f"{url}: 未发现模型列表")
    detail = "；".join(errors[-2:]) or "服务商未返回可用模型"
    raise ModelCatalogError(
        f"无法读取模型列表：{detail}。仍可在各 Stage 手动填写模型 ID"
    )
