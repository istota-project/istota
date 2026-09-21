"""The browser container's memory-pressure eviction must stay off the monitor thread.

ISSUE-382. ``docker/browser/chrome.py`` states the invariant: Patchright's sync
objects are bound to the Flask thread that created them and must never be
touched from another thread. ``recover_wedged_chrome()`` exists only to honour
it, as a Playwright-free variant of ``restart_chrome()`` the browse watchdog can
call while the Flask thread is blocked inside a CDP call (ISSUE-149, ISSUE-173).

``_resource_monitor``, the container's other background thread, ignored it.
Above ``MEMORY_EVICT_PCT`` it called ``_close_session_unlocked()``, which reaches
``chrome.get_context(browse_api._instance)``, ``page.goto()`` and ``page.close()``. In production
that poisoned the process-global asyncio loop Playwright's sync API drives:
every later ``sync_playwright().start()`` raised "It looks like you are using
Playwright Sync API inside the asyncio loop", so every browse verb returned a
Flask HTML 500 for eight hours while Chrome stayed up and ``/live?deep=1``
stayed green.

Two properties are held here. The monitor asks for an eviction instead of
performing one, and the Flask thread performs it on its next request; and
``chrome.py`` refuses a Patchright call from any thread but the one that opened
the connection, so a future caller that makes the same mistake gets a contained
error rather than a permanently poisoned process.

The browser app runs only inside its own Docker image and depends on
``patchright`` and ``flask``, neither installed in the istota test env, so both
are stubbed and the standalone modules are imported from ``docker/browser/``
directly -- the pattern ``test_browser_chrome_watchdog.py`` already uses.
"""

import sys
from functools import partial
import threading
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
# Only the decorator surface is needed; no endpoint is called from here.
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


@pytest.fixture(autouse=True)
def _reset_module_globals():
    """chrome and browse_api are singletons; reset their globals around each test."""
    def _reset():
        browse_api._instance.proc = None
        chrome._pw = None
        chrome._driver_thread_id = None
        browse_api._instance.pw_browser = None
        browse_api._instance.pw_context = None
        browse_api._instance.pw_thread_id = None
        browse_api._instance.launching = False
        browse_api._sessions.clear()
        browse_api._evict_request.clear()

    _reset()
    yield
    _reset()


def _claim_connection_on_this_thread(monkeypatch, pages=None):
    """Open a fake CDP connection owned by the calling thread.

    Returns the context double, whose `.pages` the eviction path walks.
    """
    ctx = mock.MagicMock(name="context")
    ctx.pages = pages if pages is not None else [
        mock.MagicMock(name="page0"), mock.MagicMock(name="page1"),
    ]
    # A session holds the page object now (ISSUE-535), so every path asks
    # is_closed() before using one -- and a bare MagicMock answers it with a
    # truthy mock, which reads as a tab that is gone. Say so explicitly.
    for page in ctx.pages:
        page.is_closed.return_value = False
    browser = mock.MagicMock(name="browser")
    browser.contexts = [ctx]
    started = mock.MagicMock(name="started_pw")
    started.chromium.connect_over_cdp.return_value = browser
    sp = mock.MagicMock(name="sync_playwright")
    sp.start.return_value = started
    monkeypatch.setattr(chrome, "sync_playwright", lambda: sp)

    chrome.connect_cdp(browse_api._instance)
    assert browse_api._instance.pw_context is ctx
    return ctx


class FakeProc:
    """Minimal stand-in for a subprocess.Popen Chrome handle."""

    def __init__(self, alive=True, pid=1234):
        self.pid = pid
        self._alive = alive
        self.terminated = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._alive = False


def _run_on_another_thread(fn):
    """Run fn() on a fresh thread; return (result, exception)."""
    box = {}

    def _target():
        try:
            box["result"] = fn()
        except BaseException as e:  # noqa: BLE001 - the test inspects it
            box["error"] = e

    t = threading.Thread(target=_target, name="not-the-flask-thread")
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "worker thread hung"
    return box.get("result"), box.get("error")


# ---------------------------------------------------------------------------
# The thread-affinity guard in chrome.py
# ---------------------------------------------------------------------------

