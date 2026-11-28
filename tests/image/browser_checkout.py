"""Run inside the browser image against an intercepted, test-only checkout."""

import json
import io
from pathlib import Path
import sys

from PIL import Image

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
<input id=token><input id=plain><input id=removed onfocus="this.remove()">
<script>
window.events = [];
document.addEventListener('change', e => events.push(e.target.id));
window.focused = [];
document.addEventListener('focusin', e => focused.push(e.target.id));
document.addEventListener('focusout', e => focused.push('-' + e.target.id));
window.traces = id => {
 const el = document.getElementById(id);
 return {keys: Object.keys(el).filter(key => !/^[0-9]+$/.test(key)), symbols: Object.getOwnPropertySymbols(el).length,
         style: el.getAttribute('style'), security: getComputedStyle(el).webkitTextSecurity};
};
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
        api._cleanup_expired = lambda **kwargs: None
        api._get_session = lambda _: {"page": page}
        api._session_page = lambda _: page
        api._request_instance = lambda: None
        api.chrome.connect_cdp = lambda _: None
        api.browsing.detect_captcha = lambda _: False
        api._foreground_tabs = lambda _: ([], [])

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
        assert hosted.evaluate("el => [Object.keys(el), el.getAttribute('style')]") == [[], None]

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
        # Nothing a page script can read says how the fields were filled.
        for field in ("number", "cvc", "month", "year"):
            assert page.evaluate(f"traces({field!r})") == {
                "keys": [], "symbols": 0, "style": None, "security": "none"}, field
        # The hosted frame's fill moves focus through #payment too, so filter.
        assert page.evaluate("focused.filter(id => /number|cvc/.test(id))") == [
            "number", "-number", "cvc", "-cvc"]
        masked = api._masked_screenshot(page, full_page=False)
        if output:
            Path(output).write_bytes(masked)
        image = Image.open(io.BytesIO(masked)).convert("RGB")
        raw = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
        scale = image.width / page.evaluate("innerWidth")
        for selector in ("#number", "#cvc"):
            box = page.locator(selector).bounding_box()
            centre = (int((box["x"] + box["width"] / 2) * scale), int((box["y"] + box["height"] / 2) * scale))
            assert image.getpixel(centre) == api.CARD_MASK_COLOR, selector
            assert raw.getpixel(centre) != api.CARD_MASK_COLOR, selector

        # A card field in a fixed container, on a scrolled page, in a full-page capture.
        page.evaluate("""() => {
         const spacer = document.createElement('div'); spacer.id = 'spacer';
         spacer.style.height = '3000px'; document.body.append(spacer);
         const modal = document.createElement('div'); modal.id = 'modal';
         modal.style.cssText = 'position: fixed; top: 100px; left: 50px';
         modal.innerHTML = '<input id=fixed style="background: #f00; color: #f00; border: 0; width: 200px; height: 30px">';
         document.body.append(modal); scrollTo(0, 500);
        }""")
        assert fill("number", "#fixed", NUMBER)["ok"]
        def red(png):
            data = Image.open(io.BytesIO(png)).convert("RGB").tobytes()
            return sum(1 for i in range(0, len(data), 3)
                       if data[i] > 200 and data[i + 1] < 60 and data[i + 2] < 60)
        assert red(page.screenshot(full_page=True)) > 0
        assert red(api._masked_screenshot(page, full_page=True)) == 0
        page.evaluate("() => { document.querySelector('#spacer').remove(); document.querySelector('#modal').remove(); scrollTo(0, 0); }")

        # extract withholds a filled field by its handle, whatever the page did to the node.
        assert fill(None, "#token", "api-token")["ok"]
        page.locator("#plain").fill("typed")
        page.evaluate("""() => {
         const el = document.querySelector('#token');
         el.__istotaCredential = false;
         for (const key of Object.keys(el)) delete el[key];
        }""")
        values, api._credential_values = api._credential_values, set()
        with api.app.test_request_context("/extract", method="POST", json={
            "session_id": "abc7d07e", "selector": "#token, #plain",
        }):
            extracted = api.app.make_response(api.extract()).get_json()
        api._credential_values = values
        entries = {entry["id"]: entry for entry in extracted["elements"]}
        assert "value" not in entries["token"] and entries["token"]["value_present"], extracted
        assert entries["plain"]["value"] == "typed", extracted
        # A focus handler that removes the field stops the write.
        removed = page.query_selector("#removed")
        assert fill(None, "#removed", "fixture-password")["error"] == "credential_origin_mismatch"
        assert removed.evaluate("el => el.value") == ""

        assert fill("number", "#number", NUMBER)["ok"]
        with api.app.test_request_context("/interact", method="POST", json={
            "session_id": "abc7d07e", "actions": [
                {"type": "fill", "selector": "#missing", "value": "07", "credential": True,
                 "card_field": "exp_month", "bound_hosts": ["shop.example"]},
                {"type": "click", "selector": "#pay"},
            ],
        }):
            refused = api.app.make_response(api.interact()).get_json()
        assert refused["status"] == "error" and refused["actions_not_run"] == 1
        assert refused["session_id"] == "abc7d07e"
        assert page.evaluate("window.submitted === undefined")
        page.locator("#pay").click()
        submitted = page.evaluate("submitted")
        assert submitted["number"] == NUMBER and submitted["cvc"] == CVC
        assert page.locator("#echo").inner_text() == "Test order accepted"

        # Reflect secrets in text, title, links, and a cross-origin frame.
        page.evaluate("a => { document.title=a[0]; document.querySelector('#echo').textContent=a.join(' '); document.querySelector('#link').href='/'+a[0]; }", [NUMBER, CVC])
        hosted.evaluate("(el, value) => {const p=document.createElement('p'); p.textContent=value; document.body.append(p);}", NUMBER)
        for endpoint, path in [(api.browse, "/browse"), (api.render_page, "/render")]:
            for budget in [10000, 8]:
                with api.app.test_request_context(path, method="POST", json={
                    "session_id": "abc7d07e", "max_chars": budget, "include_frames": True,
                }):
                    result = api.app.make_response(endpoint()).get_json()
                assert result["status"] == "ok", result
                assert result["session_id"] == "abc7d07e"
                assert NUMBER not in json.dumps(result) and CVC not in json.dumps(result)
                assert NUMBER[:8] not in json.dumps(result)
        page.set_content("<p>42424242<span>42424242</span></p>")
        with api.app.test_request_context("/render", method="POST", json={
            "session_id": "abc7d07e", "max_chars": 8,
        }):
            result = api.app.make_response(api.render_page()).get_json()
        assert result["markdown"] == "[REDACTE", result
        with api.app.test_request_context("/health"):
            api.request.user_scope = "alice"
            assert api.health().get_json()["card_fill"] is True
        browser.close()
    print(json.dumps({"checkout": "local test-mode fixture; no payment sent",
                      "plain_selector": "cannot reach cross-origin frame",
                      "frame_locator": "allowed owner origin only",
                      "selects": "month names/numbers and year equivalents passed",
                      "mask": "no property, symbol or style on the fields; screenshot boxes painted; submission unchanged",
                      "traces": "focus and blur around each fill; extract withholds by handle; a removed field is not written",
                      "readback": "browse/render redacted before clipping",
                      "order": "test form submitted successfully"}))


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
