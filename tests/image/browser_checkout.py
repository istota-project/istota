"""Run inside the browser image against an intercepted, test-only checkout."""

import json
from pathlib import Path
import sys

from patchright.sync_api import TimeoutError, sync_playwright

import browse_api as api


NUMBER = "4242" * 4
CVC = "817"
CHECKOUT = """<html><body><h1>Test checkout</h1><p>Total: USD 24.99</p>
<form id=checkout><input name=number id=number><input name=cvc id=cvc>
<select name=month id=month><option value=0>Month</option><option value=jul> July </option></select>
<select name=year id=year><option value=0>Year</option><option value=29>2029</option></select>
<select id=missing><option value=1>January</option></select>
<button id=pay>Place test order</button></form>
<iframe id=payment src="https://frames.example/card"></iframe>
<p id=echo></p><a id=link href=/receipt>Receipt</a>
<script>
window.events = [];
document.addEventListener('change', e => events.push(e.target.id));
document.querySelector('#checkout').onsubmit = e => {
 e.preventDefault(); window.submitted = Object.fromEntries(new FormData(e.target));
 document.querySelector('#echo').textContent = 'Test order accepted';
};
</script></body></html>"""


def run(output=None):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/usr/bin/google-chrome", headless=True,
            args=["--no-sandbox"],
        )
        context = browser.new_context()
        context.route("https://shop.example/**", lambda r: r.fulfill(
            body=CHECKOUT, content_type="text/html"))
        context.route("https://frames.example/**", lambda r: r.fulfill(
            body='<input id=hosted name=number>', content_type="text/html"))
        page = context.new_page()
        page.goto("https://shop.example/checkout")
        api._credential_values = set()

        def fill(field, selector, value, hosts=("shop.example",)):
            return api._selector_action({}, page, {
                "type": "fill", "selector": selector, "value": value,
                "credential": True, "card_field": field, "bound_hosts": list(hosts),
            })

        try:
            page.wait_for_selector("#hosted", timeout=500)
        except TimeoutError:
            pass
        else:
            raise AssertionError("plain selector reached a cross-origin frame")
        assert fill("number", "#payment>>>#hosted", NUMBER)["error"] == "credential_origin_mismatch"
        hosted = page.frame_locator("#payment").locator("#hosted")
        assert hosted.input_value() == ""
        assert fill("number", "#payment>>>#hosted", NUMBER, ("frames.example",))["ok"]
        assert hosted.input_value() == NUMBER
        assert hosted.evaluate("el => getComputedStyle(el).webkitTextSecurity") == "disc"

        assert fill("number", "#number", NUMBER)["ok"]
        assert fill("cvc", "#cvc", CVC)["ok"]
        assert fill("exp_month", "#month", "07")["ok"]
        assert fill("exp_year", "#year", "2029")["ok"]
        assert page.locator("#month").input_value() == "jul"
        assert page.locator("#year").input_value() == "29"
        assert fill("exp_month", "#missing", "07")["error"] == "credential_option_missing"
        assert page.locator("#missing").input_value() == "1"
        for option_value, text, supplied in [("7", "seven", "07"), ("x", "Jul", "7"), ("07", "seven", "7")]:
            page.locator("#month").evaluate("(el, a) => el.innerHTML = `<option value=${a[0]}>${a[1]}</option>`", [option_value, text])
            assert fill("exp_month", "#month", supplied)["ok"]
            assert page.locator("#month").input_value() == option_value
        page.locator("#year").evaluate("el => el.innerHTML = '<option value=2029>Year</option>'")
        assert fill("exp_year", "#year", "29")["ok"]
        assert page.evaluate("events.includes('month') && events.includes('year')")
        for selector in ("#number", "#cvc"):
            assert page.locator(selector).evaluate("el => el.style.getPropertyPriority('-webkit-text-security')") == "important"
            assert page.locator(selector).evaluate("el => getComputedStyle(el).webkitTextSecurity") == "disc"
        masked = page.screenshot()
        if output:
            Path(output).write_bytes(masked)
        page.locator("#number").evaluate("el => el.style.removeProperty('-webkit-text-security')")
        assert masked != page.screenshot()
        assert fill("number", "#number", NUMBER)["ok"]
        page.locator("#pay").click()
        submitted = page.evaluate("submitted")
        assert submitted["number"] == NUMBER and submitted["cvc"] == CVC
        assert page.locator("#echo").inner_text() == "Test order accepted"

        # Reflect secrets in text, title, links, and a cross-origin frame.
        page.evaluate("a => { document.title=a[0]; document.querySelector('#echo').textContent=a.join(' '); document.querySelector('#link').href='/'+a[0]; }", [NUMBER, CVC])
        hosted.evaluate("(el, value) => {const p=document.createElement('p'); p.textContent=value; document.body.append(p);}", NUMBER)
        api._cleanup_expired = lambda **kwargs: None
        api._get_session = lambda _: {"page": page}
        api._session_page = lambda _: page
        api._request_instance = lambda: None
        api.chrome.connect_cdp = lambda _: None
        api.browsing.detect_captcha = lambda _: False
        for endpoint, path in [(api.browse, "/browse"), (api.render_page, "/render")]:
            for budget in [10000, 8]:
                with api.app.test_request_context(path, method="POST", json={
                    "session_id": "test", "max_chars": budget, "include_frames": True,
                }):
                    result = api.app.make_response(endpoint()).get_json()
                assert result["status"] == "ok", result
                assert NUMBER not in json.dumps(result) and CVC not in json.dumps(result)
                assert NUMBER[:8] not in json.dumps(result)
        with api.app.test_request_context("/health"):
            api.request.user_scope = "alice"
            assert api.health().get_json()["card_fill"] is True
        browser.close()
    print(json.dumps({"checkout": "local test-mode fixture; no payment sent",
                      "plain_selector": "cannot reach cross-origin frame",
                      "frame_locator": "allowed owner origin only",
                      "selects": "month names/numbers and year equivalents passed",
                      "mask": "computed disc important; pixels differ; submission unchanged",
                      "readback": "browse/render redacted before clipping",
                      "order": "test form submitted successfully"}))


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
