"""The deep liveness probe must see a wedged Patchright behind a healthy Chrome.

ISSUE-384, deferred from ISSUE-382. The container has a complete auto-heal path
on the supported production shape: ``istota-browser-watchdog.sh`` runs every
minute, reads ``.State.Health.Status``, restarts after a debounce and pages if it
crash-loops. Nothing had to be built to act on an unhealthy verdict -- only to
reach one.

``_probe`` had two tiers, and neither asked anything about the API's own ability
to drive the browser. The cheap tier asks whether the Chrome process is alive;
the deep tier asks whether Chrome's DevTools endpoint answers. Through the whole
of ISSUE-382 both were true and both stayed green while every browse verb
returned a Flask HTML 500, because what was dead was the API process's Patchright
binding: the process-global asyncio loop was left permanently ``is_running()``,
so every ``sync_playwright().start()`` refused. The outage ended at the 05:00
proactive restart, which knew nothing about the fault.

The third arm cannot call Patchright from the liveness thread -- that is
ISSUE-382's whole lesson, and the liveness server has its own thread. So
``chrome.py`` publishes a record instead: the outcome and timestamp of the last
CDP-touching call, plus a consecutive-failure count, all plain assignments under
a leaf lock. ``_probe`` reads that against a staleness window.

Stubbing follows ``test_browser_memory_eviction.py``: the browser app runs only
inside its own Docker image and depends on ``patchright`` and ``flask``, neither
installed in the istota test env.
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

# Scope the skip to the one genuinely optional dependency; see the note in
# test_browser_memory_eviction.py.
pytest.importorskip("bs4", reason="browser render module needs bs4")

import browse_api  # noqa: E402  (import after the stubs + path insert)


def _write_cdp_health(**fields):
    """Set the record directly, the way the other browser tests poke globals.

    Deliberately not a helper in chrome.py: a test-only mutator in production
    code is a second way to write the record, and the point of the module is
    that there is exactly one.
    """
    with chrome._cdp_health_lock:
        chrome._cdp_health.update(fields)


@pytest.fixture(autouse=True)
def _reset_module_globals():
    """chrome and browse_api are singletons; reset their globals around each test."""
    def _reset():
        chrome._chrome_proc = None
        chrome._pw = None
        chrome._pw_browser = None
        chrome._pw_context = None
        chrome._pw_thread_id = None
        chrome._launching = False
        _write_cdp_health(
            last_success=0.0, last_failure=0.0,
            consecutive_failures=0, last_error="",
        )
        # The wedge-recovery record is module state like the rest, and this file
        # drives recover_wedged_chrome(). Left unreset it accumulates across
        # tests until the deep probe's fourth arm (ISSUE-394) answers
        # `wedge-loop` for every case here that expects `ok`.
        chrome._launch_generation = 0
        with chrome._wedge_lock:
            chrome._wedge_recoveries.clear()
        browse_api._wedge_loop_reported = False
        browse_api._sessions.clear()
        browse_api._evict_request.clear()

    _reset()
    yield
    _reset()


class FakeProc:
    """Minimal stand-in for a subprocess.Popen Chrome handle."""

    def __init__(self, alive=True, pid=1234):
        self.pid = pid
        self._alive = alive

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._alive = False


def _healthy_chrome(monkeypatch):
    """Chrome up, DevTools answering, not launching.

    Both old probe tiers read green in this state, and it is also the only state
    in which a CDP failure is *counted*: chrome._chrome_explains_failure() reads
    the same three facts, so a failure recorded here is one Chrome does not
    account for -- the ISSUE-382 signature.
    """
    chrome._chrome_proc = FakeProc(alive=True)
    chrome._launching = False
    monkeypatch.setattr(chrome, "devtools_responding", lambda timeout=2: True)
    monkeypatch.setattr(chrome, "is_launching", lambda: False)


def _working_playwright(monkeypatch):
    """Make chrome.connect_cdp() succeed against doubles."""
    ctx = mock.MagicMock(name="context")
    ctx.pages = []
    browser = mock.MagicMock(name="browser")
    browser.contexts = [ctx]
    started = mock.MagicMock(name="started_pw")
    started.chromium.connect_over_cdp.return_value = browser
    sp = mock.MagicMock(name="sync_playwright")
    sp.start.return_value = started
    monkeypatch.setattr(chrome, "sync_playwright", lambda: sp)
    return ctx


def _poisoned_playwright(monkeypatch, message="Playwright Sync API inside the asyncio loop"):
    """Make every sync_playwright().start() raise, as ISSUE-382 did."""
    def _raise():
        raise RuntimeError(message)

    sp = mock.MagicMock(name="sync_playwright")
    sp.start.side_effect = _raise
    monkeypatch.setattr(chrome, "sync_playwright", lambda: sp)
    monkeypatch.setattr(chrome.time, "sleep", lambda _s: None)


def _run_on_another_thread(fn):
    """Run fn() on a fresh thread; return (result, exception)."""
    box = {}

    def _target():
        try:
            box["result"] = fn()
        except BaseException as e:  # noqa: BLE001 - the test inspects it
            box["error"] = e

    t = threading.Thread(target=_target, name="foreign")
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "foreign thread did not finish"
    return box.get("result"), box.get("error")


class TestTheRecord:
    """chrome.py publishes the evidence; it performs no verdict of its own."""

    def test_the_record_has_exactly_the_four_documented_fields(self):
        """The field names are a contract: _probe and health() index them.

        Not an assertion about the initial values -- the autouse fixture writes
        those, so asserting them here would test the fixture. The key set is the
        part the fixture cannot fake, since it only ever updates.
        """
        assert set(chrome.cdp_health()) == {
            "last_success", "last_failure", "consecutive_failures", "last_error",
        }

    def test_a_successful_connect_records_a_success(self, monkeypatch):
        _working_playwright(monkeypatch)
        before = time.monotonic()
        chrome.connect_cdp()
        h = chrome.cdp_health()
        assert h["last_success"] >= before
        assert h["consecutive_failures"] == 0

    def test_reusing_a_live_connection_records_a_success(self, monkeypatch):
        _working_playwright(monkeypatch)
        chrome.connect_cdp()
        _write_cdp_health(last_success=0.0)
        # Second call takes the reuse branch: a real CDP round-trip over the
        # socket, which is as much evidence of a working binding as a fresh one.
        chrome.connect_cdp()
        assert chrome.cdp_health()["last_success"] > 0.0

    def test_exhausting_the_retries_records_a_failure_with_the_error(self, monkeypatch):
        _healthy_chrome(monkeypatch)
        _poisoned_playwright(monkeypatch)
        before = time.monotonic()
        with pytest.raises(RuntimeError):
            chrome.connect_cdp()
        h = chrome.cdp_health()
        assert h["consecutive_failures"] == 1, "one call is one failure, not one per retry"
        assert h["last_failure"] >= before
        assert "asyncio loop" in h["last_error"]

    def test_failures_accumulate_and_a_success_clears_them(self, monkeypatch):
        _healthy_chrome(monkeypatch)
        _poisoned_playwright(monkeypatch)
        for _ in range(3):
            with pytest.raises(RuntimeError):
                chrome.connect_cdp()
        assert chrome.cdp_health()["consecutive_failures"] == 3

        _working_playwright(monkeypatch)
        chrome.connect_cdp()
        assert chrome.cdp_health()["consecutive_failures"] == 0

    def test_a_thread_affinity_refusal_counts_as_a_failure(self, monkeypatch):
        """The other way this class of fault presents (ISSUE-382's guard).

        A refusal means that call could not drive the browser. When the refused
        caller is the request thread -- the lockout ISSUE-382's own review found
        -- it is exactly the fault this probe exists to catch, and nothing at the
        raise site can tell the two apart.
        """
        _healthy_chrome(monkeypatch)
        _working_playwright(monkeypatch)
        chrome.connect_cdp()  # this thread owns the connection
        _write_cdp_health(consecutive_failures=0, last_error="")

        _, err = _run_on_another_thread(lambda: chrome.connect_cdp())
        assert isinstance(err, RuntimeError)
        h = chrome.cdp_health()
        assert h["consecutive_failures"] == 1
        assert "does not own the CDP connection" in h["last_error"]

    def test_recovering_chrome_does_not_clear_the_failures(self, monkeypatch):
        """A Chrome restart does not repair a poisoned asyncio loop (ISSUE-382).

        recover_wedged_chrome() clears the ownership id so the next connect can
        claim it, but only a genuine CDP success is evidence the binding works.
        """
        _healthy_chrome(monkeypatch)
        _poisoned_playwright(monkeypatch)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                chrome.connect_cdp()
        monkeypatch.setattr(chrome, "launch_chrome", lambda: None)
        chrome._chrome_proc = FakeProc(alive=True)

        chrome.recover_wedged_chrome()

        assert chrome.cdp_health()["consecutive_failures"] == 2

    def test_a_failure_chrome_explains_is_recorded_but_not_counted(self, monkeypatch):
        """The signature ISSUE-384 names is a run of failures *with Chrome alive*.

        Chrome being down, relaunching, or silent on DevTools each explains a
        failed connect on its own, and the probe's first two arms already report
        those three states. Counting them here only buys a stale count that
        outlives the condition.
        """
        _poisoned_playwright(monkeypatch)
        chrome._chrome_proc = FakeProc(alive=False)
        monkeypatch.setattr(chrome, "devtools_responding", lambda timeout=2: True)

        with pytest.raises(RuntimeError):
            chrome.connect_cdp()

        h = chrome.cdp_health()
        assert h["consecutive_failures"] == 0, "a dead Chrome must not count"
        assert h["last_failure"] > 0.0, "but it is still recorded for diagnosis"
        assert "asyncio loop" in h["last_error"]

    def test_a_relaunch_in_progress_does_not_count(self, monkeypatch):
        """The measured recovery case: recover_wedged_chrome kills Chrome, the
        unwinding request calls _close_session -> get_context during the
        relaunch, and that used to record a failure for a recovery that worked.
        """
        _poisoned_playwright(monkeypatch)
        chrome._chrome_proc = FakeProc(alive=True)
        chrome._launching = True
        monkeypatch.setattr(chrome, "devtools_responding", lambda timeout=2: True)

        with pytest.raises(RuntimeError):
            chrome.connect_cdp()

        assert chrome.cdp_health()["consecutive_failures"] == 0

    def test_a_silent_devtools_endpoint_does_not_count(self, monkeypatch):
        _poisoned_playwright(monkeypatch)
        chrome._chrome_proc = FakeProc(alive=True)
        chrome._launching = False
        monkeypatch.setattr(chrome, "devtools_responding", lambda timeout=2: False)

        with pytest.raises(RuntimeError):
            chrome.connect_cdp()

        assert chrome.cdp_health()["consecutive_failures"] == 0

    def test_record_false_keeps_a_failure_out_of_the_count(self, monkeypatch):
        _healthy_chrome(monkeypatch)
        _poisoned_playwright(monkeypatch)

        with pytest.raises(RuntimeError):
            chrome.connect_cdp(record=False)

        assert chrome.cdp_health()["consecutive_failures"] == 0

    def test_record_false_still_lets_a_success_clear_the_count(self, monkeypatch):
        """A success is never a reason to restart, so there is no false positive
        to protect against -- and a teardown that reached the browser is real
        evidence the binding works.
        """
        _healthy_chrome(monkeypatch)
        _write_cdp_health(consecutive_failures=5)
        _working_playwright(monkeypatch)

        chrome.connect_cdp(record=False)

        assert chrome.cdp_health()["consecutive_failures"] == 0

    def test_the_record_survives_concurrent_writers_and_readers(self, monkeypatch):
        """The leaf lock, exercised rather than asserted in a comment.

        Every reader must see one consistent set of fields, and the count must
        not lose an increment to a race.
        """
        _healthy_chrome(monkeypatch)
        errors = []

        def _writer():
            try:
                for _ in range(200):
                    chrome._record_cdp_failure(RuntimeError("x"))
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        def _reader():
            try:
                for _ in range(200):
                    h = chrome.cdp_health()
                    assert set(h) == {
                        "last_success", "last_failure",
                        "consecutive_failures", "last_error",
                    }
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=_writer) for _ in range(3)]
        threads += [threading.Thread(target=_reader) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads), "a thread did not finish"
        assert errors == []
        assert chrome.cdp_health()["consecutive_failures"] == 600

    def test_the_snapshot_is_a_copy(self):
        h = chrome.cdp_health()
        h["consecutive_failures"] = 99
        assert chrome.cdp_health()["consecutive_failures"] == 0


class TestTheDeepProbeThirdArm:
    """The regression: a wedged binding behind a healthy Chrome must read 503."""

    def _wedge(self, count=None, age_s=0.0):
        _write_cdp_health(
            consecutive_failures=(
                browse_api.CDP_FAILURE_THRESHOLD if count is None else count
            ),
            last_failure=time.monotonic() - age_s,
            last_error="It looks like you are using Playwright Sync API",
        )

    def test_a_wedged_binding_behind_a_healthy_chrome_is_unhealthy(self, monkeypatch):
        _healthy_chrome(monkeypatch)
        self._wedge()
        status, body = browse_api._probe(deep=True)
        assert status == 503
        assert body == b"cdp-wedged\n"

    def test_the_same_container_passes_the_two_old_tiers(self, monkeypatch):
        """Names what stayed green for eight hours, so the arm is the difference."""
        _healthy_chrome(monkeypatch)
        self._wedge()
        assert chrome.is_chrome_running() is True
        assert chrome.devtools_responding() is True
        assert browse_api._probe(deep=False) == (200, b"ok\n")

    def test_one_failure_short_of_the_threshold_still_passes(self, monkeypatch):
        _healthy_chrome(monkeypatch)
        self._wedge(count=browse_api.CDP_FAILURE_THRESHOLD - 1)
        assert browse_api._probe(deep=True) == (200, b"ok\n")

    def test_stale_failures_do_not_hold_the_verdict_red(self, monkeypatch):
        """An old burst that has since gone quiet must not earn a restart."""
        _healthy_chrome(monkeypatch)
        self._wedge(age_s=browse_api.CDP_FAILURE_WINDOW_S + 1)
        assert browse_api._probe(deep=True) == (200, b"ok\n")

    def test_an_idle_container_that_never_touched_cdp_passes(self, monkeypatch):
        """The false-positive shape: absence of success is not evidence of a fault."""
        _healthy_chrome(monkeypatch)
        assert chrome.cdp_health()["last_success"] == 0.0
        assert browse_api._probe(deep=True) == (200, b"ok\n")

    def test_a_success_after_the_failures_clears_the_verdict(self, monkeypatch):
        _healthy_chrome(monkeypatch)
        self._wedge()
        assert browse_api._probe(deep=True)[0] == 503
        _working_playwright(monkeypatch)
        chrome.connect_cdp()
        assert browse_api._probe(deep=True) == (200, b"ok\n")

    def test_the_cheap_tier_ignores_the_record(self, monkeypatch):
        _healthy_chrome(monkeypatch)
        self._wedge()
        assert browse_api._probe(deep=False) == (200, b"ok\n")

    def test_a_dead_chrome_still_wins(self, monkeypatch):
        """Ordering: the cheap tier's verdict is the more specific one."""
        _healthy_chrome(monkeypatch)
        chrome._chrome_proc = FakeProc(alive=False)
        self._wedge()
        assert browse_api._probe(deep=True) == (503, b"chrome-down\n")

    def test_the_launch_window_is_not_exempt_from_this_arm(self, monkeypatch):
        """is_launching() exempts the DevTools tier, not a wedged binding.

        A relaunch explains DevTools being absent for a few seconds. It explains
        nothing about a run of CDP failures that already happened, and the
        watchdog's own Chrome restart is what puts the container in this window
        -- so exempting it here would make the fault invisible for exactly as
        long as the recovery attempt that cannot fix it.
        """
        _healthy_chrome(monkeypatch)
        monkeypatch.setattr(chrome, "is_launching", lambda: True)
        self._wedge()
        assert browse_api._probe(deep=True) == (503, b"cdp-wedged\n")

    def test_the_arm_can_be_switched_off(self, monkeypatch):
        _healthy_chrome(monkeypatch)
        self._wedge()
        monkeypatch.setattr(browse_api, "CDP_FAILURE_THRESHOLD", 0)
        assert browse_api._probe(deep=True) == (200, b"ok\n")

    def test_the_probe_touches_no_patchright_machinery(self, monkeypatch):
        """ISSUE-382's lesson: the liveness thread must never drive Patchright.

        Reading a counter and a timestamp is thread-safe in the way calling
        connect_cdp is not, and this is what holds that apart.
        """
        _healthy_chrome(monkeypatch)
        self._wedge()

        def _forbidden(*_a, **_k):
            raise AssertionError("the liveness probe called Patchright")

        monkeypatch.setattr(chrome, "connect_cdp", _forbidden)
        monkeypatch.setattr(chrome, "get_context", _forbidden)
        monkeypatch.setattr(chrome, "get_page_by_index", _forbidden)
        monkeypatch.setattr(chrome, "disconnect_cdp", _forbidden)

        assert browse_api._probe(deep=True) == (503, b"cdp-wedged\n")

    def test_the_probe_answers_from_a_foreign_thread(self, monkeypatch):
        """The real caller is the liveness server's own thread."""
        _healthy_chrome(monkeypatch)
        self._wedge()
        result, err = _run_on_another_thread(lambda: browse_api._probe(deep=True))
        assert err is None
        assert result == (503, b"cdp-wedged\n")


class TestWhatMustNeverEarnARestart:
    """The false positives found reviewing this change, each driven end to end.

    Every one of these is a container restart and a killed browsing session if
    it regresses, on a container that was working.
    """

    def _broken_cdp(self, monkeypatch):
        """Chrome healthy, but every CDP connect fails -- the countable state.

        The reuse branch has to fail on a live-looking handle and the retry loop
        has to fail behind it, which is exactly the ISSUE-382 shape: a stale
        websocket plus a Patchright that refuses to start. `_pw_context` must
        carry a page, or `_close_session_unlocked` returns at
        `get_page_by_index` and never reaches the call under test -- that
        omission made the first version of this test pass with the bug
        reinstated.
        """
        _healthy_chrome(monkeypatch)
        _poisoned_playwright(monkeypatch)

        def _arm():
            stale = mock.MagicMock(name="stale_browser")
            stale.new_browser_cdp_session.side_effect = RuntimeError("socket is dead")
            ctx = mock.MagicMock(name="stale_context")
            ctx.pages = [mock.MagicMock(name="page0"), mock.MagicMock(name="page1")]
            chrome._pw_browser = stale
            chrome._pw_context = ctx
            chrome._pw_thread_id = threading.get_ident()

        return _arm

    def test_expired_session_cleanup_never_counts(self, monkeypatch):
        """_cleanup_expired() runs at the top of EVERY endpoint.

        Its eviction path reaches get_context() behind `except Exception: pass`,
        and an endpoint that then returns 404 never makes a CDP call of its own
        -- so nothing can produce a compensating success. Three /interact calls
        carrying stale session ids used to restart the container.
        """
        arm = self._broken_cdp(monkeypatch)
        for i in range(5):
            arm()  # a fresh request finds the connection live-looking again
            browse_api._sessions[f"stale-{i}"] = {
                "tab_index": 0, "created_at": time.time() - browse_api.SESSION_TTL - 60,
            }
            browse_api._cleanup_expired()

        assert chrome.cdp_health()["consecutive_failures"] == 0
        assert browse_api._probe(deep=True) == (200, b"ok\n")

    def test_a_diagnostics_poll_never_counts(self, monkeypatch):
        """/health?v=1 must not be able to restart what it reports on."""
        arm = self._broken_cdp(monkeypatch)
        for _ in range(5):
            arm()
            browse_api._get_chrome_diagnostics()

        assert chrome.cdp_health()["consecutive_failures"] == 0
        assert browse_api._probe(deep=True) == (200, b"ok\n")

    def test_a_wall_clock_jump_does_not_arm_the_verdict(self, monkeypatch):
        """The record is monotonic, so an NTP step cannot move the age.

        A forward step used to re-arm a verdict that had aged out; the stamp is
        never rendered anywhere, so nothing is lost by not using wall clock.
        """
        _healthy_chrome(monkeypatch)
        # Stamped by the real recorder, so the clock under test is the one the
        # module actually uses rather than one the test chose.
        for _ in range(browse_api.CDP_FAILURE_THRESHOLD):
            chrome._record_cdp_failure(RuntimeError("wedged"))
        assert browse_api._probe(deep=True)[0] == 503

        jump = browse_api.CDP_FAILURE_WINDOW_S * 10
        real_time = time.time
        monkeypatch.setattr(browse_api.time, "time", lambda: real_time() + jump)
        monkeypatch.setattr(chrome.time, "time", lambda: real_time() + jump)

        assert browse_api._probe(deep=True)[0] == 503, (
            "a wall-clock jump aged out a wedge that is still live"
        )


class TestThePolicyKnobs:
    def test_the_window_is_clamped_to_the_detection_floor(self):
        """A 0 meaning "no staleness cutoff" would silently disable the arm.

        `age <= 0` is false for any real elapsed time, so honouring it gives the
        operator the opposite of what they asked for. Below the floor the arm is
        unreachable rather than merely eager: the healthcheck needs ~90s to say
        unhealthy and the watchdog ~120s more to act.
        """
        assert browse_api.CDP_FAILURE_WINDOW_S >= browse_api.CDP_WINDOW_FLOOR_S

    def test_the_disabled_branch_returns_the_full_record(self, monkeypatch):
        """One tuple shape on every path.

        A consumer indexing the second element must not raise: an exception in
        _probe escapes do_GET, the handler answers nothing, `curl -sf` fails,
        and the switch meant to turn the arm off causes the restart it prevents.
        """
        monkeypatch.setattr(browse_api, "CDP_FAILURE_THRESHOLD", 0)
        wedged, cdp = browse_api._cdp_wedged()
        assert wedged is False
        assert set(cdp) == {
            "last_success", "last_failure", "consecutive_failures", "last_error",
        }

    def test_the_window_boundary_is_inclusive(self, monkeypatch):
        """Pins `age <= W` rather than `age < W`; the count boundary has its own."""
        _healthy_chrome(monkeypatch)
        stamp = 1000.0
        _write_cdp_health(
            consecutive_failures=browse_api.CDP_FAILURE_THRESHOLD,
            last_failure=stamp,
            last_error="boundary",
        )
        window = browse_api.CDP_FAILURE_WINDOW_S
        assert browse_api._cdp_wedged(now=stamp + window)[0] is True
        assert browse_api._cdp_wedged(now=stamp + window + 1)[0] is False


class TestTheWedgeIsLoggedOnce:
    """The liveness thread must not block, and logging does."""

    @pytest.fixture(autouse=True)
    def _clear_flag(self):
        browse_api._cdp_wedge_reported = False
        yield
        browse_api._cdp_wedge_reported = False

    def test_a_sustained_wedge_logs_one_line_not_one_per_probe(
        self, monkeypatch, caplog,
    ):
        _healthy_chrome(monkeypatch)
        _write_cdp_health(
            consecutive_failures=browse_api.CDP_FAILURE_THRESHOLD,
            last_failure=time.monotonic(),
            last_error="It looks like you are using Playwright Sync API",
        )
        with caplog.at_level("ERROR", logger="browse_api"):
            for _ in range(20):
                assert browse_api._probe(deep=True)[0] == 503
        wedge_lines = [r for r in caplog.records if "ISSUE-384" in r.getMessage()]
        assert len(wedge_lines) == 1

    def test_recovery_is_logged_and_re_arms_the_report(self, monkeypatch, caplog):
        _healthy_chrome(monkeypatch)
        _write_cdp_health(
            consecutive_failures=browse_api.CDP_FAILURE_THRESHOLD,
            last_failure=time.monotonic(),
            last_error="wedged",
        )
        with caplog.at_level("INFO", logger="browse_api"):
            assert browse_api._probe(deep=True)[0] == 503
            _write_cdp_health(consecutive_failures=0)
            assert browse_api._probe(deep=True) == (200, b"ok\n")
            assert any(
                "recovered" in r.getMessage() for r in caplog.records
            )
            # A second wedge reports again rather than staying silent.
            caplog.clear()
            _write_cdp_health(
                consecutive_failures=browse_api.CDP_FAILURE_THRESHOLD,
                last_failure=time.monotonic(),
            )
            assert browse_api._probe(deep=True)[0] == 503
        assert any("ISSUE-384" in r.getMessage() for r in caplog.records)


class TestHealthEndpoint:
    """/health reported ok throughout the outage; a manual probe must see it."""

    @pytest.fixture(autouse=True)
    def _plain_request(self, monkeypatch):
        """health() reads request.args to decide whether to add diagnostics."""
        monkeypatch.setattr(
            browse_api, "request", types.SimpleNamespace(args={}), raising=False,
        )

    def test_a_wedged_binding_degrades_the_status(self, monkeypatch):
        _healthy_chrome(monkeypatch)
        _write_cdp_health(
            consecutive_failures=browse_api.CDP_FAILURE_THRESHOLD,
            last_failure=time.monotonic(),
            last_error="It looks like you are using Playwright Sync API",
        )
        data = browse_api.health()
        assert data["status"] == "degraded"
        assert data["cdp_healthy"] is False
        assert data["cdp_consecutive_failures"] == browse_api.CDP_FAILURE_THRESHOLD
        assert "Playwright Sync API" in data["cdp_last_error"]

    def test_the_counters_are_reported_even_with_the_arm_switched_off(
        self, monkeypatch,
    ):
        """Switching the arm off means "do not restart for this", not "stop
        reporting it" -- an operator who turned it off is the one who most needs
        to see the evidence by hand.
        """
        _healthy_chrome(monkeypatch)
        monkeypatch.setattr(browse_api, "CDP_FAILURE_THRESHOLD", 0)
        _write_cdp_health(consecutive_failures=9, last_error="still broken")
        data = browse_api.health()
        assert data["status"] == "ok", "no restart is earned with the arm off"
        assert data["cdp_healthy"] is True
        assert data["cdp_consecutive_failures"] == 9
        assert data["cdp_last_error"] == "still broken"

    def test_a_healthy_container_is_unchanged(self, monkeypatch):
        _healthy_chrome(monkeypatch)
        data = browse_api.health()
        assert data["status"] == "ok"
        assert data["cdp_healthy"] is True
        assert data["cdp_last_error"] == ""
