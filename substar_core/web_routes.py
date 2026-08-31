from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from .config import PROJECT_ROOT


WEB_DIR = PROJECT_ROOT / "web"
router = APIRouter(include_in_schema=False)


def _page(filename: str) -> HTMLResponse:
    # Portable upgrades keep the same localhost origin.  Prevent an old HTML
    # shell from pinning schema-specific assets after the executable changes.
    return HTMLResponse(
        (WEB_DIR / filename).read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/", response_class=HTMLResponse)
@router.get("/split", response_class=HTMLResponse)
def creation_page() -> HTMLResponse:
    return _page("split.html")


@router.get("/editor", response_class=HTMLResponse)
def editor_page() -> HTMLResponse:
    return _page("editor.html")


@router.get("/glossary", response_class=HTMLResponse)
def glossary_page() -> HTMLResponse:
    return _page("glossary.html")


@router.get("/settings", response_class=HTMLResponse)
def settings_page() -> HTMLResponse:
    return _page("settings.html")
