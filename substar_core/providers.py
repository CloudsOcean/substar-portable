from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .http_client import post

class ProviderError(RuntimeError):
    pass


def _is_official_mimo(settings: dict[str, Any]) -> bool:
    host = (urlparse(str(settings.get("api_base_url", ""))).hostname or "").lower()
    model = str(settings.get("api_model", "")).lower()
    return host == "api.xiaomimimo.com" or host.endswith(".xiaomimimo.com") or model.startswith("mimo-")


def _resolved_provider(settings: dict[str, Any]) -> str:
    """Recover safely from a stale UI provider selection.

    A MiMo model/base URL cannot use OpenAI's /audio/transcriptions route.
    """
    if _is_official_mimo(settings):
        return "mimo_chat"
    return str(settings.get("api_provider", ""))


def _endpoint(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith(suffix):
        return base
    return base + suffix


def _headers(api_key: str, auth_mode: str) -> dict[str, str]:
    if not api_key:
        return {}
    if auth_mode == "api-key":
        return {"api-key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


def _extract_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text") or item.get("content")
                if value:
                    parts.append(str(value))
        return "".join(parts).strip()
    return str(content or "").strip()


def transcribe_chunk(audio_path: Path, settings: dict[str, Any]) -> dict[str, Any]:
    provider = _resolved_provider(settings)
    if provider == "openai_transcriptions":
        return _openai_transcriptions(audio_path, settings)
    if provider == "mimo_chat":
        return _mimo_chat(audio_path, settings)
    raise ProviderError(f"未知 API 类型：{provider}")


def _openai_transcriptions(audio_path: Path, settings: dict[str, Any]) -> dict[str, Any]:
    url = _endpoint(settings["api_base_url"], "/audio/transcriptions")
    data: dict[str, str] = {"model": settings["api_model"], "response_format": "json"}
    language = settings.get("language", "Auto")
    language_codes = {"Chinese": "zh", "English": "en", "Cantonese": "yue"}
    if language in language_codes:
        data["language"] = language_codes[language]

    try:
        with audio_path.open("rb") as audio_file:
            mime = mimetypes.guess_type(audio_path.name)[0] or "application/octet-stream"
            response = post(
                url,
                headers=_headers(settings.get("api_key", ""), settings["api_auth_mode"]),
                files={"file": (audio_path.name, audio_file, mime)},
                data=data,
                timeout=int(settings["api_timeout_seconds"]),
            )
        response.raise_for_status()
        body = response.json()
    except requests.RequestException as exc:
        detail = getattr(exc.response, "text", "")[:500] if getattr(exc, "response", None) else ""
        raise ProviderError(f"转写 API 请求失败：{exc} {detail}") from exc
    except ValueError as exc:
        raise ProviderError("转写 API 没有返回有效 JSON") from exc

    text = str(body.get("text", "")).strip()
    if not text:
        raise ProviderError("转写 API 返回了空文本")
    return {"text": text, "language": body.get("language", ""), "raw": body}


def _mimo_chat(audio_path: Path, settings: dict[str, Any]) -> dict[str, Any]:
    url = _endpoint(settings["api_base_url"], "/chat/completions")
    encoded = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    if len(encoded) > 10 * 1024 * 1024:
        raise ProviderError(
            f"MiMo Base64 音频为 {len(encoded) / 1024 / 1024:.2f} MB，超过官方 10 MB 上限"
        )
    mime = mimetypes.guess_type(audio_path.name)[0] or "audio/wav"
    payload = {
        "model": settings["api_model"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": f"data:{mime};base64,{encoded}",
                            "format": audio_path.suffix.lower().lstrip(".") or "wav",
                        },
                    }
                ],
            }
        ],
        "stream": False,
    }
    # Xiaomi's official endpoint authenticates with the api-key header. Keep the
    # configured mode for third-party MiMo-compatible gateways.
    auth_mode = "api-key" if _is_official_mimo(settings) else settings["api_auth_mode"]
    try:
        response = post(
            url,
            headers={
                **_headers(settings.get("api_key", ""), auth_mode),
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=int(settings["api_timeout_seconds"]),
        )
        response.raise_for_status()
        body = response.json()
    except requests.RequestException as exc:
        detail = getattr(exc.response, "text", "")[:500] if getattr(exc, "response", None) else ""
        raise ProviderError(f"MiMo API 请求失败：{exc} {detail}") from exc
    except ValueError as exc:
        raise ProviderError("MiMo API 没有返回有效 JSON") from exc

    try:
        text = _extract_message_text(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError("MiMo API 响应中找不到转写文字") from exc
    if not text:
        raise ProviderError("MiMo API 返回了空文本")
    return {"text": text, "language": "", "raw": body}
