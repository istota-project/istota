"""The stop notice the SSE streams observe (`istota.web_shutdown`).

The end-to-end proof that this ends a real stream under a real uvicorn is
`tests/test_serve_shutdown.py`. These cover the pieces that file cannot see:
the wake happening at once rather than at the end of the interval, the race
between a sleeper registering and the signal arriving, and what the signal hook
does to a handler it should not touch.
"""

from __future__ import annotations

import asyncio
import signal
import threading
import time

import pytest

from istota import web_shutdown


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset the module state, and put back *both* signal handlers.

    `install_signal_hook` wraps SIGINT as well as SIGTERM, and pytest's own
    SIGINT handler is callable, so a test that restored only the one it asserts
    on left this worker's Ctrl-C running through a `web_shutdown` closure for
    the rest of the session. The suite runs under `-n auto`, so process-global
    state outliving the test that made it is exactly the shape that breaks
    another file later.
    """
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    web_shutdown.reset_for_tests()
    try:
        yield
    finally:
        web_shutdown.reset_for_tests()
        for sig, handler in saved.items():
            signal.signal(sig, handler)


class TestSleepUnlessShutdown:
    def test_a_quiet_interval_elapses_and_reports_it(self):
        async def _drive():
            started = time.monotonic()
            slept = await web_shutdown.sleep_unless_shutdown(0.05)
            return slept, time.monotonic() - started

        slept, elapsed = asyncio.run(_drive())
        assert slept is True
        assert elapsed >= 0.05

    def test_shutdown_wakes_a_sleeper_immediately(self):
        """The point of the module: a stream polling on a one-second interval
        must not hold the shutdown for the rest of that interval."""

        async def _drive():
            sleeper = asyncio.create_task(web_shutdown.sleep_unless_shutdown(30))
            await asyncio.sleep(0.05)  # let it register
            started = time.monotonic()
            web_shutdown.begin_shutdown()
            return await sleeper, time.monotonic() - started

        slept, elapsed = asyncio.run(_drive())
        assert slept is False
        assert elapsed < 5, "the sleeper waited out its interval"

    def test_a_sleep_started_after_shutdown_returns_at_once(self):
        async def _drive():
            web_shutdown.begin_shutdown()
            started = time.monotonic()
            slept = await web_shutdown.sleep_unless_shutdown(30)
            return slept, time.monotonic() - started

        slept, elapsed = asyncio.run(_drive())
        assert slept is False
        assert elapsed < 5

    def test_a_waiter_is_dropped_when_its_sleep_ends(self):
        """Every poll tick registers one. Leaking them would grow a list for as
        long as a session-lived stream stays open."""

        async def _drive():
            await web_shutdown.sleep_unless_shutdown(0.01)
            await web_shutdown.sleep_unless_shutdown(0.01)

        asyncio.run(_drive())
        assert web_shutdown._waiters == []

    def test_shutdown_from_another_thread_wakes_the_loop(self):
        """A signal handler runs on the main thread while the stream's loop may
        be anywhere; the wake is deferred onto the waiter's own loop."""

        async def _drive():
            sleeper = asyncio.create_task(web_shutdown.sleep_unless_shutdown(30))
            await asyncio.sleep(0.05)
            threading.Thread(target=web_shutdown.begin_shutdown).start()
            return await asyncio.wait_for(sleeper, 5)

        assert asyncio.run(_drive()) is False


class TestTheNoticePathTakesNoLock:
    """A signal handler runs on the main thread between bytecodes of whatever
    that thread was doing — and under both deployment shapes the main thread is
    the event loop the streams run on. So a `begin_shutdown` holding a lock the
    interrupted code also holds would block forever, on a handler that has not
    yet called uvicorn's own `handle_exit`: the stop signal is swallowed and
    only SIGKILL ends the process, which is the failure this module removes.
    """

    def test_the_module_holds_no_lock_a_handler_could_block_on(self):
        import threading as _threading

        locks = [
            name for name, value in vars(web_shutdown).items()
            if isinstance(value, (_threading.Lock().__class__, _threading.RLock().__class__))
        ]
        assert locks == [], (
            f"{locks} is reachable from begin_shutdown, which runs in a signal "
            "handler on the thread that holds it"
        )

    def test_a_real_signal_on_the_loop_thread_wakes_a_sleeper(self):
        """The delivery shape the deployment actually has: the handler runs on
        the loop's own thread, in the middle of the loop's own work."""
        signal.signal(signal.SIGTERM, lambda s, f: None)
        web_shutdown.install_signal_hook()

        async def _drive():
            sleeper = asyncio.create_task(web_shutdown.sleep_unless_shutdown(30))
            await asyncio.sleep(0.05)  # let it register
            signal.raise_signal(signal.SIGTERM)
            return await asyncio.wait_for(sleeper, 5)

        assert asyncio.run(_drive()) is False


