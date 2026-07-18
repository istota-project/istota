"""Unit tests for the browser container's Chrome lifecycle self-heal logic.

Covers the two ISSUE-173 follow-up fixes in ``docker/browser/chrome.py``:

* the process-lifecycle lock that serializes launch/ensure/restart/recover so
  the watchdog thread and the Flask request thread can't race two ``Popen``s
  against the same profile dir + debug port (the double-launch orphan); and
* the CDP staleness check in ``connect_cdp`` that forces a real round-trip
  (``new_browser_cdp_session``) instead of poking the cached ``.contexts`` list,
  which never raises on a Chrome killed from the watchdog thread.

The browser app runs only inside its own Docker image and depends on
``patchright`` + ``flask``, neither of which is installed in the istota test
env, so we stub ``patchright`` and import the standalone ``chrome`` module from
``docker/browser/`` directly.
"""

import sys
import threading
import time
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

_BROWSER_DIR = Path(__file__).resolve().parent.parent / "docker" / "browser"
if str(_BROWSER_DIR) not in sys.path:
    sys.path.insert(0, str(_BROWSER_DIR))

import chrome  # noqa: E402  (import after the patchright stub + path insert)


class FakeProc:
    """Minimal stand-in for a subprocess.Popen Chrome handle."""

    def __init__(self, alive=True, pid=1234):
        self.pid = pid
        self._alive = alive
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True
        self._alive = False


@pytest.fixture(autouse=True)
def _reset_chrome_globals():
    """Chrome is a singleton module; reset its globals around every test."""
    def _reset():
        chrome._chrome_proc = None
        chrome._pw = None
        chrome._pw_browser = None
        chrome._pw_context = None
        chrome._launching = False

    _reset()
    yield
    _reset()


# --------------------------------------------------------------------------
# #4 -- connect_cdp staleness detection (robust round-trip, not cached state)
# --------------------------------------------------------------------------

def test_connect_cdp_reuses_live_connection(monkeypatch):
    """A live browser round-trips OK -> reuse it, never reconnect."""
    live = mock.MagicMock(name="live_browser")
    session = mock.MagicMock(name="cdp_session")
    live.new_browser_cdp_session.return_value = session
    chrome._pw_browser = live

    sp = mock.MagicMock(name="sync_playwright")
    monkeypatch.setattr(chrome, "sync_playwright", lambda: sp)

    chrome.connect_cdp()

    live.new_browser_cdp_session.assert_called_once()  # a real round-trip ran
    session.detach.assert_called_once()
    sp.start.assert_not_called()  # no reconnect
    assert chrome._pw_browser is live


def test_connect_cdp_reconnects_when_round_trip_fails(monkeypatch):
    """The ISSUE-173 bug: a dead socket whose cached .contexts still 'looks'
    live must be detected via the round-trip and force a reconnect."""
    stale = mock.MagicMock(name="stale_browser")
    # The old, broken signal: .contexts is a cached list that never raises.
    stale.contexts = [mock.MagicMock(name="cached_ctx")]
    # The real state: the websocket is dead, so any round-trip raises.
    stale.new_browser_cdp_session.side_effect = Exception("Target closed")
    chrome._pw_browser = stale
    chrome._pw = mock.MagicMock(name="stale_pw")

    new_browser = mock.MagicMock(name="new_browser")
    new_browser.contexts = [mock.MagicMock(name="new_ctx")]
    started = mock.MagicMock(name="started_pw")
    started.chromium.connect_over_cdp.return_value = new_browser
    sp = mock.MagicMock(name="sync_playwright")
    sp.start.return_value = started
    monkeypatch.setattr(chrome, "sync_playwright", lambda: sp)

    chrome.connect_cdp()

    stale.new_browser_cdp_session.assert_called_once()
    started.chromium.connect_over_cdp.assert_called_once()
    assert chrome._pw_browser is new_browser  # reconnected, not the stale one


