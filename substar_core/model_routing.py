from __future__ import annotations

from typing import Any, Mapping

from .reasoning_capabilities import (
    reasoning_effort_for_request,
    resolve_thinking_mode,
)


REPAIR_STAGES = frozenset(
    {"segmentation_repair", "translation_repair", "audit_repair"}
)


def resolve_stage_request(
    settings: Mapping[str, Any], stage: str
) -> dict[str, Any]:
    """Resolve one runnable provider request from the active connection.

    Every text-model feature uses this authority.  Stage selectors choose a
    model and policy, while the active provider owns the endpoint, auth mode
    and credential.  Provider capability adaptation happens here and again in
    the low-level serializer as a final safety boundary.
    """

    stage = str(stage).strip()
    if not stage:
        raise ValueError("stage is required")
    base_url = str(
        settings.get("translation_api_base_url") or "https://api.deepseek.com"
    ).strip()
    default_model = str(
        settings.get("translation_api_model") or "deepseek-v4-flash"
    ).strip()
    model = str(settings.get(f"stage_{stage}_model") or default_model).strip()
    requested_thinking = str(
        settings.get(f"stage_{stage}_thinking_mode")
        or ("disabled" if stage in REPAIR_STAGES else "enabled")
    ).strip().lower()
    effective_thinking = resolve_thinking_mode(
        base_url, model, requested_thinking
    )
    requested_effort = str(
        settings.get(f"stage_{stage}_reasoning_effort") or "low"
    ).strip().lower()
    effective_effort = reasoning_effort_for_request(
        base_url, model, requested_effort
    )
    api_key = str(settings.get("translation_api_key") or "").strip()
    return {
        "stage": stage,
        "base_url": base_url,
        "api_key": api_key,
        "auth_mode": str(
            settings.get("translation_api_auth_mode") or "bearer"
        ),
        "model": model,
        "requested_thinking_mode": requested_thinking,
        "thinking_mode": effective_thinking,
        "requested_reasoning_effort": requested_effort,
        "reasoning_effort": effective_effort,
        "max_tokens": int(
            settings.get(f"stage_{stage}_max_tokens") or 65536
        ),
        "temperature": float(
            settings.get(f"stage_{stage}_temperature") or 0.0
        ),
    }
