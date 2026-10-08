"""Navigation must not return a previous document as a successful fetch."""

import sys
import itertools
import types
from pathlib import Path
from unittest import mock

import pytest

from tests.support.browser_instance import browser_instance  # noqa: F401 -- autouse fixture

# Stub patchright before importing chrome -- chrome does
# `from patchright.sync_api import sync_playwright` at module top.
if "patchright" not in sys.modules:
    _patchright = types.ModuleType("patchright")
    _sync_api = types.ModuleType("patchright.sync_api")
    _sync_api.sync_playwright = mock.MagicMock(name="sync_playwright")
    _patchright.sync_api = _sync_api
    sys.modules["patchright"] = _patchright
    sys.modules["patchright.sync_api"] = _sync_api

# Stub markdownify -- browse_api imports render, which subclasses MarkdownConverter.
if "markdownify" not in sys.modules:
    _markdownify = types.ModuleType("markdownify")

    class _StubConverter:
        def __init__(self, **options):
            self.options = options

        def convert_hN(self, *a, **k):  # pragma: no cover - never converts here
            raise AssertionError("stub converter used for a real conversion")

    _markdownify.MarkdownConverter = _StubConverter
    sys.modules["markdownify"] = _markdownify

# Stub flask -- browse_api builds an app and registers routes at import time.
# Endpoint tests replace request and jsonify; no HTTP server runs here.
if "flask" not in sys.modules:
    _flask = types.ModuleType("flask")

    class _StubFlask:
        def __init__(self, *_a, **_k):
            pass

        def route(self, *_a, **_k):
            return lambda fn: fn

        def before_request(self, fn):
            return fn

        def teardown_request(self, fn):
            return fn

        def after_request(self, fn):
            return fn

    _flask.Flask = _StubFlask
    _flask.Response = type("Response", (), {})
    _flask.jsonify = lambda *a, **k: dict(*a, **k)
    _flask.request = types.SimpleNamespace()
    sys.modules["flask"] = _flask

_BROWSER_DIR = Path(__file__).resolve().parent.parent / "docker" / "browser"
if str(_BROWSER_DIR) not in sys.path:
    sys.path.insert(0, str(_BROWSER_DIR))

import chrome  # noqa: E402  (import after the stubs + path insert)

# Scope the skip to the one dependency that is genuinely optional here. bs4
# reaches the env transitively (yfinance, via the `markets` extra), so it can be
# absent. Skipping on `browse_api` itself would also swallow a syntax error or a
# new import in the module under test and report the whole file as skipped.
pytest.importorskip("bs4", reason="browser render module needs bs4")

import browse_api  # noqa: E402  (import after the stubs + path insert)



REQUESTED = "https://news.example/advisories/msg00411.html"
OLD = "https://news.example/advisories/msg00417.html"


class Page:
    def __init__(self):
        self.url = OLD
        self.main_frame = types.SimpleNamespace(url=OLD)
        self.listeners = {}
        self.pending = []
        self.fronted = 0

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.listeners[event].remove(callback)

    def bring_to_front(self):
        self.fronted += 1

    def wait_for_timeout(self, milliseconds):
        for event, value in self.pending:
            if event == "framenavigated":
                self.url = value
                self.main_frame.url = value
                value = self.main_frame
            for callback in self.listeners.get(event, []):
                callback(value)
        self.pending.clear()

    def navigation(self, url, redirected_from=None, frame=None):
        req = types.SimpleNamespace(
            url=url, redirected_from=redirected_from,
            frame=frame or self.main_frame, is_navigation_request=lambda: True,
        )
        self.pending.append(("request", req))
        return req

    def commit(self, url):
        self.pending.append(("framenavigated", url))


@pytest.fixture
def navigation(monkeypatch):
    page = Page()
    monkeypatch.setattr(chrome, "connect_cdp", lambda inst: None)
    monkeypatch.setattr(chrome, "get_context", lambda inst: types.SimpleNamespace(pages=[page]))
    ticks = itertools.count()
    monkeypatch.setattr(browse_api, "time", types.SimpleNamespace(
        sleep=lambda _: None, monotonic=lambda: next(ticks),
    ))
    monkeypatch.setattr(browse_api.xdotool, "wait_for_challenges", lambda **kw: None)
    navigate = mock.Mock()
    monkeypatch.setattr(browse_api.xdotool, "navigate", navigate)
    return page, navigate