class TestPatchrightThreadAffinity:
    """A Patchright call from a foreign thread is refused, not silently served.

    This is the production failure in miniature: the monitor thread reached
    `get_context()` and the process never recovered.
    """

    def test_connect_cdp_claims_the_calling_thread(self, monkeypatch):
        _claim_connection_on_this_thread(monkeypatch)
        assert browse_api._instance.pw_thread_id == threading.get_ident()

    def test_get_context_refuses_a_foreign_thread(self, monkeypatch):
        _claim_connection_on_this_thread(monkeypatch)

        result, error = _run_on_another_thread(partial(chrome.get_context, browse_api._instance))

        assert result is None
        assert isinstance(error, RuntimeError)
        assert "thread" in str(error).lower()

    def test_get_page_by_index_refuses_a_foreign_thread(self, monkeypatch):
        _claim_connection_on_this_thread(monkeypatch)

        _, error = _run_on_another_thread(lambda: chrome.get_page_by_index(browse_api._instance, 0))

        assert isinstance(error, RuntimeError)

    def test_connect_cdp_refuses_a_foreign_thread(self, monkeypatch):
        _claim_connection_on_this_thread(monkeypatch)

        _, error = _run_on_another_thread(partial(chrome.connect_cdp, browse_api._instance))

        assert isinstance(error, RuntimeError)

    def test_disconnect_cdp_refuses_a_foreign_thread(self, monkeypatch):
        """The poisoning call. disconnect_cdp() drives _pw.stop() through the
        same thread-bound machinery, so it must be refused too."""
        _claim_connection_on_this_thread(monkeypatch)
        browser = browse_api._instance.pw_browser

        _, error = _run_on_another_thread(partial(chrome.disconnect_cdp, browse_api._instance))

        assert isinstance(error, RuntimeError)
        browser.close.assert_not_called()
        assert browse_api._instance.pw_browser is browser  # connection left intact

    def test_the_owning_thread_is_never_blocked(self, monkeypatch):
        """The control. The guard must not break the thread that legitimately
        owns the connection -- every helper still works from the Flask thread."""
        ctx = _claim_connection_on_this_thread(monkeypatch)

        assert chrome.get_context(browse_api._instance) is ctx
        assert chrome.get_page_by_index(browse_api._instance, 0) is ctx.pages[0]
        assert chrome.is_cdp_connected(browse_api._instance) is True
        chrome.connect_cdp(browse_api._instance)
        chrome.disconnect_cdp(browse_api._instance)

        assert browse_api._instance.pw_browser is None
        assert browse_api._instance.pw_thread_id is None  # released for the next owner

    def test_recover_wedged_chrome_releases_ownership(self, monkeypatch):
        """Recovery releases the instance connection without touching Patchright.

        The shared driver retains its original thread owner: recovery of one
        Chrome cannot transfer the driver used by other instances.
        """
        _claim_connection_on_this_thread(monkeypatch)
        monkeypatch.setattr(chrome.subprocess, "Popen", lambda *a, **k: FakeProc())
        monkeypatch.setattr(chrome, "_wait_for_chrome_ready", lambda inst, *a, **k: None)
        assert browse_api._instance.pw_thread_id is not None

        _, error = _run_on_another_thread(partial(chrome.recover_wedged_chrome, browse_api._instance))

        assert error is None
        assert browse_api._instance.pw_thread_id is None
        assert chrome._driver_thread_id == threading.get_ident()

    def test_a_foreign_thread_may_open_the_first_connection(self, monkeypatch):
        """The guard binds an existing connection to its owner; it does not
        reserve the module for one thread forever. With nothing connected,
        any thread may claim it."""
        sp = mock.MagicMock(name="sync_playwright")
        browser = mock.MagicMock(name="browser")
        browser.contexts = [mock.MagicMock(name="ctx")]
        sp.start.return_value.chromium.connect_over_cdp.return_value = browser
        monkeypatch.setattr(chrome, "sync_playwright", lambda: sp)

        _, error = _run_on_another_thread(partial(chrome.connect_cdp, browse_api._instance))

        assert error is None
        assert browse_api._instance.pw_thread_id is not None
        assert browse_api._instance.pw_thread_id != threading.get_ident()

    def test_is_cdp_connected_is_readable_from_any_thread(self, monkeypatch):
        """A plain attribute read, deliberately unguarded: the liveness probe
        and the diagnostics read it without owning the connection."""
        _claim_connection_on_this_thread(monkeypatch)

        result, error = _run_on_another_thread(partial(chrome.is_cdp_connected, browse_api._instance))

        assert error is None
        assert result is True


