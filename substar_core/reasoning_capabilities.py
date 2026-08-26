from __future__ import annotations

from urllib.parse import urlparse


REASONING_EFFORTS = ("low", "medium", "high", "max", "xhigh")


def _provider(base_url: str, model: str) -> str:
    host = (urlparse(str(base_url)).hostname or "").lower()
    model_name = str(model or "").strip().lower()
    if "deepseek.com" in host or model_name.startswith("deepseek-"):
        return "deepseek"
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
            "aliases": {"medium": "high", "xhigh": "high"},
            "probe_efforts": ["low", "high", "max"],
            "verified": True,
            "source": "deepseek-v4-contract",
            "note": "DeepSeek 的 Medium 和 XHigh 会映射为 High。",
        }
    if provider == "openai" and "gpt-5-pro" in model_name:
        return {
            "provider": provider,
            "model": model,
            "supported_efforts": ["high"],
            "aliases": {},
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
            "aliases": {},
            "probe_efforts": ["low", "medium", "high"],
            "verified": True,
            "source": "openai-model-contract",
            "note": "具体支持范围仍以该模型接口响应为准。",
        }
    return {
        "provider": provider,
        "model": model,
        "supported_efforts": list(REASONING_EFFORTS),
        "aliases": {},
        "probe_efforts": list(REASONING_EFFORTS),
        "verified": False,
        "source": "unverified-openai-compatible",
        "note": "兼容接口未声明能力；可点击探测来验证。",
    }


def resolve_reasoning_effort(base_url: str, model: str, requested: str) -> str:
    value = str(requested or "high").strip().lower()
    capabilities = reasoning_capabilities(base_url, model)
    aliases = capabilities.get("aliases", {})
    if isinstance(aliases, dict):
        value = str(aliases.get(value, value))
    return value if value in REASONING_EFFORTS else "high"