@pytest.mark.parametrize("endpoint", ["browse", "render_page", "screenshot", "extract"])
def test_wrong_document_is_an_error_at_each_endpoint(navigation, monkeypatch, endpoint):
    page, navigate = navigation
    monkeypatch.setattr(browse_api, "request", types.SimpleNamespace(
        get_json=lambda: {"url": REQUESTED, "skip_behavior": True},
    ))
    monkeypatch.setattr(browse_api, "jsonify", lambda value: value)
    monkeypatch.setattr(browse_api, "_cleanup_expired", lambda **kwargs: None)
    monkeypatch.setattr(browse_api, "_create_session", lambda **kwargs: ("session", page))
    monkeypatch.setattr(browse_api, "_page_is_gone", lambda p: False)
    close = mock.Mock()
    monkeypatch.setattr(browse_api, "_close_session", close)
    monkeypatch.setattr(browse_api.browsing, "wait_for_datadome", lambda p: None)
    monkeypatch.setattr(browse_api.browsing, "simulate_human_behavior", lambda p, *, display: None)
    monkeypatch.setattr(browse_api.browsing, "detect_captcha", lambda p: False)
    extract = mock.Mock(return_value={"url": OLD, "text": "Old advisory"})
    monkeypatch.setattr(browse_api.browsing, "extract_page_content", extract)

    result = getattr(browse_api, endpoint)()

    assert result == ({
        "status": "error", "error": "navigation_mismatch",
        "requested": REQUESTED, "landed": OLD,
    }, 502)
    assert navigate.call_count == 2
    extract.assert_not_called()
    close.assert_called_once_with("session")
    assert not any(page.listeners.values())


def test_retry_can_recover(navigation):
    page, navigate = navigation

    def drive(*args, **kwargs):
        if navigate.call_count == 2:
            page.navigation(REQUESTED)
            page.commit(REQUESTED)

    navigate.side_effect = drive
    assert browse_api._navigate_and_wait(page, REQUESTED) is None
    assert page.url == REQUESTED
    assert navigate.call_count == 2
    assert page.fronted == 2
    assert not any(page.listeners.values())


@pytest.mark.parametrize("redirect", ["http", "client", "none"])
def test_navigation_and_observed_redirects_are_allowed(navigation, redirect):
    page, navigate = navigation
    destination = "https://archive.example/advisory" if redirect != "none" else REQUESTED

    def drive(*args, **kwargs):
        requested = page.navigation(REQUESTED)
        if redirect == "client":
            page.commit(REQUESTED)
            page.navigation(destination)
        elif redirect == "http":
            page.navigation(destination, redirected_from=requested)
        page.commit(destination)

    navigate.side_effect = drive
    assert browse_api._navigate_and_wait(page, REQUESTED) is None
    assert page.url == destination
    assert navigate.call_count == 1
    assert not any(page.listeners.values())


def test_an_uncommitted_request_does_not_legitimize_the_old_page(navigation):
    page, navigate = navigation
    navigate.side_effect = lambda *a, **kw: page.navigation(REQUESTED)
    with pytest.raises(RuntimeError, match="navigation_mismatch"):
        browse_api._navigate_and_wait(page, REQUESTED)
    assert navigate.call_count == 2


@pytest.mark.parametrize("wrong", [OLD, "https://other.example/", REQUESTED + "?page=2"])
def test_an_iframe_request_does_not_legitimize_a_wrong_navigation(navigation, wrong):
    page, navigate = navigation

    def drive(*a, **kw):
        page.navigation(REQUESTED, frame=object())
        page.navigation(wrong)
        page.commit(wrong)

    navigate.side_effect = drive
    with pytest.raises(RuntimeError, match="navigation_mismatch"):
        browse_api._navigate_and_wait(page, REQUESTED)


def test_challenge_does_not_retry_or_read_the_page(navigation, monkeypatch):
    page, navigate = navigation
    monkeypatch.setattr(browse_api.xdotool, "wait_for_challenges", lambda **kw: "just a moment")
    page.wait_for_timeout = mock.Mock()
    assert browse_api._navigate_and_wait(page, REQUESTED) == "just a moment"
    page.wait_for_timeout.assert_called_once_with(1)
    assert navigate.call_count == 1
    assert not any(page.listeners.values())


@pytest.mark.parametrize(("requested", "landed"), [
    ("https://news.example", "https://news.example/"),
    ("news.example", "https://news.example/"),
    ("news.example", "http://news.example/"),
    ("http://news.example/", "https://news.example/"),
    ("https://NEWS.example:443/a#section", "https://news.example/a"),
    ("https://news.example/café", "https://news.example/caf%C3%A9"),
    ("https://news.example/advisories/../article", "https://news.example/article"),
    ("https://news.example/a/./b", "https://news.example/a/b"),
    ("https://news.example/a/%2E%2e/b", "https://news.example/b"),
    ("https://news.example/a/.%2e/b", "https://news.example/b"),
    ("https://news.example/a/..", "https://news.example/"),
    ("https://news.example/a/.", "https://news.example/a/"),
    ("https://news.example/../../b", "https://news.example/b"),
    ("https://news.example/a//../b", "https://news.example/a/b"),
])
def test_browser_url_spelling_and_upgrade(navigation, requested, landed):
    page, navigate = navigation
    navigate.side_effect = lambda *a, **kw: page.commit(landed)
    assert browse_api._navigate_and_wait(page, requested) is None
    assert page.url == landed
    assert navigate.call_count == 1


