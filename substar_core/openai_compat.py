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
