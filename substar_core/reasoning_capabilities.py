from __future__ import annotations

from urllib.parse import urlparse


REASONING_EFFORTS = ("low", "medium", "high", "max", "xhigh")


def _provider(base_url: str, model: str) -> str:
    host = (urlparse(str(base_url)).hostname or "").lower()
    model_name = str(model or "").strip().lower()
    if "deepseek.com" in host or model_name.startswith("deepseek-"):
        return "deepseek"
    if "bigmodel.cn" in host or model_name.startswith("glm-"):
        return "glm"
    if "openai.com" in host or model_name.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    return "openai-compatible"


def reasoning_capabilities(base_url: str, model: str) -> dict[str, object]:
    """Return the best-known effort contract without making a paid request.

    OpenAI-compatible gateways do not expose a reliable, standard capability
    schema. Known provider contracts are therefore explicit, while unknown
    endpoints are marked unverified and can be checked with the probe API.
    """

    provider = _provider(base_url, model)
    model_name = str(model or "").strip().lower()
    if provider == "deepseek":
        return {
            "provider": provider,
            "model": model,
            "supported_efforts": ["low", "high", "max"],
            "effort_selection_aliases": {"medium": "high", "xhigh": "high"},
            "effort_wire_map": {"low": "low", "high": "high", "max": "max"},
            "probe_efforts": ["low", "high", "max"],
            "verified": True,
            "source": "deepseek-v4-contract",
            "note": "DeepSeek 的 Medium 和 XHigh 会映射为 High。",
        }
    if provider == "glm" and model_name.startswith(("glm-5.2", "glm-5.3")):
        forced_thinking = model_name.startswith("glm-5.3")
        glm_53 = model_name.startswith("glm-5.3")
        return {
            "provider": provider,
            "model": model,
            "supported_efforts": ["low", "high", "max"] if glm_53 else ["high", "max"],
            "effort_selection_aliases": (
                {"medium": "high", "xhigh": "max"}
                if glm_53
                else {"low": "high", "medium": "high", "xhigh": "max"}
            ),
            "effort_wire_map": {"low": "low", "high": "high", "max": "max"},
            "probe_efforts": ["low", "high", "max"] if glm_53 else ["high", "max"],
            "forced_thinking": forced_thinking,
            "supported_thinking_modes": ["enabled"] if forced_thinking else ["enabled", "disabled"],
            "verified": True,
            "source": "zhipu-glm-5.3-contract" if forced_thinking else "zhipu-glm-5.2-contract",
            "note": (
                "GLM-5.3 强制开启思考，并提供 Low、High、Max 三档。"
                if forced_thinking
                else "GLM 的 Low 和 Medium 会映射为 High，XHigh 会映射为 Max。"
            ),
        }
    if provider == "openai" and "gpt-5-pro" in model_name:
        return {
            "provider": provider,
            "model": model,
            "supported_efforts": ["high"],
            "effort_selection_aliases": {},
            "effort_wire_map": {"high": "high"},
            "probe_efforts": ["high"],
            "verified": True,
            "source": "openai-model-contract",
            "note": "该模型只使用 High 推理强度。",
        }
    if provider == "openai" and "gpt-5.1" in model_name:
        return {
            "provider": provider,
            "model": model,
            "supported_efforts": ["low", "medium", "high"],
            "effort_selection_aliases": {},
            "effort_wire_map": {"low": "low", "medium": "medium", "high": "high"},
            "probe_efforts": ["low", "medium", "high"],
            "verified": True,
            "source": "openai-model-contract",
            "note": "具体支持范围仍以该模型接口响应为准。",
        }
    return {
        "provider": provider,
        "model": model,
        "supported_efforts": list(REASONING_EFFORTS),
        "effort_selection_aliases": {},
        "effort_wire_map": {value: value for value in REASONING_EFFORTS},
        "probe_efforts": list(REASONING_EFFORTS),
        "verified": False,
        "source": "unverified-openai-compatible",
        "note": "兼容接口未声明能力；可点击探测来验证。",
    }


def resolve_reasoning_effort(base_url: str, model: str, requested: str) -> str:
    value = str(requested or "high").strip().lower()
    capabilities = reasoning_capabilities(base_url, model)
    aliases = capabilities.get("effort_selection_aliases", {})
    if isinstance(aliases, dict):
        value = str(aliases.get(value, value))
    return value if value in REASONING_EFFORTS else "high"


def reasoning_effort_for_request(base_url: str, model: str, selected: str) -> str:
    """Serialize a UI effort through the model's declared provider wire map."""

    normalized = resolve_reasoning_effort(base_url, model, selected)
    capabilities = reasoning_capabilities(base_url, model)
    wire_map = capabilities.get("effort_wire_map", {})
    if isinstance(wire_map, dict):
        return str(wire_map.get(normalized, normalized))
    return normalized


def resolve_thinking_mode(base_url: str, model: str, requested: str) -> str:
    """Choose a declared thinking mode without embedding provider request logic."""

    value = str(requested or "disabled").strip().lower()
    capabilities = reasoning_capabilities(base_url, model)
    modes = capabilities.get("supported_thinking_modes", ["disabled", "enabled"])
    if not isinstance(modes, list) or not modes:
        modes = ["disabled", "enabled"]
    normalized = [str(item) for item in modes if str(item) in {"enabled", "disabled"}]
    if not normalized:
        normalized = ["disabled", "enabled"]
    return value if value in normalized else normalized[0]