def test_stale_matching_url_without_a_commit_is_not_success(navigation):
    page, navigate = navigation
    page.url = REQUESTED
    with pytest.raises(RuntimeError, match="navigation_mismatch"):
        browse_api._navigate_and_wait(page, REQUESTED)
    assert navigate.call_count == 2


def test_no_navigation_from_blank_fails_cleanly(navigation):
    page, navigate = navigation
    page.url = "about:blank"
    with pytest.raises(RuntimeError, match="navigation_mismatch"):
        browse_api._navigate_and_wait(page, REQUESTED)
    assert navigate.call_count == 2


def test_a_slow_commit_is_waited_for(navigation):
    page, navigate = navigation
    dispatch = page.wait_for_timeout
    waits = []

    def pump(ms):
        waits.append(ms)
        if waits.count(100) == 2:
            page.commit(REQUESTED)
        dispatch(ms)

    page.wait_for_timeout = pump
    assert browse_api._navigate_and_wait(page, REQUESTED) is None
    assert waits.count(100) == 2
    assert navigate.call_count == 1


@pytest.mark.parametrize(("requested", "landed"), [
    ("https://news.example/a//b", "https://news.example/a/b"),
    ("https://news.example/a%2Fb", "https://news.example/a/b"),
    ("https://news.example/a/.../b", "https://news.example/a/b"),
])
def test_path_normalization_does_not_merge_distinct_documents(navigation, requested, landed):
    page, navigate = navigation
    navigate.side_effect = lambda *a, **kw: page.commit(landed)
    with pytest.raises(RuntimeError, match="navigation_mismatch"):
        browse_api._navigate_and_wait(page, requested)
    assert navigate.call_count == 2


def test_extract_keeps_textless_controls_and_scrubs_before_truncation(monkeypatch):
    page = mock.Mock()
    page.url = 'https://example.com/'
    page.is_closed.return_value = False
    field = mock.Mock()
    field.inner_text.return_value = ''
    field.evaluate.return_value = {
        'text': 'Search', 'html': '<input value="vault&amp;secret">',
        'tag': 'input', 'type': 'text', 'value': 'vault&secret',
        'value_present': True, 'name': 'query', 'checked': False,
    }
    page.query_selector_all.return_value = [field]
    page.evaluate.return_value = []
    monkeypatch.setattr(browse_api, '_credential_values', {'vault&secret'}, raising=False)
    monkeypatch.setattr(browse_api, '_cleanup_expired', lambda **kwargs: None)
    monkeypatch.setattr(browse_api, '_get_session', lambda _: {'page': page})
    monkeypatch.setattr(browse_api.chrome, 'connect_cdp', lambda inst: None)
    monkeypatch.setattr(browse_api, 'request', types.SimpleNamespace(
        get_json=lambda: {'session_id': 's1', 'selector': 'input', 'max_chars': 25}))
    monkeypatch.setattr(browse_api, 'jsonify', lambda value: value)
    result = browse_api.extract()
    assert result['count'] == 1
    entry = result['elements'][0]
    assert entry['text'] == 'Search'
    assert 'value' not in entry
    assert entry['value_present'] is True
    assert entry['checked'] is False
    assert 'vault' not in str(result)


@pytest.mark.parametrize('passwords, entry, expected', [
    ([], {'text': '', 'html': '', 'tag': 'input', 'value': 'typed query',
          'checked': True}, {'value': 'typed query', 'checked': True}),
    (['secret', 'default-secret'],
     {'text': 'secret', 'html': '<input value="default-secret">',
      'value_present': True, 'type': 'password'},
     {'text': '[REDACTED]', 'html': '<input value="[REDACTED]">',
      'value_present': True}),
    (["a\"b'c"],
     {'text': '', 'html': "<input type=\"password\" value=\"a&quot;b'c\">"},
     {'html': '<input type="password" value="[REDACTED]">'}),
    ([], {'text': 'Photo', 'html': '', 'src': '/photo.png', 'alt': 'Photo'},
     {'src': '/photo.png', 'alt': 'Photo'}),
])
def test_extract_live_state_and_password_reflections(monkeypatch, passwords, entry, expected):
    page = mock.Mock()
    page.url = 'https://example.com/'
    page.is_closed.return_value = False
    field = mock.Mock()
    field.evaluate.return_value = entry
    page.query_selector_all.return_value = [field]
    page.evaluate.return_value = passwords
    monkeypatch.setattr(browse_api, '_credential_values', set())
    monkeypatch.setattr(browse_api, '_cleanup_expired', lambda **kwargs: None)
    monkeypatch.setattr(browse_api, '_get_session', lambda _: {'page': page})
    monkeypatch.setattr(browse_api.chrome, 'connect_cdp', lambda inst: None)
    monkeypatch.setattr(browse_api, 'request', types.SimpleNamespace(
        get_json=lambda: {'session_id': 's1', 'selector': '*'}))
    monkeypatch.setattr(browse_api, 'jsonify', lambda value: value)
    result = browse_api.extract()
    assert result['count'] == 1
    for key, value in expected.items():
        assert result['elements'][0][key] == value
    field.evaluate.assert_called_once_with(browse_api._EXTRACT_ELEMENT_JS)


