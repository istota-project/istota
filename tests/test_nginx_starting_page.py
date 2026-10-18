"""The page the stack's nginx shows while the web service is restarting.

nginx serves `docker/nginx/starting.html` itself, so the page can reach nothing
the app serves: the sigil is inlined and the colour tokens are copied from the
login page. These tests hold both copies to their sources. What nginx does with
the page is `tests/image/test_nginx_starting_page.py`.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

from istota.webui import app as webui_app

REPO = Path(__file__).resolve().parent.parent
PAGE = REPO / "docker" / "nginx" / "starting.html"
TEMPLATE = REPO / "docker" / "nginx" / "istota.conf.template"
SIGIL = REPO / "web" / "static" / "octopus-sigil.webp"

ROOT_BLOCKS = re.compile(r"(:root(?:\[data-theme='light'\])?\s*\{[^}]*\})")


def _locations(conf: str) -> dict[str, str]:
    """Each `location` block's body, keyed by its match text."""
    blocks = {}
    for match in re.finditer(r"^location\s+([^{]+?)\s*\{(.*?)^\}", conf, re.M | re.S):
        blocks[match.group(1)] = match.group(2)
    return blocks


def test_the_inlined_sigil_is_the_apps_sigil():
    page = PAGE.read_text()
    found = re.search(r'src="data:image/webp;base64,([A-Za-z0-9+/=]+)"', page)
    assert found, "no inlined sigil"
    assert base64.b64decode(found.group(1)) == SIGIL.read_bytes()


def test_the_colour_tokens_are_the_login_pages():
    page_tokens = ROOT_BLOCKS.findall(PAGE.read_text())
    login_tokens = ROOT_BLOCKS.findall(webui_app._LOGIN_PAGE_CSS)
    assert len(login_tokens) == 2
    assert page_tokens == login_tokens


def test_the_theme_script_is_the_login_pages():
    assert f"<script>{webui_app._LOGIN_PAGE_THEME_SCRIPT}</script>" in PAGE.read_text()


def test_the_page_loads_nothing_from_elsewhere():
    page = PAGE.read_text()
    assert not re.search(r'(?:src|href)="/', page)
    assert "<link" not in page


# The sign-in pages carved out of /istota/ for their body cap (ISSUE-777) are
# still web UI pages, and the Mac app opens /istota/auth/local before `web` is up.
WEB_UI_LOCATIONS = {"/istota/", "/istota/auth/", "= /istota/login/email"}
# Under /istota/ but not pages: the chat stream is an API, and set-password's
# access log is off, which an internal redirect to the page would undo.
NOT_WEB_UI_PAGES = {"/istota/api/chat/", "= /istota/auth/set-password"}


def test_only_the_web_ui_falls_back_to_the_page():
    locations = _locations(TEMPLATE.read_text())
    for name, body in locations.items():
        if name in WEB_UI_LOCATIONS:
            assert "error_page 502 =503 /_istota_starting;" in body, name
        else:
            assert "error_page" not in body, f"{name} must keep nginx's plain error"


def test_every_web_ui_location_keeps_the_page_and_relative_redirects():
    """A new carve-out of /istota/ must say which of the two lists it is on."""
    locations = _locations(TEMPLATE.read_text())
    under = {name for name, body in locations.items()
             if name.removeprefix("= ").startswith("/istota/") and "proxy_pass http://$upstream_web;" in body}
    assert under == WEB_UI_LOCATIONS | NOT_WEB_UI_PAGES
    for name in WEB_UI_LOCATIONS:
        assert "error_page 502 =503 /_istota_starting;" in locations[name], name
        assert "absolute_redirect off;" in locations[name], name


def test_the_page_location_is_internal_and_marks_its_answer():
    body = _locations(TEMPLATE.read_text())["= /_istota_starting"]
    assert "internal;" in body
    assert "alias /etc/istota-nginx/starting.html;" in body
    assert "X-Istota-Upstream" in body
    assert "Retry-After" in body
    # add_header here replaces the server level's, so the robots header is repeated.
    assert 'X-Robots-Tag "noindex, nofollow" always;' in body


def test_the_poll_reloads_on_the_marker_nginx_sets():
    assert "res.headers.get('X-Istota-Upstream')" in PAGE.read_text()
