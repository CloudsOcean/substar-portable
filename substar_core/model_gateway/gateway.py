from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Mapping, Sequence

import requests

from substar_core.openai_compat import auth_headers, endpoint_url
from substar_core.reasoning_capabilities import (
    reasoning_effort_for_request,
    resolve_thinking_mode,
)


class ModelGatewayError(RuntimeError):
    pass


class ModelGatewayRequestError(ModelGatewayError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status in {408, 425, 429} or (
            self.status is not None and self.status >= 500
        )


def _response_json_utf8(response: requests.Response) -> Any:
    return json.loads(response.content.decode("utf-8-sig"))


def _response_text_utf8(response: requests.Response) -> str:
    return response.content.decode("utf-8-sig", errors="replace")


def _response_text(body: Mapping[str, Any]) -> str:
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelGatewayError("模型响应中找不到 choices[0].message") from exc
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        combined = "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
        if combined.strip():
            return combined
    if content not in (None, ""):
        return str(content)
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    raise ModelGatewayError("模型响应正文为空")


def _extract_json(text: str) -> dict[str, Any]:
    value = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.DOTALL)
    if fenced:
        value = fenced.group(1)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ModelGatewayError(f"模型没有返回有效 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise ModelGatewayError("模型 JSON 顶层必须是对象")
    return parsed


def call_json_model(
    *,
    base_url: str,
    api_key: str,
    auth_mode: str = "bearer",
    model: str,
    system_prompt: str,
    user_payload: Mapping[str, Any],
    timeout: int,
    thinking_mode: str,
    reasoning_effort: str,
    request_attempts: int = 2,
    max_tokens: int = 32768,
    temperature: float = 0.0,
    conversation_tail: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The only production transport for text-model JSON requests.

    Provider/model/key/thinking are frozen by the caller's task snapshot. This
    adapter applies provider capability mapping once, performs bounded
    transport retries only, and never performs semantic repair.
    """

    if not str(api_key).strip():
        raise ModelGatewayError("模型服务密钥不可用")
    requested_mode = str(thinking_mode or "disabled").strip().lower()
    effective_mode = resolve_thinking_mode(base_url, model, requested_mode)
    requested_effort = str(reasoning_effort or "low").strip().lower()
    effective_effort = reasoning_effort_for_request(
        base_url, model, requested_effort
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(dict(user_payload), ensure_ascii=False),
        },
    ]
    for raw_message in conversation_tail or ():
        role = str(raw_message.get("role") or "").strip()
        content = raw_message.get("content")
        if role not in {"assistant", "user"} or not isinstance(content, str):
            raise ModelGatewayError("模型续接消息必须是 assistant/user 文本")
        messages.append({"role": role, "content": content})

    payload: dict[str, Any] = {
        "model": str(model),
        "messages": messages,
        "max_tokens": max(1, int(max_tokens)),
        "stream": False,
    }
    if effective_mode == "enabled":
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = effective_effort
    else:
        payload["thinking"] = {"type": "disabled"}
        payload["temperature"] = float(temperature)

    idempotency_key = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    attempts = max(1, min(3, int(request_attempts)))
    started = time.perf_counter()
    body: dict[str, Any] | None = None
    last_error: Exception | None = None
    used_attempts = 0
    for attempt in range(1, attempts + 1):
        used_attempts = attempt
        try:
            response = requests.post(
                endpoint_url(base_url, "/chat/completions"),
                headers={
                    **auth_headers(api_key, auth_mode),
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotency_key,
                },
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            parsed_body = _response_json_utf8(response)
            if not isinstance(parsed_body, dict):
                raise ModelGatewayError("模型 HTTP 响应不是 JSON 对象")
            body = parsed_body
            break
        except requests.RequestException as exc:
            last_error = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            wrapped = ModelGatewayRequestError(
                "模型 API 请求失败："
                + str(exc)
                + (
                    " " + _response_text_utf8(exc.response)[:1000]
                    if getattr(exc, "response", None) is not None
                    else ""
                ),
                status=status,
            )
            if wrapped.retryable and attempt < attempts:
                time.sleep(min(3.0, attempt * 0.75))
                continue
            raise wrapped from exc
        except ValueError as exc:
            raise ModelGatewayError("模型 API 返回的 HTTP 内容不是 JSON") from exc
    if body is None:
        raise ModelGatewayError(f"模型 API 请求未完成：{last_error}")
    finish_reason = body.get("choices", [{}])[0].get("finish_reason")
    if finish_reason == "length":
        raise ModelGatewayError(
            f"模型输出达到当前 {payload['max_tokens']} token 上限"
        )
    result = _extract_json(_response_text(body))
    usage = body.get("usage", {})
    usage = usage if isinstance(usage, dict) else {}
    prompt_details = usage.get("prompt_tokens_details", {})
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    return result, {
        "model": model,
        "requested_thinking_mode": requested_mode,
        "effective_thinking_mode": effective_mode,
        "thinking_mode": effective_mode,
        "requested_reasoning_effort": (
            requested_effort if effective_mode == "enabled" else None
        ),
        "effective_reasoning_effort": (
            effective_effort if effective_mode == "enabled" else None
        ),
        "reasoning_effort": (
            effective_effort if effective_mode == "enabled" else None
        ),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "finish_reason": finish_reason,
        "max_tokens": payload["max_tokens"],
        "attempted_output_budgets": [payload["max_tokens"]],
        "transport_attempt_count": used_attempts,
        "usage": usage,
        "cache_usage": {
            "hit_tokens": int(usage.get("prompt_cache_hit_tokens", 0) or 0),
            "miss_tokens": int(usage.get("prompt_cache_miss_tokens", 0) or 0),
            "cached_tokens": int(prompt_details.get("cached_tokens", 0) or 0),
        },
    }


def call_translation_model(
    *, groups: list[dict[str, Any]], mapping_mode: str | None = None, **kwargs: Any
):
    user_payload: dict[str, Any] = {"groups": groups}
    if mapping_mode:
        user_payload["mapping_mode"] = str(mapping_mode)
    return call_json_model(
        user_payload=user_payload,
        **kwargs,
    )
