from __future__ import annotations

import base64
import io
import json
import wave
from typing import Any
from urllib.parse import urlparse

import requests

from .http_client import post
from .reasoning_capabilities import reasoning_effort_for_request
from .openai_compat import auth_headers, endpoint_url


class ApiTestError(RuntimeError):
    pass


def endpoint(base_url: str, suffix: str) -> str:
    return endpoint_url(base_url, suffix)


def silence_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 6400)
    return buffer.getvalue()


def safe_error(response: requests.Response) -> str:
    body = response.text.strip()
    if not body:
        return f"HTTP {response.status_code}（服务未返回错误正文）"
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            error = parsed.get("error") or parsed.get("detail") or parsed.get("message")
            if isinstance(error, dict):
                error = error.get("message") or json.dumps(error, ensure_ascii=False)
            if error:
                return f"HTTP {response.status_code}: {error}"
    except ValueError:
        pass
    return f"HTTP {response.status_code}: {body[:500]}"


def test_chat(
    *,
    base_url: str,
    model: str,
    api_key: str,
    auth_mode: str,
    timeout: int,
    thinking_mode: str = "disabled",
    reasoning_effort: str = "low",
    max_tokens: int = 32,
    strict_controls: bool = False,
) -> dict[str, Any]:
    wire_effort = reasoning_effort_for_request(base_url, model, reasoning_effort)
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "user", "content": '只返回 JSON：{"substar_ok":true}'}
        ],
        "max_tokens": max(32, int(max_tokens)),
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    if thinking_mode == "enabled":
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = wire_effort
    elif thinking_mode == "disabled":
        payload["thinking"] = {"type": "disabled"}
        payload["temperature"] = 0
    try:
        response = post(
            endpoint(base_url, "/chat/completions"),
            headers={**auth_headers(api_key, auth_mode), "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ApiTestError(f"网络请求失败：{exc}") from exc
    if not response.ok and not strict_controls and response.status_code in {400, 422}:
        minimal_payload = {
            key: value for key, value in payload.items()
            if key not in {"response_format", "thinking", "reasoning_effort", "temperature"}
        }
        try:
            response = post(
                endpoint(base_url, "/chat/completions"),
                headers={**auth_headers(api_key, auth_mode), "Content-Type": "application/json"},
                json=minimal_payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise ApiTestError(f"网络请求失败：{exc}") from exc
    if not response.ok:
        raise ApiTestError(safe_error(response))
    try:
        body = response.json()
        message = body["choices"][0]["message"]
        if not isinstance(message, dict):
            raise TypeError("message is not an object")
        content = message.get("content")
        reasoning_content = message.get("reasoning_content")
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ApiTestError("接口已响应，但返回格式不是 Chat Completions") from exc
    response_channel = (
        "content" if str(content or "").strip()
        else "reasoning_content" if str(reasoning_content or "").strip()
        else "empty"
    )
    return {
        "ok": True,
        "message": (
            f"连接成功，模型 {model} 已返回内容"
            if response_channel != "empty"
            else f"连接成功，模型 {model} 已接受请求（测试响应无正文）"
        ),
        "model": body.get("model", model),
        "usage": body.get("usage", {}),
        "response_channel": response_channel,
        "requested_reasoning_effort": reasoning_effort if thinking_mode == "enabled" else None,
        "effective_reasoning_effort": wire_effort if thinking_mode == "enabled" else None,
    }


def probe_chat_thinking_modes(
    *,
    base_url: str,
    model: str,
    api_key: str,
    auth_mode: str,
    timeout: int,
    reasoning_effort: str = "high",
) -> dict[str, Any]:
    thinking_results: dict[str, Any] = {}
    for mode in ("disabled", "enabled"):
        try:
            response = test_chat(
                base_url=base_url,
                model=model,
                api_key=api_key,
                auth_mode=auth_mode,
                timeout=timeout,
                thinking_mode=mode,
                reasoning_effort=reasoning_effort,
                max_tokens=256,
                strict_controls=True,
            )
            thinking_results[mode] = {
                "accepted": True,
                "effective": response.get("effective_reasoning_effort"),
            }
        except ApiTestError as exc:
            thinking_results[mode] = {"accepted": False, "error": str(exc)}
    return {
        "thinking_results": thinking_results,
        "accepted_thinking_modes": [
            mode for mode in ("disabled", "enabled")
            if thinking_results.get(mode, {}).get("accepted")
        ],
    }


def test_transcription(
    *, base_url: str, model: str, api_key: str, auth_mode: str, timeout: int
) -> dict[str, Any]:
    try:
        response = post(
            endpoint(base_url, "/audio/transcriptions"),
            headers=auth_headers(api_key, auth_mode),
            files={"file": ("substar_connection_test.wav", silence_wav(), "audio/wav")},
            data={"model": model, "response_format": "json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ApiTestError(f"网络请求失败：{exc}") from exc
    if not response.ok:
        raise ApiTestError(safe_error(response))
    return {"ok": True, "message": f"连接成功，转写模型 {model} 接受了测试音频"}


def test_mimo_audio(
    *, base_url: str, model: str, api_key: str, auth_mode: str, timeout: int
) -> dict[str, Any]:
    audio = base64.b64encode(silence_wav()).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": f"data:audio/wav;base64,{audio}", "format": "wav"},
                    }
                ],
            }
        ],
        "stream": False,
    }
    host = (urlparse(base_url).hostname or "").lower()
    mode = "api-key" if host.endswith("xiaomimimo.com") else auth_mode
    try:
        response = post(
            endpoint(base_url, "/chat/completions"),
            headers={**auth_headers(api_key, mode), "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ApiTestError(f"网络请求失败：{exc}") from exc
    if not response.ok:
        raise ApiTestError(safe_error(response))
    return {"ok": True, "message": f"连接成功，MiMo 模型 {model} 接受了测试音频"}
