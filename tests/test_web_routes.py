import re
from substar_core import web_routes


def test_application_pages_disable_html_shell_caching() -> None:
    response = web_routes.editor_page()

    assert response.headers["cache-control"] == "no-store"
    assert re.search(rb'src="/assets/editor_document\.js\?v=[^"\s]+"', response.body)