# ---------------------------------------------------------------------------
# The monitor thread asks; the Flask thread evicts
# ---------------------------------------------------------------------------

class _PatchrightTripwire:
    """Stands in for the chrome module and records every Patchright access.

    Two things this must get right, both learned the hard way.

    It **records** as well as raising, and the test asserts on the record.
    `_close_session_unlocked` wraps its whole chrome block in a bare
    `except Exception`, so a double that only raised would be swallowed on the
    exact path that poisons the process, and the test would pass green over a
    live defect.

    And it raises a `BaseException` subclass rather than `AssertionError`, for
    the same reason: `AssertionError` is an `Exception` and would be caught
    there too. `BaseException` escapes that handler, so a call that does slip
    through fails the test loudly instead of silently.
    """

    _FORBIDDEN = (
        "connect_cdp", "disconnect_cdp", "get_context",
        "get_page_by_index", "is_cdp_connected",
    )

    class Touched(BaseException):
        """Deliberately not an Exception -- see the class docstring."""

    def __init__(self):
        self.touches = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._FORBIDDEN:
            self.touches.append((name, threading.current_thread().name))
            raise self.Touched(
                f"chrome.{name}() reached from thread "
                f"{threading.current_thread().name!r} -- this is ISSUE-382",
            )
        raise AttributeError(name)


