"""A credential fill leaves nothing on the page that a password manager would not (ISSUE-1002).

The fill and extract scripts run in node against hand-built elements; the
Python side runs against mocks. The real browser half is
`tests/image/browser_checkout.py`, run inside the browser image.
"""

import io
import json
import shutil
import subprocess
import types
from unittest import mock

import pytest
from PIL import Image

from tests.support.browser_instance import browser_instance  # noqa: F401 -- autouse fixture
from tests.test_browser_navigation import browse_api


_FILL_HARNESS = r'''
const fill = eval(process.argv[1]);
const cases = JSON.parse(process.argv[2]);
const out = [];
for (const c of cases) {
  const log = [];
  const state = new WeakMap();
  class HTMLElement {
    focus() { log.push('focus'); const s = state.get(this); if (s.onfocus) s.onfocus(this, s); }
    blur() { log.push('blur'); }
    dispatchEvent(e) { log.push(e.type + ':' + e.constructor.name); return true; }
    get isConnected() { return state.get(this).connected; }
  }
  class HTMLInputElement extends HTMLElement {}
  class HTMLTextAreaElement extends HTMLElement {}
  class HTMLSelectElement extends HTMLElement {}
  for (const cls of [HTMLInputElement, HTMLSelectElement, HTMLTextAreaElement])
    Object.defineProperty(cls.prototype, 'value', {
      get() { return state.get(this).value; },
      set(v) { log.push('write'); state.get(this).value = v; },
      configurable: true,
    });
  Object.assign(globalThis, {HTMLInputElement, HTMLTextAreaElement, HTMLSelectElement});
  globalThis.document = {location: {origin: 'https://portal.example'}};
  const el = c.tag === 'select' ? new HTMLSelectElement() : new HTMLInputElement();
  const onfocus = {
    detach: (node, s) => { s.connected = false; },
    navigate: () => { document.location = {origin: 'https://evil.example'}; },
    disable: node => { node.disabled = true; },
    rebuild: node => { node.options = [{value: '7', text: 'July', disabled: false, parentElement: {disabled: false}}]; },
    empty: node => { node.options = []; },
  }[c.onfocus];
  state.set(el, {value: '', connected: true, onfocus});
  Object.assign(el, {type: c.type || 'text', disabled: false, readOnly: false,
                     isContentEditable: false, ownerDocument: document,
                     style: {setProperty: () => log.push('style')}});
  if (c.tag === 'select')
    el.options = [{value: 'jul', text: ' July ', disabled: false, parentElement: {disabled: false}}];
  const before = Object.keys(el);
  const result = fill(el, c.args);
  out.push({result, log, value: state.get(el).value,
            added: Object.keys(el).filter(k => !before.includes(k)),
            symbols: Object.getOwnPropertySymbols(el).length});
}
console.log(JSON.stringify(out));
'''


