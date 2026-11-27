from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def endpoint_url(base_url: str, suffix: str) -> str:
    """Append an OpenAI-compatible route without losing endpoint query data.

    Azure deployment URLs commonly carry ``api-version`` in the query string;
    simple string concatenation puts the route after that query and corrupts
    the request URL.
    """

    parsed = urlsplit(str(base_url or "").strip())
    path = parsed.path.rstrip("/")
    route = "/" + suffix.strip("/")
    if not path.lower().endswith(route.lower()):
        path += route
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def auth_headers(api_key: str, auth_mode: str = "bearer") -> dict[str, str]:
    if str(auth_mode).strip().lower() == "api-key":
        return {"api-key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


def chat_controls(base_url, model, max_tokens, mode, effort, temperature):
    """Serialize the same chat controls for connectivity checks and actual work."""
    host = urlsplit(base_url).hostname or ""
    official = host == "api.openai.com" or host.endswith(".openai.azure.com")
    reasoning_model = model.lower().startswith(("o1", "o3", "o4", "gpt-5", "gpt-6"))
    if official:
        controls = {"max_completion_tokens": max_tokens}
        if reasoning_model and mode == "enabled":
            controls["reasoning_effort"] = effort
        elif not reasoning_model:
            controls["temperature"] = temperature
        return controls
    controls = {"max_tokens": max_tokens}
    if mode == "enabled":
        controls["thinking"] = {"type": "enabled"}
        controls["reasoning_effort"] = effort
    elif mode == "disabled":
        controls["thinking"] = {"type": "disabled"}
        controls["temperature"] = temperature
    return controls
