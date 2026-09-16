from substar_core import web_routes


def test_application_pages_disable_html_shell_caching() -> None:
    response = web_routes.editor_page()

    assert response.headers["cache-control"] == "no-store"
    assert b"editor_document.js?v=20260916-save-safety-1" in response.body