def test_credential_fill_registers_before_failed_evaluation(monkeypatch):
    page = mock.Mock()
    page.wait_for_selector.return_value.evaluate.side_effect = RuntimeError('input failed')
    page.wait_for_selector.return_value.owner_frame.return_value.url = 'https://example.com/login'
    monkeypatch.setattr(browse_api, '_credential_values', set())
    monkeypatch.setattr(browse_api.visual, 'bring_to_front',
                        lambda *a, display: types.SimpleNamespace(ok=False, detail='hidden'))
    with pytest.raises(RuntimeError, match='input failed'):
        browse_api._selector_action({}, page, {
            'type': 'fill', 'selector': '#token', 'value': 'api-secret',
            'credential': True, 'bound_hosts': ['example.com'],
        })
    assert browse_api._credential_values == {'api-secret'}
    page.wait_for_selector.return_value.evaluate.assert_called_once_with(
        browse_api._CREDENTIAL_FILL_JS, {'value': 'api-secret', 'origin': 'https://example.com'})


def test_credential_fill_waits_for_field_before_marking(monkeypatch):
    page = mock.Mock()
    handle = mock.Mock()
    events = []

    def wait_for_field(selector, **kwargs):
        events.append('wait')
        return handle

    page.wait_for_selector.side_effect = wait_for_field
    page.eval_on_selector.side_effect = RuntimeError('field not inserted yet')
    handle.owner_frame.return_value.url = 'https://example.com/login'
    def evaluate(script, args):
        events.append('fill')
        return {'ok': True}
    handle.evaluate.side_effect = evaluate
    monkeypatch.setattr(browse_api, '_credential_values', set())
    monkeypatch.setattr(browse_api.visual, 'bring_to_front',
                        lambda *a, display: types.SimpleNamespace(ok=False, detail='hidden'))
    result = browse_api._selector_action({}, page, {
        'type': 'fill', 'selector': '#password', 'value': 'fixture-secret',
        'credential': True, 'bound_hosts': ['example.com'],
    })
    assert result['ok'] is True
    assert events == ['wait', 'fill']
    page.wait_for_selector.assert_called_once_with(
        '#password', state='visible', timeout=browse_api.SELECTOR_TIMEOUT_MS)


def test_extract_javascript_reads_live_properties_and_withholds_sensitive_values():
    import json
    import shutil
    import subprocess

    node = shutil.which('node')
    if not node:
        pytest.skip('node is needed to execute the browser extraction script')
    script = r'''
const extract = eval(process.argv[1]);
function field(tag, attrs, props = {}) {
  return {tagName: tag, innerText: '', innerHTML: '',
          getAttribute: name => attrs[name] ?? null, ...props};
}
const controls = [
  field('INPUT', {name: 'search', value: 'old'}, {value: 'typed', checked: false}),
  field('INPUT', {type: 'checkbox'}, {value: 'on', checked: true}),
  field('SELECT', {name: 'quantity'}, {value: '3', innerText: 'One Two Three'}),
  field('IMG', {src: '/photo.png', alt: 'Photo'}),
  field('BUTTON', {'aria-label': 'Close'}),
  field('INPUT', {type: 'password'}, {type: 'password', value: 'hidden'}),
  field('INPUT', {name: 'token'}, {value: 'new secret', __istotaCredential: true}),
];
console.log(JSON.stringify(controls.map(extract)));
'''
    result = subprocess.run([node, '-e', script, browse_api._EXTRACT_ELEMENT_JS],
                            capture_output=True, text=True, check=True)
    entries = json.loads(result.stdout)
    assert entries[0]['value'] == 'typed'
    assert entries[0]['text'] == 'search'
    assert entries[1]['checked'] is True
    assert entries[2]['value'] == '3'
    assert entries[3]['src'] == '/photo.png'
    assert entries[3]['text'] == 'Photo'
    assert entries[4]['text'] == 'Close'
    for entry in entries[5:]:
        assert 'value' not in entry
        assert entry['value_present'] is True