def test_connect_cdp_detach_failure_does_not_force_reconnect(monkeypatch):
    """A failing detach on an otherwise-live session must not be misread as a
    dead connection -- only an attach (round-trip) failure reconnects."""
    live = mock.MagicMock(name="live_browser")
    session = mock.MagicMock(name="cdp_session")
    session.detach.side_effect = Exception("detach boom")
    live.new_browser_cdp_session.return_value = session
    chrome._pw_browser = live

    sp = mock.MagicMock(name="sync_playwright")
    monkeypatch.setattr(chrome, "sync_playwright", lambda: sp)

    chrome.connect_cdp()

    sp.start.assert_not_called()  # attach succeeded -> reuse
    assert chrome._pw_browser is live


# --------------------------------------------------------------------------
# #1 -- process-lifecycle lock serialization (no double-launch orphan)
# --------------------------------------------------------------------------

def test_concurrent_recover_and_ensure_never_double_launch(monkeypatch):
    """The watchdog's recover and a Flask-thread ensure must not both Popen at
    once. Without the lock this races to max concurrency 2 (the orphan bug)."""
    state = {"cur": 0, "max": 0}
    guard = threading.Lock()

    def fake_popen(*_a, **_k):
        with guard:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        time.sleep(0.05)  # widen the overlap window
        with guard:
            state["cur"] -= 1
        return FakeProc()

    monkeypatch.setattr(chrome.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(chrome, "_wait_for_chrome_ready", lambda *a, **k: None)

    chrome._chrome_proc = None
    errors = []

    def _run(fn):
        try:
            fn()
        except Exception as exc:  # pragma: no cover - surfaced via assert
            errors.append(exc)

    t1 = threading.Thread(target=_run, args=(chrome.recover_wedged_chrome,))
    t2 = threading.Thread(target=_run, args=(chrome.ensure_chrome,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors
    assert state["max"] == 1  # the lock serialized the two launches


def test_recover_wedged_kills_old_proc_and_skips_disconnect(monkeypatch):
    """recover_wedged_chrome kills the old process and relaunches, but must
    never touch Patchright (thread affinity -- it runs on the watchdog thread)."""
    new_proc = FakeProc()
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda *a, **k: new_proc)
    monkeypatch.setattr(chrome, "_wait_for_chrome_ready", lambda *a, **k: None)
    disc = mock.MagicMock(name="disconnect_cdp")
    monkeypatch.setattr(chrome, "disconnect_cdp", disc)

    old = FakeProc(alive=True)
    chrome._chrome_proc = old

    chrome.recover_wedged_chrome()

    assert old.terminated is True
    assert chrome._chrome_proc is new_proc
    disc.assert_not_called()


def test_recover_wedged_relaunches_with_no_existing_proc(monkeypatch):
    new_proc = FakeProc()
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda *a, **k: new_proc)
    monkeypatch.setattr(chrome, "_wait_for_chrome_ready", lambda *a, **k: None)

    chrome._chrome_proc = None
    chrome.recover_wedged_chrome()

    assert chrome._chrome_proc is new_proc


def test_ensure_chrome_noop_when_already_running(monkeypatch):
    popen = mock.MagicMock(name="Popen")
    monkeypatch.setattr(chrome.subprocess, "Popen", popen)

    chrome._chrome_proc = FakeProc(alive=True)
    chrome.ensure_chrome()

    popen.assert_not_called()


def test_launch_sets_launching_window_and_clears_it(monkeypatch):
    """_launching is True across the launch (so the deep liveness probe exempts
    the window) and False once ready."""
    seen = {}

    def fake_wait(*_a, **_k):
        seen["during"] = chrome._launching

    monkeypatch.setattr(chrome.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(chrome, "_wait_for_chrome_ready", fake_wait)

    assert chrome.is_launching() is False
    chrome.launch_chrome()

    assert seen["during"] is True
    assert chrome.is_launching() is False
