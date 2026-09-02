"""The browser container's Chrome process hygiene: stderr, reaping, staleness.

The wedge these cover was diagnosed on the production container. ``launch_chrome``
passed ``--enable-logging=stderr`` and ``stderr=subprocess.PIPE``, and nothing in
``docker/browser/`` ever read that pipe. Chrome's own wrapper script routes its
stderr through a ``cat`` into it, so the 64 KiB pipe filled after two or three
renders -- measured on the live container at 15900, then 46445, then 63199 of
65536 bytes, with the ``cat`` blocked in ``pipe_write`` -- and the next log write
on Chrome's browser UI thread blocked forever.

That produces a Chrome that is alive by every measure the container had. CSS
animations keep running, because compositing is a different thread in a different
process. ``/json/version`` keeps answering, because DevTools HTTP is served on the
browser IO thread. So ``/live?deep=1`` stayed green through every wedge, and the
only actor was the in-process browse watchdog, which killed Chrome after 90s and
relaunched it into the same condition.

Four defects rode along with it, each covered here:

* ``proc.kill()`` with no ``proc.wait()`` after ``terminate()`` timed out, and no
  process group, so the browser became a zombie and its renderer/GPU/zygote
  children were orphaned. 43 zombies accumulated in 40 minutes of production.
* ``_sessions`` survived a relaunch holding tab indices into a Chrome that no
  longer existed, so every request against a pre-kill session id returned
  "Tab not found" for the rest of the 600s TTL.
* Nothing escalated a recovery loop. The heartbeat arm only observes
  ``connect_cdp``, and the successful connect after each relaunch zeroes its
  counter, so a wedge every 90s reads healthy forever.
* No JavaScript dialog handling at all, under an Xvfb with no window manager, so
  a modal nothing can dismiss is an independent second way into the same state.

Stubbing follows ``test_browser_cdp_heartbeat.py``: the browser app runs only
inside its own Docker image and depends on ``patchright`` and ``flask``, neither
installed in the istota test env.
"""

import ast
import signal
import subprocess
import sys
import time
import types
from pathlib import Path
from unittest import mock

import pytest
import yaml

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

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BROWSER_DIR = _REPO_ROOT / "docker" / "browser"
if str(_BROWSER_DIR) not in sys.path:
    sys.path.insert(0, str(_BROWSER_DIR))

import chrome  # noqa: E402  (import after the stubs + path insert)

pytest.importorskip("bs4", reason="browser render module needs bs4")

import browse_api  # noqa: E402  (import after the stubs + path insert)


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
        chrome._launch_generation = 0
        with chrome._cdp_health_lock:
            chrome._cdp_health.update(
                last_success=0.0, last_failure=0.0,
                consecutive_failures=0, last_error="",
            )
        with chrome._wedge_lock:
            chrome._wedge_recoveries.clear()
        browse_api._sessions.clear()
        browse_api._evict_request.clear()
        browse_api._wedge_loop_reported = False

    _reset()
    yield
    _reset()


class FakeProc:
    """A subprocess.Popen stand-in that records how it was signalled and reaped."""

    def __init__(self, alive=True, pid=1234, terminate_works=True):
        self.pid = pid
        self._alive = alive
        self._terminate_works = terminate_works
        self.terminated = False
        self.killed = False
        self.waits = []          # every wait() call, in order
        self.waits_after_kill = 0

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        if self._terminate_works:
            self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.killed:
            self.waits_after_kill += 1
        if self._alive:
            raise subprocess.TimeoutExpired("chrome", timeout)
        return 0


@pytest.fixture
def launched(monkeypatch):
    """Capture the argv and kwargs of the next launch_chrome() Popen."""
    calls = []

    def _popen(args, **kwargs):
        calls.append({"args": list(args), "kwargs": kwargs})
        return FakeProc()

    monkeypatch.setattr(chrome.subprocess, "Popen", _popen)
    monkeypatch.setattr(chrome, "_wait_for_chrome_ready", lambda *a, **k: None)
    return calls