@pytest.mark.parametrize("endpoint", ["browse", "render_page", "interact"])
def test_captcha_names_operator_instance_and_routes_console(navigation, monkeypatch, endpoint):
    from urllib.parse import parse_qs, urlsplit, unquote

    page, _ = navigation
    monkeypatch.setattr(browse_api, "_navigate_and_wait", lambda *a, **k: None)
    inst = types.SimpleNamespace(user_id="alice: team", slot=1, display=":101")
    monkeypatch.setattr(browse_api, "_instance", inst)
    monkeypatch.setenv("BROWSER_VNC_URL", "https://console.example/vnc.html")
    monkeypatch.setattr(browse_api, "request", types.SimpleNamespace(
        get_json=lambda: {"session_id": "session", "url": REQUESTED, "skip_behavior": True, "actions": []},
    ))
    monkeypatch.setattr(browse_api, "jsonify", lambda value: value)
    monkeypatch.setattr(browse_api, "_cleanup_expired", lambda **kwargs: None)
    monkeypatch.setattr(browse_api, "_get_session", lambda sid: {"page": page})
    monkeypatch.setattr(browse_api, "_session_page", lambda session: page)
    monkeypatch.setattr(browse_api, "_foreground_tabs", lambda page: ([], []))
    monkeypatch.setattr(browse_api, "_page_is_gone", lambda page: False)
    monkeypatch.setattr(browse_api.browsing, "detect_captcha", lambda page: True)
    monkeypatch.setattr(browse_api.browsing, "wait_for_datadome", lambda page: None)
    monkeypatch.setattr(browse_api.browsing, "simulate_human_behavior", lambda *a, **k: None)
    result = getattr(browse_api, endpoint)()
    assert result["status"] == "captcha"
    assert result["instance"] == {"user": "alice: team", "slot": 1}
    assert "operator" in result["message"]
    path = parse_qs(urlsplit(result["vnc_url"]).query)["path"][0]
    assert unquote(parse_qs(urlsplit(path).query)["token"][0]) == inst.user_id
    if endpoint == "interact":
        assert result["actions"] == []


@pytest.mark.parametrize("origin,hosts", [
    ("https://evil.example", ["portal.example"]),
    ("http://portal.example", ["portal.example"]),
    ("https://portal.example:8443", ["portal.example"]),
    ("https://portal.example", []),
])
def test_credential_origin_refused_before_input(monkeypatch, origin, hosts):
    page = mock.Mock()
    page.wait_for_selector.return_value.owner_frame.return_value.url = origin
    monkeypatch.setattr(browse_api, "_credential_values", set())
    result = browse_api._selector_action({}, page, {
        "type": "fill", "selector": "#password", "value": "fixture-password",
        "credential": True, "bound_hosts": hosts,
    })
    assert result["error"] == "credential_origin_mismatch"
    page.fill.assert_not_called()
    page.wait_for_selector.return_value.fill.assert_not_called()


def test_credential_fill_never_dispatches_to_current_focus(monkeypatch):
    page = mock.Mock()
    handle = page.wait_for_selector.return_value
    handle.owner_frame.return_value.url = "https://portal.example/login"
    handle.evaluate.return_value = {"ok": True}
    monkeypatch.setattr(browse_api, "_credential_values", set())
    result = browse_api._selector_action({}, page, {
        "type": "fill", "selector": "#password", "value": "fixture-password",
        "credential": True, "bound_hosts": ["portal.example"],
    })
    assert result["ok"] is True
    # Patchright's handle.fill internally calls page.keyboard.insertText.
    # It can target another frame if a focus handler steals the focus.
    handle.fill.assert_not_called()
    page.fill.assert_not_called()
    handle.evaluate.assert_called_once_with(browse_api._CREDENTIAL_FILL_JS,
                                           {"value": "fixture-password", "origin": "https://portal.example"})


