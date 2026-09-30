"""Real Chromium control for credential insertion after focus or navigation changes.

Run explicitly with the browser image's Patchright version and a local Chrome
executable in ISTOTA_TEST_CHROME. Ordinary unit runs need neither dependency.
"""

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def credential_page():
    executable = os.environ.get("ISTOTA_TEST_CHROME")
    if not executable:
        pytest.skip("set ISTOTA_TEST_CHROME to a local Chrome executable")
    api = pytest.importorskip("patchright.sync_api")
    with api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=executable, headless=True)
        context = browser.new_context()
        context.route("https://portal.example/**", lambda route: route.fulfill(body="""
            <input id="password" type="password"
                   onfocus="document.querySelector('iframe').contentWindow.focus()">
            <iframe src="https://other.example/"></iframe>
            <script>
                window.events = [];
                for (const type of ['input', 'change'])
                    document.querySelector('input').addEventListener(type, () => events.push(type));
            </script>
        """, content_type="text/html"))
        context.route("https://other.example/**", lambda route: route.fulfill(
            body='<input id="other">', content_type="text/html"))
        page = context.new_page()
        page.goto("https://portal.example/")
        yield page
        browser.close()


def test_credential_write_ignores_stolen_cross_origin_focus(credential_page, monkeypatch):
    from tests.test_browser_navigation import browse_api
    page = credential_page
    other = page.frame(url="https://other.example/")
    other.locator("#other").focus()
    monkeypatch.setattr(browse_api, "_credential_values", set())
    result = browse_api._selector_action({}, page, {
        "type": "fill", "selector": "#password", "value": "fixture-password",
        "credential": True, "bound_hosts": ["portal.example"],
    })
    assert result["ok"] is True
    assert other.locator("#other").input_value() == ""
    assert page.locator("#password").input_value() == "fixture-password"
    assert page.evaluate("events") == ["input", "change"]


def test_credential_write_rechecks_origin_in_the_browser(credential_page):
    from tests.test_browser_navigation import browse_api
    page = credential_page
    page.goto("https://other.example/")
    handle = page.query_selector("#other")
    result = handle.evaluate(browse_api._CREDENTIAL_FILL_JS,
                             {"value": "fixture-password", "origin": "https://portal.example"})
    assert result == {"ok": False, "error": "credential_origin_mismatch"}
    assert handle.input_value() == ""


def test_credential_write_refuses_a_replaced_document(credential_page):
    from tests.test_browser_navigation import browse_api
    page = credential_page
    handle = page.query_selector("#password")
    page.evaluate("document.open(); document.write('<input id=password>'); document.close()")
    result = handle.evaluate(browse_api._CREDENTIAL_FILL_JS,
                             {"value": "fixture-password", "origin": "https://portal.example"})
    assert result == {"ok": False, "error": "credential_origin_mismatch"}
    assert page.locator("#password").input_value() == ""