class TestMemoryPressureIsDeferredToTheFlaskThread:
    def _add_session(self, session_id, age_s=0.0, page=None):
        """Register a live session `age_s` seconds old, holding `page`.

        Ages are relative to now and well inside SESSION_TTL, so _evict_expired()
        leaves them alone and what the drain does is the only thing under test.

        `page` is the object itself rather than an index into ctx.pages
        (ISSUE-535). It is not optional in any test that asserts on a close: a
        session with no page is one the sweep counts as naming nothing, so its
        tab is closed as an orphan and the eviction assertion passes for the
        wrong reason.
        """
        assert age_s < browse_api.SESSION_TTL, "session would expire on its own"
        browse_api._sessions[session_id] = {
            "page": page,
            "created_at": browse_api.time.time() - age_s, "last_used_at": browse_api.time.time() - age_s,
        }

    def _pressure(self, monkeypatch, pct):
        """Pin what the container reports as its memory usage.

        Patched at the cgroup read rather than at `_get_memory_pct`, so every
        caller -- the monitor's own sampling, its log line, and the drain's
        re-check -- sees one consistent number and none of them is stubbed out.
        """
        monkeypatch.setattr(
            browse_api, "_read_container_memory_mb", lambda: (pct * 10, 1000),
        )

    # -- the monitor thread asks -------------------------------------------

    def test_pressure_requests_an_eviction(self, monkeypatch):
        self._add_session("old", age_s=60)

        requested = browse_api._note_memory_pressure(
            browse_api.MEMORY_EVICT_PCT + 5,
        )

        assert requested is True
        assert browse_api._evict_request.is_set()
        assert "old" in browse_api._sessions  # not evicted here

    def test_pressure_below_the_threshold_asks_for_nothing(self):
        self._add_session("old", age_s=60)

        requested = browse_api._note_memory_pressure(
            browse_api.MEMORY_EVICT_PCT - 5,
        )

        assert requested is False
        assert not browse_api._evict_request.is_set()

    def test_sustained_pressure_reports_once_not_every_tick(self):
        """Sustained pressure must not log a fresh warning every 30s forever,
        beside the HIGH MEMORY line already reporting the same condition."""
        over = browse_api.MEMORY_EVICT_PCT + 5

        assert browse_api._note_memory_pressure(over) is True
        assert browse_api._note_memory_pressure(over) is False
        assert browse_api._note_memory_pressure(over) is False
        assert browse_api._evict_request.is_set()  # still pending

    # -- the regression site itself ----------------------------------------

    def test_a_monitor_tick_under_pressure_never_reaches_patchright(
        self, monkeypatch,
    ):
        """The regression test, driven through the function that held the bug.

        `_monitor_tick` is the extracted body of `_resource_monitor`'s loop --
        the code that used to evict inline. Restoring that eviction makes this
        go red, which the earlier version of this file could not do: it called
        `_note_memory_pressure` directly and so never executed the defect site.
        """
        tripwire = _PatchrightTripwire()
        monkeypatch.setattr(browse_api, "chrome", tripwire)
        # Steer only the memory reading. `_note_memory_pressure` and the whole
        # of `_monitor_tick` run for real -- patching either is what made the
        # first version of this test vacuous.
        self._pressure(monkeypatch, browse_api.MEMORY_EVICT_PCT + 5)
        monkeypatch.setattr(
            browse_api.subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(stdout=""),
        )
        self._add_session("old", age_s=60)

        _, error = _run_on_another_thread(browse_api._monitor_tick)

        assert error is None, f"monitor tick raised: {error!r}"
        assert tripwire.touches == [], (
            f"the monitor thread reached Patchright: {tripwire.touches}"
        )
        assert "old" in browse_api._sessions  # still the Flask thread's job
        assert browse_api._evict_request.is_set()  # it did see the pressure

    def test_a_monitor_tick_survives_a_missing_ps(self, monkeypatch):
        """The loop swallows errors, so a tick that raises is invisible in
        production. Confirm the ordinary path does not depend on `ps`."""
        monkeypatch.setattr(
            browse_api.subprocess, "run",
            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("ps")),
        )

        with pytest.raises(FileNotFoundError):
            browse_api._monitor_tick()

        assert not browse_api._evict_request.is_set()

    # -- the Flask thread evicts -------------------------------------------

    def test_cleanup_drains_the_request_and_evicts_the_oldest(self, monkeypatch):
        """The Flask thread does the work the monitor asked for."""
        ctx = _claim_connection_on_this_thread(monkeypatch)
        self._pressure(monkeypatch, browse_api.MEMORY_EVICT_PCT + 5)
        self._add_session("older", age_s=60, page=ctx.pages[0])
        self._add_session("newer", age_s=10, page=ctx.pages[1])
        browse_api._evict_request.set()

        browse_api._cleanup_expired()

        assert "older" not in browse_api._sessions
        assert "newer" in browse_api._sessions
        assert not browse_api._evict_request.is_set()
        ctx.pages[0].close.assert_called_once()

    def test_a_stale_request_is_dropped_once_memory_recovers(self, monkeypatch):
        """The eviction request is a deferral, not a latch.

        Nothing clears the flag but a request, and requests can be hours apart.
        Without re-reading memory, a spike at 03:00 kills a live session on the
        first request of the morning at 20% memory -- and since every endpoint
        calls _cleanup_expired() before _get_session(), the session it takes can
        be the one that request just named.
        """
        ctx = _claim_connection_on_this_thread(monkeypatch)
        self._pressure(monkeypatch, browse_api.MEMORY_EVICT_PCT - 60)
        self._add_session("live", age_s=60, page=ctx.pages[0])
        browse_api._evict_request.set()

        browse_api._cleanup_expired()

        assert "live" in browse_api._sessions
        assert not browse_api._evict_request.is_set()  # consumed, not left armed
        ctx.pages[0].close.assert_not_called()

    def test_cleanup_evicts_nothing_when_no_request_is_pending(self, monkeypatch):
        """The control. Under real pressure but with nothing asked for, an
        unexpired session stays -- so it is the request that drives eviction."""
        ctx = _claim_connection_on_this_thread(monkeypatch)
        self._pressure(monkeypatch, browse_api.MEMORY_EVICT_PCT + 5)
        self._add_session("live", age_s=0, page=ctx.pages[0])

        browse_api._cleanup_expired()

        assert "live" in browse_api._sessions
        ctx.pages[0].close.assert_not_called()

    def test_a_drained_request_does_not_evict_twice(self, monkeypatch):
        """The flag is consumed, so one pressure report costs one session."""
        ctx = _claim_connection_on_this_thread(monkeypatch)
        self._pressure(monkeypatch, browse_api.MEMORY_EVICT_PCT + 5)
        self._add_session("a", age_s=60, page=ctx.pages[0])
        self._add_session("b", age_s=10, page=ctx.pages[1])
        browse_api._evict_request.set()

        browse_api._cleanup_expired()
        browse_api._cleanup_expired()

        assert "a" not in browse_api._sessions
        assert "b" in browse_api._sessions

    def test_a_request_with_no_sessions_is_harmless(self, monkeypatch):
        """Sessions can drain between the report and the next request."""
        _claim_connection_on_this_thread(monkeypatch)
        self._pressure(monkeypatch, browse_api.MEMORY_EVICT_PCT + 5)
        browse_api._evict_request.set()

        browse_api._cleanup_expired()

        assert not browse_api._evict_request.is_set()
        assert browse_api._sessions == {}

    def test_the_eviction_runs_on_the_thread_that_owns_the_connection(
        self, monkeypatch,
    ):
        """End to end. The connection is owned here, pressure is reported from
        another thread, and the eviction happens back here -- which is the whole
        point of the split."""
        ctx = _claim_connection_on_this_thread(monkeypatch)
        self._pressure(monkeypatch, browse_api.MEMORY_EVICT_PCT + 5)
        self._add_session("old", age_s=60, page=ctx.pages[0])

        _, error = _run_on_another_thread(
            lambda: browse_api._note_memory_pressure(
                browse_api.MEMORY_EVICT_PCT + 5,
            ),
        )
        assert error is None
        assert "old" in browse_api._sessions  # the monitor evicted nothing

        browse_api._cleanup_expired()

        assert "old" not in browse_api._sessions
        ctx.pages[0].close.assert_called_once()
        assert browse_api._instance.pw_thread_id == threading.get_ident()