@pytest.mark.parametrize("endpoint", ["browse", "render_page"])
def test_page_reads_scrub_registered_card_values(monkeypatch, endpoint):
    page = mock.Mock()
    page.url = "https://shop.example/"
    page.is_closed.return_value = False
    page.content.return_value = "<p>4242424242424242</p>"
    page.title.return_value = "817"
    monkeypatch.setattr(browse_api, "_credential_values", {"4242" * 4, "817"})
    monkeypatch.setattr(browse_api, "_cleanup_expired", lambda **kwargs: None)
    monkeypatch.setattr(browse_api, "_get_session", lambda _: {"page": page})
    monkeypatch.setattr(browse_api.chrome, "connect_cdp", lambda inst: None)
    monkeypatch.setattr(browse_api.browsing, "detect_captcha", lambda page: None)
    monkeypatch.setattr(browse_api.browsing, "extract_page_content", lambda *a, **kw: {
        "text": "4242" * 4, "links": [{"href": "https://shop.example/817"}]})
    monkeypatch.setattr(browse_api, "_collect_frames", lambda *a: ([], False))
    monkeypatch.setattr(browse_api.render, "to_markdown", lambda html, **kw: {"markdown": html})
    monkeypatch.setattr(browse_api, "request", types.SimpleNamespace(
        get_json=lambda: {"session_id": "s1"}))
    monkeypatch.setattr(browse_api, "jsonify", lambda value: value)
    result = getattr(browse_api, endpoint)()
    assert result["status"] == "ok"
    assert "4242" not in str(result)
    assert "817" not in str(result)


def test_browse_scrubs_before_text_and_link_windows():
    page = mock.Mock()
    page.title.return_value = "Checkout"
    page.url = "https://shop.example"
    secret = "4242" * 4
    page.inner_text.return_value = secret
    link = mock.Mock()
    link.inner_text.return_value = "x" * 95 + secret
    link.get_attribute.return_value = "/receipt"
    page.query_selector_all.return_value = [link]
    result = browse_api.browsing.extract_page_content(
        page, max_chars=8, scrub=lambda value: browse_api._scrub_extracted(value, {secret}),
    )
    assert result["text"] == "[REDACTE"
    assert "4242" not in str(result)


def test_card_failure_does_not_log_value(monkeypatch, caplog):
    page = mock.Mock()
    secret = "4242" * 4
    monkeypatch.setattr(browse_api, "_credential_values", {secret})
    monkeypatch.setattr(browse_api, "_cleanup_expired", lambda **kw: None)
    monkeypatch.setattr(browse_api, "_get_session", lambda _: {"page": page})
    monkeypatch.setattr(browse_api, "_session_page", lambda _: page)
    monkeypatch.setattr(browse_api.chrome, "connect_cdp", lambda _: None)
    monkeypatch.setattr(browse_api, "_foreground_tabs", lambda _: ([], []))
    monkeypatch.setattr(browse_api, "_selector_action", mock.Mock(side_effect=RuntimeError(secret)))
    monkeypatch.setattr(browse_api, "jsonify", lambda value: value)
    monkeypatch.setattr(browse_api, "request", types.SimpleNamespace(get_json=lambda: {
        "session_id": "s1", "actions": [{"type": "fill", "credential": True, "card_field": "number"}],
    }))
    result, status = browse_api.interact()
    assert status == 500
    assert secret not in str(result)
    assert secret not in caplog.text


@pytest.mark.parametrize("endpoint", ["browse", "render_page", "interact"])
def test_card_scrub_preserves_session_identifier(monkeypatch, endpoint):
    page = mock.Mock()
    page.url = "https://shop.example/"
    page.title.return_value = "Expiry 07"
    page.content.return_value = "<p>Expiry 07</p>"
    page.is_closed.return_value = False
    monkeypatch.setattr(browse_api, "_credential_values", {"07", "7"})
    monkeypatch.setattr(browse_api, "_cleanup_expired", lambda **kw: None)
    monkeypatch.setattr(browse_api, "_get_session", lambda _: {"page": page})
    monkeypatch.setattr(browse_api, "_session_page", lambda _: page)
    monkeypatch.setattr(browse_api.chrome, "connect_cdp", lambda _: None)
    monkeypatch.setattr(browse_api, "_foreground_tabs", lambda _: ([], []))
    monkeypatch.setattr(browse_api.browsing, "detect_captcha", lambda _: False)
    monkeypatch.setattr(browse_api.browsing, "extract_page_content", lambda *a, **kw: {"text": "Expiry 07"})
    monkeypatch.setattr(browse_api, "_collect_frames", lambda *a: ([], False))
    monkeypatch.setattr(browse_api.render, "to_markdown", lambda html, **kw: {"markdown": html})
    monkeypatch.setattr(browse_api, "jsonify", lambda value: value)
    monkeypatch.setattr(browse_api, "request", types.SimpleNamespace(get_json=lambda: {
        "session_id": "abc7d07e", "actions": [],
    }))
    result = getattr(browse_api, endpoint)()
    assert result["session_id"] == "abc7d07e"
    assert "07" not in result.get("text", result.get("markdown", ""))