# ---------------------------------------------------------------------------
# The wedge itself: Chrome's stderr must never be an unread pipe
# ---------------------------------------------------------------------------

class TestChromeStderrIsNeverAnUnreadPipe:
    """The root cause. A PIPE nobody reads blocks Chrome's UI thread at 64 KiB."""

    def test_stderr_is_not_a_pipe(self, launched):
        chrome.launch_chrome()
        assert launched[0]["kwargs"]["stderr"] is not subprocess.PIPE

    def test_stderr_goes_to_devnull_by_default(self, launched, monkeypatch):
        monkeypatch.delenv("CHROME_LOG_STDERR", raising=False)
        chrome.launch_chrome()
        assert launched[0]["kwargs"]["stderr"] is subprocess.DEVNULL

    def test_stderr_logging_flags_are_absent_by_default(self, launched, monkeypatch):
        """Nothing consumes the log, so asking Chrome to produce it is all cost."""
        monkeypatch.delenv("CHROME_LOG_STDERR", raising=False)
        chrome.launch_chrome()
        args = launched[0]["args"]
        assert "--enable-logging=stderr" not in args
        assert not any(a.startswith("--v=") for a in args)

    def test_opting_in_inherits_rather_than_pipes(self, launched, monkeypatch):
        """CHROME_LOG_STDERR sends the log to the container's own stderr.

        Inherited, not piped: the container's stderr is drained by the Docker
        log collector, so it cannot fill. A PIPE here would be the production
        wedge restored behind a flag that reads like a debugging convenience.
        """
        monkeypatch.setenv("CHROME_LOG_STDERR", "1")
        chrome.launch_chrome()
        assert launched[0]["kwargs"]["stderr"] is None
        assert "--enable-logging=stderr" in launched[0]["args"]

    def test_opting_in_still_never_uses_a_pipe(self, launched, monkeypatch):
        for value in ("1", "true", "yes", "on"):
            launched.clear()
            monkeypatch.setenv("CHROME_LOG_STDERR", value)
            chrome.launch_chrome()
            assert launched[0]["kwargs"]["stderr"] is not subprocess.PIPE

    def test_stdout_is_still_sunk(self, launched):
        chrome.launch_chrome()
        assert launched[0]["kwargs"]["stdout"] is subprocess.DEVNULL


# ---------------------------------------------------------------------------
# Reaping: the process group, and the wait() after kill()
# ---------------------------------------------------------------------------