def _run_fill(cases):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed to execute the credential fill script")
    result = subprocess.run([node, "-e", _FILL_HARNESS, browse_api._CREDENTIAL_FILL_JS, json.dumps(cases)],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


ORIGIN = "https://portal.example"
NUMBER = "4242" * 4


class TestTheFillScript:
    def test_a_fill_focuses_writes_and_blurs_with_plain_events(self):
        [password, number, month] = _run_fill([
            {"type": "password", "args": {"value": "fixture-password", "origin": ORIGIN}},
            {"args": {"value": NUMBER, "origin": ORIGIN, "card_field": "number"}},
            {"tag": "select", "args": {"value": "07", "origin": ORIGIN, "card_field": "exp_month"}},
        ])
        for case in (password, number, month):
            assert case["result"] == {"ok": True}
            assert case["log"] == ["focus", "write", "input:Event", "change:Event", "blur"]
        assert password["value"] == "fixture-password"
        assert number["value"] == NUMBER
        assert month["value"] == "jul"

    def test_a_fill_adds_no_property_symbol_or_style_to_the_field(self):
        for case in _run_fill([
            {"type": "password", "args": {"value": "fixture-password", "origin": ORIGIN}},
            {"args": {"value": "api-token", "origin": ORIGIN}},
            {"args": {"value": NUMBER, "origin": ORIGIN, "card_field": "number"}},
            {"args": {"value": "817", "origin": ORIGIN, "card_field": "cvc"}},
            {"tag": "select", "args": {"value": "07", "origin": ORIGIN, "card_field": "exp_month"}},
        ]):
            assert case["result"] == {"ok": True}
            assert case["added"] == []
            assert case["symbols"] == 0
            assert "style" not in case["log"]

    @pytest.mark.parametrize("onfocus, error", [
        ("detach", "credential_origin_mismatch"),
        ("navigate", "credential_origin_mismatch"),
        ("disable", "credential_field_unfillable"),
    ])
    def test_a_focus_handler_that_changes_the_field_stops_the_write(self, onfocus, error):
        [case] = _run_fill([{"type": "password", "onfocus": onfocus,
                             "args": {"value": "fixture-password", "origin": ORIGIN}}])
        assert case["result"] == {"ok": False, "error": error}
        assert "write" not in case["log"]
        assert case["value"] == ""

    def test_a_select_takes_the_option_its_focus_handler_put_there(self):
        [rebuilt, emptied] = _run_fill([
            {"tag": "select", "onfocus": "rebuild",
             "args": {"value": "07", "origin": ORIGIN, "card_field": "exp_month"}},
            {"tag": "select", "onfocus": "empty",
             "args": {"value": "07", "origin": ORIGIN, "card_field": "exp_month"}},
        ])
        assert rebuilt["result"] == {"ok": True} and rebuilt["value"] == "7"
        assert emptied["result"] == {"ok": False, "error": "credential_option_missing"}
        assert emptied["log"] == ["focus", "blur"]


_EXTRACT_HARNESS = r'''
const extract = eval(process.argv[1]);
function field(attrs, props) {
  return {tagName: 'INPUT', innerText: '', innerHTML: '',
          getAttribute: name => attrs[name] ?? null, ...props};
}
const token = field({name: 'token'}, {value: 'new secret'});
const filled = extract(token, [token]);
const plain = extract(token, []);
token.__istotaCredential = true;
const marked = extract(token, []);
console.log(JSON.stringify({filled, plain, marked}));
'''


def test_extract_withholds_a_filled_field_by_identity_not_by_a_mark_on_it():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed to execute the browser extraction script")
    result = subprocess.run([node, "-e", _EXTRACT_HARNESS, browse_api._EXTRACT_ELEMENT_JS],
                            capture_output=True, text=True, check=True)
    entries = json.loads(result.stdout)
    assert "value" not in entries["filled"]
    assert entries["filled"]["value_present"] is True
    # Nothing the page can set on the element decides it either way.
    assert entries["plain"]["value"] == "new secret"
    assert entries["marked"]["value"] == "new secret"


def _filled_page(monkeypatch, url="https://portal.example/login", card_field=None):
    page = mock.Mock()
    handle = page.wait_for_selector.return_value
    handle.owner_frame.return_value = page.main_frame
    page.main_frame.url = url
    handle.evaluate.return_value = {"ok": True}
    monkeypatch.setattr(browse_api, "_credential_values", set())
    monkeypatch.setattr(browse_api, "_filled_fields", {})
    action = {"type": "fill", "selector": "#field", "value": "fixture-secret",
              "credential": True, "bound_hosts": ["portal.example"]}
    if card_field:
        action["card_field"] = card_field
    assert browse_api._selector_action({}, page, action)["ok"] is True
    return page, handle


def _extract(monkeypatch, page, fields):
    page.url = "https://portal.example/login"
    page.is_closed.return_value = False
    page.query_selector_all.return_value = fields
    page.evaluate.return_value = []
    monkeypatch.setattr(browse_api, "_cleanup_expired", lambda **kwargs: None)
    monkeypatch.setattr(browse_api, "_get_session", lambda _: {"page": page})
    monkeypatch.setattr(browse_api.chrome, "connect_cdp", lambda inst: None)
    monkeypatch.setattr(browse_api.pdf_text, "probe", lambda page: (None, None))
    monkeypatch.setattr(browse_api, "request", types.SimpleNamespace(
        get_json=lambda: {"session_id": "s1", "selector": "input"}))
    monkeypatch.setattr(browse_api, "jsonify", lambda value: value)
    return browse_api.extract()


class TestTheFilledFieldsAreKeptHere:
    def test_extract_hands_the_filled_handles_to_the_page_read(self, monkeypatch):
        page, handle = _filled_page(monkeypatch)
        field = mock.Mock()
        field.evaluate.return_value = {"text": "", "html": "", "tag": "input", "value_present": True}
        _extract(monkeypatch, page, [field])
        field.evaluate.assert_called_once_with(browse_api._EXTRACT_ELEMENT_JS, [handle])

    def test_a_handle_whose_document_is_gone_is_dropped(self, monkeypatch):
        page, handle = _filled_page(monkeypatch)
        handle.evaluate.side_effect = RuntimeError("Execution context was destroyed")
        field = mock.Mock()
        field.evaluate.return_value = {"text": "", "html": "", "tag": "input"}
        _extract(monkeypatch, page, [field])
        field.evaluate.assert_called_once_with(browse_api._EXTRACT_ELEMENT_JS, [])
        assert browse_api._filled_fields[page] == []
        handle.dispose.assert_called_once()

    def test_a_handle_in_another_frame_is_not_handed_to_the_main_frame(self, monkeypatch):
        page, handle = _filled_page(monkeypatch)
        browse_api._filled_fields[page][0] = browse_api.FilledField(
            frame=mock.Mock(), handle=handle, masked=False)
        field = mock.Mock()
        field.evaluate.return_value = {"text": "", "html": "", "tag": "input"}
        _extract(monkeypatch, page, [field])
        field.evaluate.assert_called_once_with(browse_api._EXTRACT_ELEMENT_JS, [])

    def test_only_the_card_number_and_cvc_are_masked(self, monkeypatch):
        for card_field, masked in [(None, False), ("number", True), ("cvc", True),
                                   ("exp_month", False), ("name", False)]:
            page, _ = _filled_page(monkeypatch, card_field=card_field)
            assert [f.masked for f in browse_api._filled_fields[page]] == [masked]

    def test_the_record_per_page_is_bounded(self, monkeypatch):
        page, handle = _filled_page(monkeypatch)
        for _ in range(browse_api.FILLED_FIELDS_MAX + 5):
            browse_api._remember_filled(page, page.main_frame, mock.Mock(), False)
        assert len(browse_api._filled_fields[page]) == browse_api.FILLED_FIELDS_MAX
        handle.dispose.assert_called_once()

    def test_closing_the_tab_forgets_its_fields(self, monkeypatch):
        page, handle = _filled_page(monkeypatch)
        context = mock.Mock(pages=[page, mock.Mock()])
        browse_api._close_page(page, context)
        assert page not in browse_api._filled_fields
        handle.dispose.assert_called_once()
        page.close.assert_called_once()

    def test_a_page_that_has_gone_is_forgotten_at_the_next_fill(self, monkeypatch):
        gone, handle = _filled_page(monkeypatch)
        live = mock.Mock()
        monkeypatch.setattr(browse_api, "_page_is_gone", lambda page: page is gone)
        browse_api._remember_filled(live, live.main_frame, mock.Mock(), False)
        assert list(browse_api._filled_fields) == [live]
        handle.dispose.assert_called_once()


def _png(width, height):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def _card_page(monkeypatch, box, dpr=1, scroll=(0, 0)):
    page, handle = _filled_page(monkeypatch, card_field="number")
    handle.bounding_box.return_value = box
    page.screenshot.return_value = _png(200, 120)
    monkeypatch.setattr(browse_api.visual, "page_state", lambda page: {
        "viewport": [200 // dpr, 120 // dpr], "dpr": dpr, "scroll": list(scroll), "url": ""})
    return page, handle


def _pixel(png, x, y):
    return Image.open(io.BytesIO(png)).convert("RGB").getpixel((x, y))


class TestTheScreenshotMasksCardFields:
    def test_the_card_number_box_is_painted_over(self, monkeypatch):
        page, _ = _card_page(monkeypatch, {"x": 20, "y": 30, "width": 60, "height": 20})
        png = browse_api._masked_screenshot(page, full_page=False)
        assert _pixel(png, 50, 40) == browse_api.CARD_MASK_COLOR
        assert _pixel(png, 150, 100) == (255, 255, 255)
        page.screenshot.assert_called_once_with(full_page=False)

    def test_the_box_is_scaled_by_the_pixel_ratio(self, monkeypatch):
        page, _ = _card_page(monkeypatch, {"x": 10, "y": 15, "width": 30, "height": 10}, dpr=2)
        png = browse_api._masked_screenshot(page, full_page=False)
        assert _pixel(png, 75, 45) == browse_api.CARD_MASK_COLOR
        assert _pixel(png, 30, 20) == (255, 255, 255)

    def test_a_full_page_capture_offsets_the_box_by_the_scroll(self, monkeypatch):
        page, _ = _card_page(monkeypatch, {"x": 20, "y": 10, "width": 60, "height": 20}, scroll=(0, 50))
        png = browse_api._masked_screenshot(page, full_page=True)
        assert _pixel(png, 50, 70) == browse_api.CARD_MASK_COLOR
        assert _pixel(png, 50, 20) == (255, 255, 255)

    def test_a_capture_it_cannot_place_the_mask_on_is_refused(self, monkeypatch):
        page, _ = _card_page(monkeypatch, {"x": 20, "y": 30, "width": 60, "height": 20})
        monkeypatch.setattr(browse_api.visual, "page_state", lambda page: None)
        with pytest.raises(RuntimeError, match="card field mask"):
            browse_api._masked_screenshot(page, full_page=False)

    def test_a_page_with_no_card_fill_is_returned_untouched(self, monkeypatch):
        page, handle = _filled_page(monkeypatch)
        page.screenshot.return_value = raw = _png(200, 120)
        assert browse_api._masked_screenshot(page, full_page=False) is raw
        handle.bounding_box.assert_not_called()

    def test_a_field_that_moved_during_the_capture_is_masked_at_both_places(self, monkeypatch):
        page, handle = _card_page(monkeypatch, None)
        handle.bounding_box.side_effect = [{"x": 0, "y": 0, "width": 40, "height": 20},
                                           {"x": 100, "y": 80, "width": 40, "height": 20}]
        png = browse_api._masked_screenshot(page, full_page=False)
        assert _pixel(png, 20, 10) == browse_api.CARD_MASK_COLOR
        assert _pixel(png, 120, 90) == browse_api.CARD_MASK_COLOR

    def test_the_endpoint_returns_the_masked_capture(self, monkeypatch):
        page, _ = _card_page(monkeypatch, {"x": 20, "y": 30, "width": 60, "height": 20})
        page.is_closed.return_value = False
        sent = {}
        monkeypatch.setattr(browse_api, "_cleanup_expired", lambda **kwargs: None)
        monkeypatch.setattr(browse_api, "_get_session", lambda _: {"page": page})
        monkeypatch.setattr(browse_api.chrome, "connect_cdp", lambda inst: None)
        monkeypatch.setattr(browse_api, "_capture_foreground", lambda page: None)
        monkeypatch.setattr(browse_api, "_foreground_headers", lambda verdict: {})
        monkeypatch.setattr(browse_api.visual, "build_capture", lambda *a, **k: (None, "no display"))
        monkeypatch.setattr(browse_api, "request", types.SimpleNamespace(
            get_json=lambda: {"session_id": "s1"}))
        monkeypatch.setattr(browse_api, "Response", lambda body, **kw: sent.setdefault("body", body))
        browse_api.screenshot()
        assert _pixel(sent["body"], 50, 40) == browse_api.CARD_MASK_COLOR


def test_the_image_pins_the_pillow_the_lock_resolves():
    from pathlib import Path
    import re
    root = Path(__file__).resolve().parent.parent
    pinned = re.search(r"^Pillow==(\S+)$", (root / "docker/browser/requirements.txt").read_text(), re.M)
    locked = re.search(r'^name = "pillow"\nversion = "([^"]+)"', (root / "uv.lock").read_text(), re.M)
    assert pinned and locked and pinned.group(1) == locked.group(1)