@pytest.mark.parametrize("fill_metadata,error", [
    ({"card_field": "exp_month"}, "credential_option_missing"),
    ({"expires_at": 90}, "otp_expired"),
    ({"expires_at": 90}, "credential_origin_mismatch"),
])
def test_failed_sensitive_fill_stops_before_submit(monkeypatch, fill_metadata, error):
    page = mock.Mock()
    monkeypatch.setattr(browse_api, "_credential_values", set())
    monkeypatch.setattr(browse_api, "_cleanup_expired", lambda **kw: None)
    monkeypatch.setattr(browse_api, "_get_session", lambda _: {"page": page})
    monkeypatch.setattr(browse_api, "_session_page", lambda _: page)
    monkeypatch.setattr(browse_api.chrome, "connect_cdp", lambda _: None)
    monkeypatch.setattr(browse_api, "_foreground_tabs", lambda _: ([], []))
    selector_action = mock.Mock(return_value={"action": "fill", "ok": False, "error": error})
    monkeypatch.setattr(browse_api, "_selector_action", selector_action)
    monkeypatch.setattr(browse_api, "jsonify", lambda value: value)
    monkeypatch.setattr(browse_api, "request", types.SimpleNamespace(get_json=lambda: {
        "session_id": "s1", "actions": [
            {"type": "fill", "credential": True, **fill_metadata},
            {"type": "click", "selector": "#submit"},
        ],
    }))
    monkeypatch.setattr(browse_api.browsing, "extract_page_content", lambda *a, **kw: {"text": ""})
    monkeypatch.setattr(browse_api.browsing, "detect_captcha", lambda _: False)
    result = browse_api.interact()
    selector_action.assert_called_once()
    assert result["status"] == "error"
    assert result["error"] == error
    assert result["actions_not_run"] == 1


@pytest.mark.parametrize("origin,hosts", [
    ("https://evil.example", ["acme.example"]),
    ("http://acme.example", ["acme.example"]),
    ("https://acme.example", []),
])
def test_recovery_read_refused_off_the_bound_origin(monkeypatch, origin, hosts):
    page = mock.Mock()
    handle = page.wait_for_selector.return_value
    handle.owner_frame.return_value.url = origin + "/settings"
    monkeypatch.setattr(browse_api, "_credential_values", set())
    result, text = browse_api._read_recovery_action(page, {
        "type": "read_recovery", "selector": "#codes", "bound_hosts": hosts})
    assert result["error"] == "credential_origin_mismatch"
    assert text == ""
    handle.evaluate.assert_not_called()


def test_recovery_read_registers_codes_for_redaction_but_not_headings(monkeypatch):
    page = mock.Mock()
    handle = page.wait_for_selector.return_value
    handle.owner_frame.return_value.url = "https://acme.example/settings"
    codes = "Recovery codes\n1111-aaaa-2222\n3333-bbbb-4444\n"
    handle.evaluate.return_value = {"ok": True, "text": codes}
    monkeypatch.setattr(browse_api, "_credential_values", set())
    result, text = browse_api._read_recovery_action(page, {
        "type": "read_recovery", "selector": "#codes", "bound_hosts": ["acme.example"]})
    assert result["ok"] is True and text == codes
    assert "1111-aaaa-2222" not in str(result)
    handle.evaluate.assert_called_once_with(browse_api._RECOVERY_READ_JS,
                                           {"origin": "https://acme.example"})
    assert {"1111-aaaa-2222", "3333-bbbb-4444"} <= browse_api._credential_values
    assert "Recovery codes" not in browse_api._credential_values


def test_recovery_redactions_take_each_code_token_but_not_plain_words():
    found = browse_api._recovery_redactions(
        "Your codes\nabcd-efgh-ijkl  mnop-qrst-uvwx\n12345678 87654321\nDownload")
    assert {"abcd-efgh-ijkl", "mnop-qrst-uvwx", "12345678", "87654321"} <= found
    assert not {"Your", "codes", "Download"} & found


def test_an_over_broad_recovery_read_registers_nothing(monkeypatch):
    page = mock.Mock()
    handle = page.wait_for_selector.return_value
    handle.owner_frame.return_value.url = "https://acme.example/settings"
    handle.evaluate.return_value = {"ok": True, "text": "\n".join(f"line-{n:04d}" for n in range(65))}
    monkeypatch.setattr(browse_api, "_credential_values", set())
    result, text = browse_api._read_recovery_action(page, {
        "type": "read_recovery", "selector": "body", "bound_hosts": ["acme.example"]})
    assert result["error"] == "recovery_too_large" and text == ""
    assert not browse_api._credential_values