class TestIsShuttingDown:
    def test_it_starts_false_and_latches(self):
        assert web_shutdown.is_shutting_down() is False
        web_shutdown.begin_shutdown()
        assert web_shutdown.is_shutting_down() is True
        # Idempotent: uvicorn's own handler is called for both Ctrl-Cs.
        web_shutdown.begin_shutdown()
        assert web_shutdown.is_shutting_down() is True


class TestInstallSignalHook:
    def test_it_wraps_the_installed_handler_and_delegates(self):
        calls = []
        signal.signal(signal.SIGTERM, lambda s, f: calls.append(s))

        assert web_shutdown.install_signal_hook() is True
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        assert web_shutdown.is_shutting_down() is True
        assert calls == [signal.SIGTERM], "uvicorn's own handler must still run"

    def test_it_does_not_wrap_a_default_disposition(self):
        """Replacing SIG_DFL with a wrapper that only takes a note would break
        Ctrl-C rather than improve it — the default is what ends the process."""
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        web_shutdown.install_signal_hook()
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL

    def test_installing_twice_does_not_stack_wrappers(self):
        signal.signal(signal.SIGTERM, lambda s, f: None)
        web_shutdown.install_signal_hook()
        first = signal.getsignal(signal.SIGTERM)
        web_shutdown.install_signal_hook()
        assert signal.getsignal(signal.SIGTERM) is first

    def test_a_failing_notice_still_delivers_the_signal(self, monkeypatch):
        """The wrapped handler is what actually stops the server. Nothing this
        module does on top of it may swallow the stop signal."""
        calls = []
        signal.signal(signal.SIGTERM, lambda s, f: calls.append(s))
        web_shutdown.install_signal_hook()
        handler = signal.getsignal(signal.SIGTERM)

        def boom():
            raise RuntimeError("notice failed")

        monkeypatch.setattr(web_shutdown, "begin_shutdown", boom)
        handler(signal.SIGTERM, None)

        assert calls == [signal.SIGTERM]

    def test_starting_a_server_clears_a_previous_run_s_latch(self):
        """uvicorn restores the pre-server handlers when it returns, so the hook
        removes itself — the latch does not. A second server in one process
        would otherwise answer every stream with a response that ends at its
        first loop check, silently and for good."""
        signal.signal(signal.SIGTERM, lambda s, f: None)
        web_shutdown.begin_shutdown()
        assert web_shutdown.is_shutting_down() is True

        web_shutdown.install_signal_hook()

        assert web_shutdown.is_shutting_down() is False

    def test_a_worker_thread_gets_nothing_and_does_not_raise(self):
        """`signal.signal` is main-thread-only. This is a notice, not a
        boundary, so a caller off the main thread is told no rather than
        stopped."""
        result: list[object] = []
        before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}

        def _run():
            try:
                result.append(web_shutdown.install_signal_hook())
            except BaseException as exc:  # noqa: BLE001 - the point of the test
                result.append(exc)

        t = threading.Thread(target=_run)
        t.start()
        t.join(timeout=5)

        assert result == [False]
        assert {s: signal.getsignal(s) for s in before} == before


class TestLifespanWiring:
    """The hook is installed from the web app's lifespan, which is the one
    startup path `istota serve` and a plain `uvicorn istota.web_app:app` share.
    Wired anywhere else it would cover one of them and not the other."""

    def test_the_lifespan_installs_the_hook(self, monkeypatch):
        pytest.importorskip("fastapi")
        import istota.web_app as mod

        calls: list[int] = []
        monkeypatch.setattr(mod, "_reload_config", lambda: None)
        monkeypatch.setattr(mod, "_publish_config", lambda app: None)
        monkeypatch.setattr(signal, "signal", lambda *a, **k: None)
        monkeypatch.setattr(
            mod.web_shutdown, "install_signal_hook", lambda: calls.append(1) or True,
        )

        async def _run():
            async with mod.lifespan(object()):
                pass

        asyncio.run(_run())
        assert calls == [1]
