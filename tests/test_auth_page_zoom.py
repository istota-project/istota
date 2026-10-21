"""The server-rendered auth pages never zoom in the iOS app (ISSUE-821).

The SvelteKit half is ISSUE-818. In a browser these pages lose only double-tap
zoom; pinch-zoom stays. Running the pre-paint script under an app and a browser
UA is `web/src/lib/authPageZoom.test.ts`, against nginx's starting page, which
`tests/test_nginx_starting_page.py` holds to the same script.
"""

import re
from pathlib import Path

from istota.webui import app as webui_app

ZOOM_PIN = Path(__file__).resolve().parent.parent / "web" / "src" / "lib" / "zoomPin.ts"
PLAIN_VIEWPORT = "width=device-width, initial-scale=1"


def viewport(page: str) -> str:
    return re.search(r'<meta name="viewport" content="([^"]+)">', page)[1]


def test_the_locked_viewport_is_the_web_apps():
    source = ZOOM_PIN.read_text()
    found = re.search(r"export const VIEWPORT_LOCKED =\s*'([^']+)';", source)
    assert found, "VIEWPORT_LOCKED not found in zoomPin.ts"
    assert webui_app._VIEWPORT_LOCKED == found[1]


def test_no_element_takes_double_tap_zoom():
    # nginx's starting page copies the login page's stylesheet by hand.
    starting = (ZOOM_PIN.parents[3] / "docker" / "nginx" / "starting.html").read_text()
    for css in (webui_app._LOGIN_PAGE_CSS, starting):
        assert re.search(r"^\* \{[^}]*touch-action: manipulation;", css, re.M)


def test_the_sign_in_sheet_is_locked_server_side():
    # The sheet is the system browser: its User-Agent carries no IstotaApp/.
    page = webui_app._render_form_page("Bot", "Headline", "<p>body</p>", "", sheet=True)
    assert viewport(page) == webui_app._VIEWPORT_LOCKED


def test_a_browser_page_keeps_pinch_zoom():
    page = webui_app._render_form_page("Bot", "Headline", "<p>body</p>", "")
    assert viewport(page) == PLAIN_VIEWPORT


def test_the_app_webview_is_locked_before_first_paint():
    page = webui_app._render_form_page("Bot", "Headline", "<p>body</p>", "")
    head = page.split("</head>", 1)[0]
    assert head.index('name="viewport"') < head.index(f"<script>{webui_app._LOGIN_PAGE_THEME_SCRIPT}</script>")
    assert "IstotaApp" in webui_app._LOGIN_PAGE_THEME_SCRIPT
    assert f"'{webui_app._VIEWPORT_LOCKED}'" in webui_app._LOGIN_PAGE_THEME_SCRIPT