def test_interact_returns_codes_beside_a_scrubbed_response(monkeypatch):
    page = mock.Mock()
    codes = "1111-aaaa-2222\n3333-bbbb-4444"

    def read(_page, action):
        browse_api._credential_values.update(browse_api._recovery_redactions(codes))
        return {"action": "read_recovery", "selector": "#codes", "ok": True}, codes

    monkeypatch.setattr(browse_api, "_credential_values", set())
    monkeypatch.setattr(browse_api, "_read_recovery_action", read)
    monkeypatch.setattr(browse_api, "_cleanup_expired", lambda **kw: None)
    monkeypatch.setattr(browse_api, "_get_session", lambda _: {"page": page})
    monkeypatch.setattr(browse_api, "_session_page", lambda _: page)
    monkeypatch.setattr(browse_api.chrome, "connect_cdp", lambda _: None)
    monkeypatch.setattr(browse_api, "_foreground_tabs", lambda _: ([], []))
    monkeypatch.setattr(browse_api.browsing, "detect_captcha", lambda _: False)
    monkeypatch.setattr(browse_api.browsing, "extract_page_content",
                        lambda *a, **kw: {"text": "Save these: " + codes})
    monkeypatch.setattr(browse_api, "jsonify", lambda value: value)
    monkeypatch.setattr(browse_api, "request", types.SimpleNamespace(get_json=lambda: {
        "session_id": "s1", "actions": [{"type": "read_recovery", "selector": "#codes",
                                         "bound_hosts": ["acme.example"], "credential": True}],
    }))
    result = browse_api.interact()
    assert result["recovery"] == [codes]
    assert result["actions"][0]["recovery"] == 0
    without = {key: value for key, value in result.items() if key != "recovery"}
    assert "1111-aaaa-2222" not in str(without)
    assert "3333-bbbb-4444" not in str(without)


@pytest.mark.parametrize("now,accepted", [(88.99, True), (89, False), (90, False)])
def test_otp_expiry_checked_after_waiting_for_selector(monkeypatch, now, accepted):
    page = mock.MagicMock()
    handle = page.wait_for_selector.return_value
    handle.owner_frame.return_value.url = "https://acme.example/login"
    handle.evaluate.return_value = {"ok": True}
    clock = [60]
    def wait_for_selector(*args, **kwargs):
        clock[0] = now
        return handle

    page.wait_for_selector.side_effect = wait_for_selector
    monkeypatch.setattr(browse_api.time, "time", lambda: clock[0])
    monkeypatch.setattr(browse_api, "_credential_values", set())
    result = browse_api._selector_action({}, page, {
        "type": "fill", "selector": "#code", "value": "123456",
        "credential": True, "bound_hosts": ["acme.example"], "expires_at": 90,
    })
    assert result["ok"] is accepted
    if accepted:
        handle.evaluate.assert_called_once()
        assert "123456" in browse_api._credential_values
    else:
        assert result["error"] == "otp_expired"
        handle.evaluate.assert_not_called()
        assert not browse_api._credential_values
    page.fill.assert_not_called()


@pytest.mark.parametrize("suffix,source,attr", [("", "text", None), ("::value", "value", None), ("::attr(data-secret)", "attr", "data-secret")])
def test_secret_capture_frame_and_sources(monkeypatch, suffix, source, attr):
    page = mock.Mock(url="https://unbound.example")
    handle = page.frame_locator.return_value.locator.return_value.element_handle.return_value
    handle.owner_frame.return_value.url = "https://acme.example/setup"
    handle.evaluate.return_value = {"ok": True, "text": "JBSW Y3DP EHPK 3PXP"}
    monkeypatch.setattr(browse_api, "_credential_values", set())
    result, text = browse_api._read_secret_action(page, {"selector": "iframe>>>#seed" + suffix, "kind": "otp", "bound_hosts": ["acme.example"]})
    assert result["ok"] and text
    assert result["host"] == "acme.example"
    assert handle.evaluate.call_args.args[1] == {"origin": "https://acme.example", "source": source, "attr": attr}
    assert "JBSWY3DPEHPK3PXP" in browse_api._credential_values
    handle.owner_frame.return_value.url = "https://unbound.example"
    result, text = browse_api._read_secret_action(page, {"selector": "iframe>>>#seed", "kind": "otp", "bound_hosts": ["acme.example"]})
    assert result["error"] == "credential_origin_mismatch" and not text


def test_capture_phrase_redacts_lines_and_joined_readback(monkeypatch):
    page = mock.Mock()
    handle = page.wait_for_selector.return_value
    handle.owner_frame.return_value.url = "https://acme.example/setup"
    phrase = "apple berry cherry\nlemon mango peach\napple berry cherry\nlemon mango peach"
    handle.evaluate.return_value = {"ok": True, "text": phrase}
    monkeypatch.setattr(browse_api, "_credential_values", set())
    result, text = browse_api._read_secret_action(page, {"selector": "#phrase", "kind": "phrase", "bound_hosts": ["acme.example"]})
    assert result["ok"] and text == phrase
    assert {"apple berry cherry", "lemon mango peach", " ".join(phrase.split())} <= browse_api._credential_values