class TestChromeIsReaped:
    """43 zombies in 40 minutes of production, from one missing wait()."""

    def test_chrome_leads_its_own_session(self, launched):
        """Without a group of its own there is nothing safe to signal."""
        chrome.launch_chrome()
        assert launched[0]["kwargs"]["start_new_session"] is True

    def test_a_killed_chrome_is_waited_on(self, monkeypatch):
        """The production defect: kill() with no wait() leaves a zombie forever."""
        proc = FakeProc(terminate_works=False)
        monkeypatch.setattr(chrome, "_signal_group", lambda *a, **k: False)

        chrome._kill_chrome_proc(proc)

        assert proc.killed is True
        assert proc.waits_after_kill >= 1

    def test_a_cooperative_chrome_is_waited_on_too(self, monkeypatch):
        proc = FakeProc(terminate_works=True)
        monkeypatch.setattr(chrome, "_signal_group", lambda *a, **k: False)

        chrome._kill_chrome_proc(proc)

        assert proc.waits, "terminate() path must reap as well"
        assert proc.killed is False

    def test_the_whole_process_group_is_signalled(self, monkeypatch):
        """Renderers, GPU and zygotes are children of Chrome, not of Python."""
        sent = []
        monkeypatch.setattr(chrome.os, "getpgid", lambda pid: pid)
        monkeypatch.setattr(
            chrome.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)),
        )

        chrome._kill_chrome_proc(FakeProc(terminate_works=False, pid=4242))

        assert (4242, signal.SIGTERM) in sent
        assert (4242, signal.SIGKILL) in sent

    def test_a_process_that_leads_no_group_is_signalled_alone(self, monkeypatch):
        """killpg against the daemon's own group would kill the API process."""
        sent = []
        # pid 4242 sitting in group 1 -- not a leader, so its group is shared.
        monkeypatch.setattr(chrome.os, "getpgid", lambda pid: 1)
        monkeypatch.setattr(
            chrome.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)),
        )
        proc = FakeProc(terminate_works=True, pid=4242)

        chrome._kill_chrome_proc(proc)

        assert sent == [], "must not signal a group this process shares"
        assert proc.terminated is True

    def test_killing_never_raises(self, monkeypatch):
        """It runs from a watchdog thread; an exception there reports a false heal."""
        monkeypatch.setattr(
            chrome.os, "getpgid",
            mock.Mock(side_effect=ProcessLookupError("gone")),
        )
        broken = mock.Mock()
        broken.pid = 9
        broken.terminate.side_effect = OSError("boom")
        broken.kill.side_effect = OSError("boom")
        broken.wait.side_effect = OSError("boom")

        chrome._kill_chrome_proc(broken)  # must not raise

    def test_every_kill_path_goes_through_the_helper(self, monkeypatch):
        """restart, recover and cleanup had three copies of the same broken code."""
        killed = []
        monkeypatch.setattr(chrome, "_kill_chrome_proc", lambda p, **k: killed.append(p))
        monkeypatch.setattr(chrome, "launch_chrome", lambda: None)
        monkeypatch.setattr(chrome, "disconnect_cdp", lambda: None)

        for fn in (chrome.restart_chrome, chrome.recover_wedged_chrome, chrome.cleanup):
            killed.clear()
            chrome._chrome_proc = FakeProc()
            fn()
            assert len(killed) == 1, f"{fn.__name__} did not use the shared kill path"


# ---------------------------------------------------------------------------
# Stale sessions across a relaunch
# ---------------------------------------------------------------------------

class TestSessionsDoNotSurviveARelaunch:
    """A tab index into a Chrome that no longer exists is not a session."""

    def test_the_generation_advances_on_every_launch(self, launched):
        before = chrome.launch_generation()
        chrome.launch_chrome()
        assert chrome.launch_generation() == before + 1

    def test_recovery_advances_the_generation(self, monkeypatch):
        monkeypatch.setattr(chrome, "_kill_chrome_proc", lambda p, **k: None)
        monkeypatch.setattr(chrome.subprocess, "Popen", lambda *a, **k: FakeProc())
        monkeypatch.setattr(chrome, "_wait_for_chrome_ready", lambda *a, **k: None)
        before = chrome.launch_generation()

        chrome.recover_wedged_chrome()

        assert chrome.launch_generation() > before

    def test_a_session_from_an_older_generation_is_dropped(self):
        browse_api._sessions["s1"] = {
            "tab_index": 1,
            "created_at": time.time(),
            "generation": chrome.launch_generation(),
        }
        chrome._launch_generation += 1

        assert browse_api._get_session("s1") is None
        assert "s1" not in browse_api._sessions

    def test_a_session_from_the_current_generation_survives(self):
        browse_api._sessions["s1"] = {
            "tab_index": 1,
            "created_at": time.time(),
            "generation": chrome.launch_generation(),
        }

        assert browse_api._get_session("s1") is not None

    def test_a_new_session_records_the_generation(self, monkeypatch):
        ctx = mock.MagicMock()
        ctx.pages = [mock.MagicMock(), mock.MagicMock()]
        monkeypatch.setattr(browse_api, "_get_memory_pct", lambda: 10)
        monkeypatch.setattr(chrome, "connect_cdp", lambda **k: None)
        monkeypatch.setattr(chrome, "get_context", lambda **k: ctx)

        sid, _ = browse_api._create_session()

        assert browse_api._sessions[sid]["generation"] == chrome.launch_generation()


# ---------------------------------------------------------------------------
# Escalation: a recovery loop must eventually read unhealthy
# ---------------------------------------------------------------------------

