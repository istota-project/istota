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


@pytest.mark.parametrize("shadow", [False, True])
def test_capture_from_frame_and_open_shadow(credential_page, monkeypatch, shadow):
    from tests.test_browser_navigation import browse_api
    page = credential_page
    monkeypatch.setattr(browse_api, "_credential_values", set())
    if shadow:
        page.evaluate("""() => {
            const host = document.createElement('div'); document.body.append(host);
            host.attachShadow({mode: 'open'}).innerHTML = '<input id="seed" value="JBSWY3DPEHPK3PXP">';
        }""")
        selector, hosts = "#seed::value", ["portal.example"]
    else:
        page.frame(url="https://other.example/").locator("#other").fill("JBSWY3DPEHPK3PXP")
        selector, hosts = "iframe>>>#other::value", ["other.example"]
    result, text = browse_api._read_secret_action(page, {"selector": selector, "kind": "otp", "bound_hosts": hosts})
    assert result["ok"] and text == "JBSWY3DPEHPK3PXP"
    assert text in browse_api._credential_values
    if not shadow:
        result, text = browse_api._read_secret_action(page, {"selector": selector, "kind": "otp", "bound_hosts": ["portal.example"]})
        assert result["error"] == "credential_origin_mismatch" and not text


def test_capture_text_download(credential_page, monkeypatch):
    from tests.test_browser_navigation import browse_api
    page = credential_page
    page.context.route("https://portal.example/codes.txt", lambda route: route.fulfill(
        body="abcd-1234\nefgh-5678", content_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="codes.txt"'}))
    page.evaluate("""() => {
        document.body.innerHTML = '<section><h2>Recovery codes</h2><a href="/codes.txt" download>Download</a></section>';
    }""")
    monkeypatch.setattr(browse_api, "_credential_values", set())
    candidates = browse_api._find_secrets(page)
    assert len(candidates) == 1 and candidates[0]["download"]
    result, text = browse_api._read_secret_action(page, {
        "type": "read_secret_download", "selector": "auto", "kind": "codes",
        "bound_hosts": ["portal.example"],
    })
    assert result["ok"] and text == "abcd-1234\nefgh-5678"
    assert "abcd-1234" in browse_api._credential_values


@pytest.mark.parametrize("shadow", [False, True])
def test_find_and_auto_capture_frame_or_shadow(credential_page, monkeypatch, shadow):
    from tests.test_browser_navigation import browse_api
    page = credential_page
    if shadow:
        page.evaluate("""() => {
            const host = document.createElement('div'); document.body.append(host);
            host.attachShadow({mode: 'open'}).innerHTML = '<input value="JBSWY3DPEHPK3PXP">';
        }""")
        host = "portal.example"
    else:
        page.frame(url="https://other.example/").locator("#other").fill("JBSWY3DPEHPK3PXP")
        host = "other.example"
    monkeypatch.setattr(browse_api, "_credential_values", set())
    candidates = browse_api._find_secrets(page)
    assert len(candidates) == 1 and candidates[0]["kind_guess"] == "otp"
    assert "JBSW" not in str(candidates)
    result, text = browse_api._read_secret_action(page, {
        "selector": "auto", "kind": "otp", "bound_hosts": [host],
    })
    assert result["ok"] and text == "JBSWY3DPEHPK3PXP"