@pytest.mark.parametrize("existing_owner", ["first-task", None])
@pytest.mark.parametrize("owner", ["other-task", None, "", [], {}])
def test_capacity_never_evicts_another_caller(monkeypatch, owner, existing_owner):
    ctx = _claim_connection_on_this_thread(monkeypatch)
    monkeypatch.setattr(browse_api, "MAX_SESSIONS", 1)
    monkeypatch.setattr(browse_api, "_get_memory_pct", lambda: 0)
    browse_api._sessions["existing"] = {
        "owner": existing_owner, "created_at": browse_api.time.time(), "last_used_at": browse_api.time.time(), "page": ctx.pages[0],
    }
    with pytest.raises(RuntimeError, match="capacity"):
        browse_api._create_session(owner=owner)
    assert "existing" in browse_api._sessions
    ctx.new_page.assert_not_called()


def test_capacity_can_replace_only_same_owner(monkeypatch):
    ctx = _claim_connection_on_this_thread(monkeypatch)
    monkeypatch.setattr(browse_api, "MAX_SESSIONS", 2)
    monkeypatch.setattr(browse_api, "_get_memory_pct", lambda: 0)
    now = browse_api.time.time()
    browse_api._sessions.update({
        "foreign": {"owner": "other", "created_at": now - 10, "last_used_at": now - 10, "page": ctx.pages[0]},
        "own": {"owner": "task", "created_at": now, "last_used_at": now, "page": ctx.pages[1]},
    })
    sid, _ = browse_api._create_session(owner="task")
    assert set(browse_api._sessions) == {"foreign", sid}
    assert browse_api._sessions[sid]["owner"] == "task"


