"""Tests for the persistent asyncio runtime (Stage 1).

The runtime hosts one long-lived event loop on a dedicated daemon thread and
bridges sync call sites to it via ``submit`` / ``run_coro``. See
``Specs/Active/scheduler-persistent-asyncio-loop.md``.
"""

import asyncio
import concurrent.futures
import threading
import time

import pytest

from istota.async_runtime import (
    AsyncRuntime,
    get_async_runtime,
    reset_async_runtime,
    run_coro,
)


@pytest.fixture
def runtime():
    """A fresh, isolated runtime per test; torn down on exit."""
    rt = AsyncRuntime()
    rt.start()
    try:
        yield rt
    finally:
        rt.stop()


class TestRuntimeLifecycle:
    def test_runtime_start_stop(self):
        rt = AsyncRuntime()
        assert rt.is_running is False
        rt.start()
        assert rt.is_running is True
        rt.stop()
        assert rt.is_running is False

    def test_start_idempotent(self, runtime):
        loop_before = runtime.loop
        runtime.start()  # second start is a no-op
        assert runtime.loop is loop_before
        assert runtime.is_running is True

    def test_stop_idempotent(self):
        rt = AsyncRuntime()
        rt.start()
        rt.stop()
        rt.stop()  # must not raise
        assert rt.is_running is False

    def test_loop_property_before_start_raises(self):
        rt = AsyncRuntime()
        with pytest.raises(RuntimeError):
            _ = rt.loop


class TestSubmit:
    def test_submit_returns_coroutine_result(self, runtime):
        async def f():
            return 42

        assert runtime.submit(f()) == 42

    def test_submit_propagates_exception(self, runtime):
        async def boom():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            runtime.submit(boom())

    def test_submit_timeout(self, runtime):
        cancelled = threading.Event()

        async def slow():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        start = time.monotonic()
        with pytest.raises((TimeoutError, concurrent.futures.TimeoutError)):
            runtime.submit(slow(), timeout=0.5)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, "timeout should fire promptly"
        # The coroutine must actually be cancelled, not orphaned.
        assert cancelled.wait(timeout=2.0), "coroutine was not cancelled on timeout"

    def test_submit_before_start_raises(self):
        rt = AsyncRuntime()

        async def f():
            return 1

        with pytest.raises(RuntimeError):
            rt.submit(f())

    def test_submit_from_within_loop_raises(self, runtime):
        captured: dict = {}

        async def reenter():
            async def inner():
                return 1

            try:
                runtime.submit(inner())
            except Exception as exc:  # noqa: BLE001
                captured["exc"] = exc
            else:
                captured["exc"] = None

        runtime.submit(reenter())
        assert isinstance(captured["exc"], RuntimeError)
        assert "within the persistent loop" in str(captured["exc"])

    def test_concurrent_submits(self, runtime):
        async def square(n):
            await asyncio.sleep(0.01)
            return n * n

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(runtime.submit, square(i)) for i in range(50)]
            results = sorted(f.result(timeout=10) for f in futures)

        assert results == sorted(i * i for i in range(50))


class TestCleanupHooks:
    def test_cleanup_hook_called_on_stop(self):
        rt = AsyncRuntime()
        rt.start()
        sentinel = threading.Event()

        async def cleanup():
            sentinel.set()

        rt.add_cleanup_hook(cleanup)
        rt.stop()
        assert sentinel.is_set()

    def test_cleanup_hook_failure_does_not_block_stop(self):
        rt = AsyncRuntime()
        rt.start()

        async def bad_cleanup():
            raise RuntimeError("cleanup boom")

        rt.add_cleanup_hook(bad_cleanup)
        rt.stop()  # must not raise
        assert rt.is_running is False


class TestShutdownOrdering:
    def test_inflight_cancelled_before_cleanup_hooks(self):
        """stop() must cancel in-flight coroutines BEFORE running cleanup hooks.

        Otherwise a hook like TalkClient.aclose closes the shared client out from
        under a live request (e.g. the poller's long-poll), surfacing a spurious
        "client closed" error instead of a clean CancelledError.
        """
        rt = AsyncRuntime()
        rt.start()
        order: list[str] = []
        started = threading.Event()

        async def inflight():
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                order.append("cancelled")
                raise

        async def hook():
            order.append("hook")

        # Schedule the long-running coroutine on the loop without blocking this
        # thread, so it is a genuine pending task when stop() runs.
        asyncio.run_coroutine_threadsafe(inflight(), rt.loop)
        assert started.wait(timeout=2.0), "in-flight coroutine never started"
        rt.add_cleanup_hook(hook)

        rt.stop()

        assert order == ["cancelled", "hook"]

    def test_start_clears_stale_cleanup_hooks(self):
        """A restart of the same instance must not accumulate hooks from the
        prior run (get_talk_client appends one aclose hook per client)."""
        rt = AsyncRuntime()
        calls: list[str] = []

        async def hook():
            calls.append("x")

        rt.start()
        rt.add_cleanup_hook(hook)
        rt.stop()
        assert calls == ["x"]

        # Restart + stop: the stale hook from the first cycle must be gone.
        rt.start()
        rt.stop()
        assert calls == ["x"], "stale cleanup hook ran again after restart"


class TestModuleSingleton:
    def teardown_method(self):
        reset_async_runtime()

    def test_run_coro_lazy_starts_singleton(self):
        async def f():
            return "ok"

        assert run_coro(f()) == "ok"
        assert get_async_runtime().is_running is True

    def test_get_async_runtime_returns_same_instance(self):
        a = get_async_runtime()
        b = get_async_runtime()
        assert a is b

    def test_reset_stops_and_clears_singleton(self):
        a = get_async_runtime()
        assert a.is_running is True
        reset_async_runtime()
        assert a.is_running is False
        b = get_async_runtime()
        assert b is not a
