from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import requests
from substar_core.job_diagnostics import model_failure, request_failure, response_diagnostics, safe_text
from substar_core.model_concurrency import model_slot, retry_after_seconds

from substar_core.openai_compat import auth_headers, endpoint_url, chat_controls
from substar_core.reasoning_capabilities import (
    reasoning_effort_for_request,
    resolve_thinking_mode,
)


class ModelGatewayError(RuntimeError):
    pass


class ModelGatewayRequestError(ModelGatewayError):
    def __init__(self, message: str, *, status: int | None = None, diagnostics=None) -> None:
        super().__init__(message)
        self.status = status
        self.diagnostics = dict(diagnostics or {})

    @property
    def retryable(self) -> bool:
        if self.diagnostics.get("code") == "model_finish_error":
            return False
        if self.diagnostics.get("provider_code") in {"1113", "1308", "1309", "1310", "1311"} and self.diagnostics.get("official_glm"):
            return False
        return self.status is None or self.status in {408, 425, 429} or (
            self.status is not None and self.status >= 500
        )


def _response_json_utf8(response: requests.Response) -> Any:
    return json.loads(response.content.decode("utf-8-sig"))


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


def call_text_model(
    *,
    base_url: str,
    api_key: str,
    auth_mode: str = "bearer",
    model: str,
    system_prompt: str,
    user_payload: Mapping[str, Any] | None = None,
    user_text: str | None = None,
    timeout: int,
    thinking_mode: str,
    reasoning_effort: str,
    request_attempts: int = 2,
    max_tokens: int = 32768,
    temperature: float = 0.0,
    conversation_tail: Sequence[Mapping[str, Any]] | None = None,
    cancel_event: Any = None,
) -> tuple[str, dict[str, Any]]:
    """The production transport for raw text-model responses.

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
    if user_text is None and user_payload is None:
        raise ModelGatewayError("模型请求正文不可为空")
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                str(user_text)
                if user_text is not None
                else json.dumps(dict(user_payload or {}), ensure_ascii=False)
            ),
        },
    ]
    for raw_message in conversation_tail or ():
        role = str(raw_message.get("role") or "").strip()
        content = raw_message.get("content")
        if role not in {"assistant", "user"} or not isinstance(content, str):
            raise ModelGatewayError("模型续接消息必须是 assistant/user 文本")
        messages.append({"role": role, "content": content})

    output_budget = max(1, int(max_tokens))
    payload: dict[str, Any] = {
        "model": str(model),
        "messages": messages,
        "stream": False,
        **chat_controls(base_url, model, output_budget, effective_mode, effective_effort, float(temperature)),
    }

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
    error_context = {
        "model": safe_text(model, secret=api_key, limit=160),
        "official_glm": urlsplit(base_url).hostname == "open.bigmodel.cn",
    }
    for attempt in range(1, attempts + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("模型请求已取消")
        used_attempts = attempt
        try:
            slot_args = {"cancelled": cancel_event.is_set} if cancel_event is not None else {}
            with model_slot(base_url, model, api_key, **slot_args):
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
            try:
                parsed_body = _response_json_utf8(response)
            except ValueError as exc:
                raise ModelGatewayRequestError(
                    "模型服务返回的内容不是有效 JSON",
                    status=response.status_code,
                    diagnostics={**error_context, **response_diagnostics(response, body={}),
                                 "code": "invalid_model_response"},
                ) from exc
            if not isinstance(parsed_body, dict):
                raise ModelGatewayRequestError(
                    "模型服务返回的 JSON 不是对象",
                    status=response.status_code,
                    diagnostics={**error_context, **response_diagnostics(response, body={}),
                                 "code": "invalid_model_response"},
                )
            if parsed_body.get("error"):
                details = response_diagnostics(response, body=parsed_body, secret=api_key)
                raise ModelGatewayRequestError(
                    model_failure(base_url, details), status=response.status_code,
                    diagnostics={**details, **error_context},
                )
            body = parsed_body
            break
        except requests.RequestException as exc:
            last_error = exc
            failure_response = exc.response
            status = failure_response.status_code if failure_response is not None else None
            details = (
                response_diagnostics(failure_response, secret=api_key)
                if failure_response is not None else request_failure(exc)
            )
            wrapped = ModelGatewayRequestError(
                details.get("message") or model_failure(base_url, details),
                status=status,
                diagnostics={**details, **error_context},
            )
            if wrapped.retryable and attempt < attempts:
                delay = retry_after_seconds(exc.response, attempt) if status==429 else min(3.0, attempt * 0.75)
                if cancel_event is not None:
                    if cancel_event.wait(delay): raise InterruptedError("模型请求已取消")
                else: time.sleep(delay)
                continue
            raise wrapped from exc
    if body is None:
        raise ModelGatewayError(f"模型 API 请求未完成：{last_error}")
    choices = body.get("choices")
    first_choice = choices[0] if isinstance(choices, list) and choices else None
    finish_reason = first_choice.get("finish_reason") if isinstance(first_choice, dict) else None
    if finish_reason == "length":
        raise ModelGatewayError(
            f"模型输出达到当前 {output_budget} token 上限"
        )
    if isinstance(finish_reason, str) and finish_reason in {"content_filter", "sensitive", "network_error", "error"}:
        raise ModelGatewayRequestError(
            "模型异常结束 · " + finish_reason,
            status=response.status_code,
            diagnostics={**error_context, "code": "model_finish_error", "finish_reason": finish_reason,
                         "http_status": response.status_code},
        )
    result = _response_text(body)
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
        "max_tokens": output_budget,
        "attempted_output_budgets": [output_budget],
        "transport_attempt_count": used_attempts,
        "usage": usage,
        "cache_usage": {
            "hit_tokens": int(usage.get("prompt_cache_hit_tokens", 0) or 0),
            "miss_tokens": int(usage.get("prompt_cache_miss_tokens", 0) or 0),
            "cached_tokens": int(prompt_details.get("cached_tokens", 0) or 0),
        },
    }


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
    cancel_event: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compatibility wrapper for stages that still use a JSON model contract."""
    text, telemetry = call_text_model(
        base_url=base_url,
        api_key=api_key,
        auth_mode=auth_mode,
        model=model,
        system_prompt=system_prompt,
        user_payload=user_payload,
        timeout=timeout,
        thinking_mode=thinking_mode,
        reasoning_effort=reasoning_effort,
        request_attempts=request_attempts,
        max_tokens=max_tokens,
        temperature=temperature,
        conversation_tail=conversation_tail,
    )
    return _extract_json(text), telemetry


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