@pytest.mark.parametrize("pressure", [False, True])
@pytest.mark.parametrize("endpoint", ["browse", "screenshot", "extract", "render_page"])
def test_create_endpoint_refuses_capacity_without_draining_foreign_session(monkeypatch, endpoint, pressure):
    ctx = _claim_connection_on_this_thread(monkeypatch)
    monkeypatch.setattr(browse_api, "MAX_SESSIONS", 1)
    monkeypatch.setattr(browse_api, "_get_memory_pct", lambda: browse_api.MEMORY_EVICT_PCT + 1 if pressure else 0)
    monkeypatch.setattr(browse_api, "_sweep_unclaimed_pages_unlocked", lambda: None)
    monkeypatch.setattr(browse_api, "jsonify", lambda value: value)
    monkeypatch.setattr(browse_api, "request", types.SimpleNamespace(
        get_json=lambda: {"url": "https://example.com", "owner": "other"},
    ))
    browse_api._sessions["foreign"] = {
        "owner": "first", "created_at": browse_api.time.time(), "last_used_at": browse_api.time.time(), "page": ctx.pages[0],
    }
    browse_api._evict_request.set()
    body, status = getattr(browse_api, endpoint)()
    assert status == 503
    assert body["status"] == "error"
    assert body["retry_after_seconds"] > 0
    assert "foreign" in browse_api._sessions
    ctx.new_page.assert_not_called()


def test_capacity_retry_estimate_uses_first_expiration(monkeypatch):
    _claim_connection_on_this_thread(monkeypatch)
    monkeypatch.setattr(browse_api, "MAX_SESSIONS", 2)
    monkeypatch.setattr(browse_api, "_get_memory_pct", lambda: 0)
    monkeypatch.setattr(browse_api.time, "time", lambda: 1000)
    browse_api._sessions.update({
        "first": {"owner": "other", "created_at": 450, "last_used_at": 950},
        "second": {"owner": "other", "created_at": 900, "last_used_at": 900},
    })
    with pytest.raises(browse_api.SessionCapacityError) as caught:
        browse_api._create_session(owner="new")
    assert caught.value.retry_after_seconds == 501


@pytest.mark.parametrize("expire_via_lookup", [False, True])
def test_session_activity_extends_idle_expiry(monkeypatch, expire_via_lookup):
    ctx = _claim_connection_on_this_thread(monkeypatch)
    ctx.new_page.return_value = ctx.pages[0]
    monkeypatch.setattr(browse_api, "_get_memory_pct", lambda: 0)
    clock = [1000]
    monkeypatch.setattr(browse_api.time, "time", lambda: clock[0])
    sid, page = browse_api._create_session(owner="task")
    for _ in range(3):
        clock[0] += browse_api.SESSION_TTL - 1
        browse_api._evict_expired()
        session = browse_api._get_session(sid)
        assert session is not None
        assert session["created_at"] == 1000
    monkeypatch.setattr(browse_api, "jsonify", lambda value: value)
    info = browse_api.get_session_info(sid)
    assert info["age_seconds"] == clock[0] - 1000
    assert info["ttl_seconds"] == browse_api.SESSION_TTL
    clock[0] += browse_api.SESSION_TTL + 1
    if expire_via_lookup:
        assert browse_api._get_session(sid) is None
    else:
        browse_api._evict_expired()
    assert sid not in browse_api._sessions
    page.close.assert_called_once()


@pytest.mark.parametrize("pressure", [False, True])
def test_eviction_prefers_idle_session_over_older_active_session(monkeypatch, pressure):
    ctx = _claim_connection_on_this_thread(monkeypatch)
    monkeypatch.setattr(browse_api, "MAX_SESSIONS", 2)
    monkeypatch.setattr(browse_api, "_get_memory_pct", lambda: 0)
    clock = [1000]
    monkeypatch.setattr(browse_api.time, "time", lambda: clock[0])
    ctx.new_page.return_value = ctx.pages[0]
    active, _ = browse_api._create_session(owner="task")
    clock[0] += 10
    ctx.new_page.return_value = ctx.pages[1]
    idle, _ = browse_api._create_session(owner="task")
    clock[0] += 10
    assert browse_api._get_session(active) is not None
    if pressure:
        monkeypatch.setattr(browse_api, "_get_memory_pct", lambda: 90)
        browse_api._evict_request.set()
        assert browse_api._drain_evict_request_unlocked() == idle
    else:
        browse_api._create_session(owner="task")
    assert active in browse_api._sessions
    assert idle not in browse_api._sessions
    ctx.pages[1].close.assert_called_once()
    ctx.pages[0].close.assert_not_called()