class TestARecoveryLoopEscalates:
    """The watchdog healing the same wedge every 90s is not a healthy container."""

    def test_recovery_is_recorded(self, monkeypatch):
        monkeypatch.setattr(chrome, "_kill_chrome_proc", lambda p, **k: None)
        monkeypatch.setattr(chrome, "launch_chrome", lambda: None)

        chrome.recover_wedged_chrome()

        assert len(chrome.wedge_recovery_history()) == 1

    def test_one_recovery_is_not_a_loop(self, monkeypatch):
        monkeypatch.setattr(browse_api, "WEDGE_RECOVERY_THRESHOLD", 3)
        chrome.record_wedge_recovery()

        assert browse_api._wedge_looping()[0] is False

    def test_the_threshold_is_a_loop(self, monkeypatch):
        monkeypatch.setattr(browse_api, "WEDGE_RECOVERY_THRESHOLD", 3)
        for _ in range(3):
            chrome.record_wedge_recovery()

        assert browse_api._wedge_looping()[0] is True

    def test_recoveries_outside_the_window_do_not_count(self, monkeypatch):
        monkeypatch.setattr(browse_api, "WEDGE_RECOVERY_THRESHOLD", 3)
        monkeypatch.setattr(browse_api, "WEDGE_RECOVERY_WINDOW_S", 600)
        old = time.monotonic() - 3600
        with chrome._wedge_lock:
            chrome._wedge_recoveries.extend([old, old, old])

        assert browse_api._wedge_looping()[0] is False

    def test_zero_disables_the_arm(self, monkeypatch):
        monkeypatch.setattr(browse_api, "WEDGE_RECOVERY_THRESHOLD", 0)
        for _ in range(20):
            chrome.record_wedge_recovery()

        assert browse_api._wedge_looping()[0] is False

    def test_the_deep_probe_reports_the_loop(self, monkeypatch):
        monkeypatch.setattr(browse_api, "WEDGE_RECOVERY_THRESHOLD", 2)
        monkeypatch.setattr(chrome, "is_chrome_running", lambda: True)
        monkeypatch.setattr(chrome, "devtools_responding", lambda timeout=2: True)
        monkeypatch.setattr(chrome, "is_launching", lambda: False)
        for _ in range(2):
            chrome.record_wedge_recovery()

        assert browse_api._probe(deep=True) == (503, b"wedge-loop\n")

    def test_the_shallow_probe_is_unaffected(self, monkeypatch):
        monkeypatch.setattr(browse_api, "WEDGE_RECOVERY_THRESHOLD", 2)
        monkeypatch.setattr(chrome, "is_chrome_running", lambda: True)
        for _ in range(5):
            chrome.record_wedge_recovery()

        assert browse_api._probe(deep=False) == (200, b"ok\n")

    def test_the_history_is_bounded(self):
        """A record kept per recovery must not grow for the life of the process."""
        for _ in range(5000):
            chrome.record_wedge_recovery()

        assert len(chrome._wedge_recoveries) <= chrome.WEDGE_HISTORY_MAX

    def test_a_healthy_container_still_passes(self, monkeypatch):
        monkeypatch.setattr(chrome, "is_chrome_running", lambda: True)
        monkeypatch.setattr(chrome, "devtools_responding", lambda timeout=2: True)
        monkeypatch.setattr(chrome, "is_launching", lambda: False)

        assert browse_api._probe(deep=True) == (200, b"ok\n")


# ---------------------------------------------------------------------------
# Modal browser UI nothing in this container can dismiss
# ---------------------------------------------------------------------------

