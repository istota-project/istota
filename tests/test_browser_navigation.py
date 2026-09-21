"""Navigation must not return a previous document as a successful fetch."""

import sys
import itertools
import types
from pathlib import Path
from unittest import mock

import pytest

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
    monkeypatch.setattr(chrome, "connect_cdp", lambda: None)
    monkeypatch.setattr(chrome, "get_context", lambda: types.SimpleNamespace(pages=[page]))
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
    monkeypatch.setattr(browse_api.browsing, "simulate_human_behavior", lambda p: None)
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
    monkeypatch.setattr(browse_api.chrome, 'connect_cdp', lambda: None)
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
    monkeypatch.setattr(browse_api.chrome, 'connect_cdp', lambda: None)
    monkeypatch.setattr(browse_api, 'request', types.SimpleNamespace(
        get_json=lambda: {'session_id': 's1', 'selector': '*'}))
    monkeypatch.setattr(browse_api, 'jsonify', lambda value: value)
    result = browse_api.extract()
    assert result['count'] == 1
    for key, value in expected.items():
        assert result['elements'][0][key] == value
    field.evaluate.assert_called_once_with(browse_api._EXTRACT_ELEMENT_JS)


def test_credential_fill_registers_before_fallback_and_failed_input(monkeypatch):
    page = mock.Mock()
    page.fill.side_effect = RuntimeError('input failed')
    monkeypatch.setattr(browse_api, '_credential_values', set())
    monkeypatch.setattr(browse_api.visual, 'bring_to_front',
                        lambda *a: types.SimpleNamespace(ok=False, detail='hidden'))
    with pytest.raises(RuntimeError, match='input failed'):
        browse_api._selector_action({}, page, {
            'type': 'fill', 'selector': '#token', 'value': 'api-secret',
            'credential': True,
        })
    assert browse_api._credential_values == {'api-secret'}
    page.wait_for_selector.return_value.evaluate.assert_called_once_with(
        'el => { el.__istotaCredential = true; }')


def test_credential_fill_waits_for_field_before_marking(monkeypatch):
    page = mock.Mock()
    handle = mock.Mock()
    events = []

    def wait_for_field(selector, **kwargs):
        events.append('wait')
        return handle

    page.wait_for_selector.side_effect = wait_for_field
    page.eval_on_selector.side_effect = RuntimeError('field not inserted yet')
    handle.evaluate.side_effect = lambda script: events.append('mark')
    page.fill.side_effect = lambda *args, **kwargs: events.append('fill')
    monkeypatch.setattr(browse_api, '_credential_values', set())
    monkeypatch.setattr(browse_api.visual, 'bring_to_front',
                        lambda *a: types.SimpleNamespace(ok=False, detail='hidden'))
    result = browse_api._selector_action({}, page, {
        'type': 'fill', 'selector': '#password', 'value': 'fixture-secret',
        'credential': True,
    })
    assert result['ok'] is True
    assert events == ['wait', 'mark', 'fill']
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