class TestModalUiCannotBlockTheMainThread:
    """Xvfb runs with no window manager, and input goes through xdotool.

    A JavaScript dialog blocks the renderer main thread until something answers
    it. Nothing here can: xdotool targets the largest window, which is the
    browser rather than the modal child, and Patchright's auto-dismiss only
    covers pages it is currently attached to.
    """

    @pytest.mark.parametrize("flag", [
        "--noerrdialogs",
        "--disable-hang-monitor",
        "--disable-prompt-on-repost",
        "--disable-session-crashed-bubble",
        "--disable-print-preview",
    ])
    def test_the_suppression_flags_are_passed(self, launched, flag):
        chrome.launch_chrome()
        assert flag in launched[0]["args"]

    def test_a_dialog_handler_is_registered_on_connect(self, monkeypatch):
        page = mock.MagicMock()
        ctx = mock.MagicMock()
        ctx.pages = [page]
        browser = mock.MagicMock()
        browser.contexts = [ctx]
        started = mock.MagicMock()
        started.chromium.connect_over_cdp.return_value = browser
        sp = mock.MagicMock()
        sp.start.return_value = started
        monkeypatch.setattr(chrome, "sync_playwright", lambda: sp)

        chrome.connect_cdp()

        assert "dialog" in {c.args[0] for c in page.on.call_args_list}
        assert "page" in {c.args[0] for c in ctx.on.call_args_list}

    def test_the_handler_dismisses(self):
        dialog = mock.MagicMock()
        dialog.type = "beforeunload"
        dialog.message = "Leave site?"

        chrome._dismiss_dialog(dialog)

        dialog.dismiss.assert_called_once()

    def test_the_handler_never_raises(self):
        """It runs on Patchright's event dispatcher; an exception there is a wedge."""
        dialog = mock.MagicMock()
        dialog.dismiss.side_effect = Exception("target closed")

        chrome._dismiss_dialog(dialog)  # must not raise

    def test_registering_never_raises(self):
        """A page that closed between listing and registering must not fail connect."""
        page = mock.MagicMock()
        page.on.side_effect = Exception("target closed")
        ctx = mock.MagicMock()
        ctx.pages = [page]

        chrome._install_dialog_guards(ctx)  # must not raise


# ---------------------------------------------------------------------------
# The container needs an init to reap what still escapes
# ---------------------------------------------------------------------------

class TestTheContainerHasAnInit:
    """PID 1 is `su`, which reaps no orphan. Chrome's children reparent to it."""

    def test_the_compose_stack_runs_an_init(self):
        compose = yaml.safe_load(
            (_REPO_ROOT / "docker" / "docker-compose.yml").read_text(),
        )
        assert compose["services"]["browser"].get("init") is True

    def test_the_ansible_template_runs_an_init(self):
        template = (
            _REPO_ROOT / "deploy" / "ansible" / "templates"
            / "docker-compose.browser.yml.j2"
        ).read_text()
        assert "init: true" in template


# ---------------------------------------------------------------------------
# The vendored copy must match its source of truth
# ---------------------------------------------------------------------------

def test_no_popen_in_the_container_pipes_a_stream():
    """The one-line invariant behind the whole file.

    Nothing in this package reads a child's output -- there is no reader thread,
    no ``communicate()``, no ``select``. So a ``PIPE`` here is always an
    undrained one, and an undrained pipe stops the child dead at 64 KiB. That
    makes the rule checkable exactly rather than heuristically: no ``Popen`` in
    ``docker/browser/`` may pass ``PIPE`` for ``stdout`` or ``stderr`` at all.

    Structural rather than a grep, so the prose in ``launch_chrome`` explaining
    why the constant must never come back cannot itself trip it -- and so a
    future ``PIPE`` cannot hide behind a comment either.

    If a reader is ever added, this is the test to change, and changing it is
    the point at which someone has to say where the reader is.
    """
    offenders = []
    for path in sorted(_BROWSER_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute) and node.func.attr == "Popen"):
                continue
            for kw in node.keywords:
                if kw.arg not in ("stdout", "stderr"):
                    continue
                if isinstance(kw.value, ast.Attribute) and kw.value.attr == "PIPE":
                    offenders.append(f"{path.name}:{node.lineno} {kw.arg}=PIPE")

    assert offenders == [], (
        "a subprocess PIPE with no reader blocks the child at 64 KiB -- this is "
        f"the production wedge (ISSUE-394): {offenders}"
    )
