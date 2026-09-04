"""Tests for the persistent asyncio runtime (Stage 1).

The runtime hosts one long-lived event loop on a dedicated daemon thread and
bridges sync call sites to it via ``submit`` / ``run_coro``. See
``Specs/Active/scheduler-persistent-asyncio-loop.md``.
"""

import asyncio
import concurrent.futures
import logging
import threading
import time

import pytest

from istota.async_runtime import (
    AsyncRuntime,
    get_async_runtime,
    reset_async_runtime,
    run_coro,
    spawn_task,
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


class TestSpawn:
    """Fire-and-forget scheduling for long-lived coroutines.

    ``submit``/``run_coro`` block the submitting thread for a result, which is
    the right shape for a request and the wrong one for a watcher that runs
    for the life of the daemon. ``spawn`` schedules and returns, handing back a
    handle the caller holds for cancellation.

    The shutdown ordering is what these assertions are really about.
    ``_shutdown`` cancels in-flight coroutines *before* running the cleanup
    hooks, so the shared ``TalkClient`` is not closed under a live request. A
    spawned task that outlived cancellation would put that back.
    """

    def test_spawn_does_not_block_the_caller(self, runtime):
        started = threading.Event()
        release = threading.Event()

        async def forever():
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)

        t0 = time.monotonic()
        handle = runtime.spawn(forever(), name="forever")
        elapsed = time.monotonic() - t0

        try:
            assert elapsed < 1.0, "spawn blocked the caller"
            assert started.wait(timeout=2.0), "the coroutine never started"
            assert handle.done() is False
        finally:
            release.set()
            handle.cancel()

    def test_the_handle_cancels_the_task(self, runtime):
        started = threading.Event()
        cancelled = threading.Event()

        async def forever():
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        handle = runtime.spawn(forever(), name="forever")
        assert started.wait(timeout=2.0)

        handle.cancel()

        assert cancelled.wait(timeout=2.0), "cancelling the handle did not reach the task"

    def test_a_spawned_task_is_cancelled_by_stop(self):
        rt = AsyncRuntime()
        rt.start()
        started = threading.Event()
        cancelled = threading.Event()

        async def forever():
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        rt.spawn(forever(), name="forever")
        assert started.wait(timeout=2.0)

        rt.stop()

        assert cancelled.is_set(), (
            "stop() left a spawned task running; the cleanup hooks would then "
            "close the shared TalkClient under it"
        )

    def test_a_spawned_task_is_cancelled_before_the_cleanup_hooks_run(self):
        """The ordering ``_shutdown``'s docstring exists to protect."""
        rt = AsyncRuntime()
        rt.start()
        order = []
        started = threading.Event()

        async def forever():
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                order.append("task-cancelled")
                raise

        async def hook():
            order.append("hook")

        rt.spawn(forever(), name="forever")
        rt.add_cleanup_hook(hook)
        assert started.wait(timeout=2.0)

        rt.stop()

        assert order == ["task-cancelled", "hook"], order

    def test_a_spawned_task_that_raises_is_reported(self, runtime, caplog):
        done = threading.Event()

        async def boom():
            raise ValueError("watcher blew up")

        with caplog.at_level(logging.ERROR, logger="istota.async_runtime"):
            handle = runtime.spawn(boom(), name="boom")
            handle.add_done_callback(lambda _f: done.set())
            assert done.wait(timeout=2.0)
            # The internal callback runs on the same future; give it a beat.
            time.sleep(0.05)

        assert any("boom" in r.getMessage() for r in caplog.records), (
            "a spawned task died with no log line naming it"
        )

    def test_an_ordinary_completion_is_not_reported_as_a_failure(self, runtime, caplog):
        done = threading.Event()

        async def quiet():
            return 7

        with caplog.at_level(logging.WARNING, logger="istota.async_runtime"):
            handle = runtime.spawn(quiet(), name="quiet")
            handle.add_done_callback(lambda _f: done.set())
            assert done.wait(timeout=2.0)
            time.sleep(0.05)

        assert handle.result(timeout=1.0) == 7
        assert not caplog.records, [r.getMessage() for r in caplog.records]

    def test_cancellation_is_not_reported_as_a_failure(self, runtime, caplog):
        started = threading.Event()

        async def forever():
            started.set()
            await asyncio.sleep(30)

        with caplog.at_level(logging.WARNING, logger="istota.async_runtime"):
            handle = runtime.spawn(forever(), name="forever")
            assert started.wait(timeout=2.0)
            handle.cancel()
            time.sleep(0.1)

        assert not caplog.records, [r.getMessage() for r in caplog.records]

    def test_spawn_from_the_loop_thread_does_not_raise(self, runtime):
        """Unlike ``submit``, which deadlocks there and therefore refuses.

        ``spawn`` never blocks for a result, so scheduling from the loop's own
        thread is safe — and it is what the supervisor does when it starts a
        watcher from a coroutine already running on the loop.
        """
        inner_started = threading.Event()

        async def inner():
            inner_started.set()

        async def outer():
            runtime.spawn(inner(), name="inner")

        runtime.submit(outer())

        assert inner_started.wait(timeout=2.0)

    def test_spawn_before_start_raises_and_closes_the_coroutine(self):
        rt = AsyncRuntime()

        async def never():
            pass

        coro = never()
        with pytest.raises(RuntimeError):
            rt.spawn(coro, name="never")
        # Closed by spawn, so pytest reports no "never awaited" warning.
        assert coro.cr_frame is None

    def test_start_clears_handles_from_a_previous_run(self):
        rt = AsyncRuntime()
        rt.start()

        async def forever():
            await asyncio.sleep(30)

        rt.spawn(forever(), name="forever")
        assert rt.spawned_names() == ["forever"]
        rt.stop()

        rt.start()
        try:
            assert rt.spawned_names() == []
        finally:
            rt.stop()

    def test_a_finished_task_is_dropped_from_the_registry(self, runtime):
        done = threading.Event()

        async def quiet():
            return None

        handle = runtime.spawn(quiet(), name="quiet")
        handle.add_done_callback(lambda _f: done.set())
        assert done.wait(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while runtime.spawned_names() and time.monotonic() < deadline:
            time.sleep(0.01)

        assert runtime.spawned_names() == []


class TestModuleLevelSpawnTask:
    def teardown_method(self):
        reset_async_runtime()

    def test_it_schedules_on_the_process_global_runtime(self):
        started = threading.Event()

        async def work():
            started.set()

        handle = spawn_task(work(), name="work")
        try:
            assert started.wait(timeout=2.0)
        finally:
            handle.cancel()


class TestWhatTheHandleDoesAndDoesNotSay:
    """A cancelled handle is a delivered cancel, not a stopped task.

    ``run_coroutine_threadsafe``'s future is never put into ``RUNNING``, so
    ``cancel()`` resolves it out of ``PENDING`` and fires every done-callback
    inline while the task's own ``finally`` has not run. A supervisor that read
    its own done-callback as "the watcher has stopped" would restart one on top
    of a watcher still holding its socket and its Talk room session.
    """

    def test_the_handle_resolves_before_the_task_has_finished(self, runtime):
        started = threading.Event()
        finished = threading.Event()

        async def slow_cleanup():
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(0.3)
                finished.set()
                raise

        handle = runtime.spawn(slow_cleanup(), name="slow")
        assert started.wait(timeout=2.0)

        handle.cancel()

        assert handle.done() is True
        assert handle.cancelled() is True
        assert finished.is_set() is False, (
            "the task had already finished, so this test proves nothing about "
            "the gap the docstring warns about"
        )
        assert finished.wait(timeout=3.0)

    def test_the_registry_still_holds_a_task_that_is_winding_down(self, runtime):
        """What makes ``stop()``'s timeout report attributable.

        A registry keyed on handle resolution would empty at the ``cancel``
        above, so the one task worth naming is the one it forgets first.
        """
        started = threading.Event()
        release = threading.Event()

        async def slow_cleanup():
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                while not release.is_set():
                    await asyncio.sleep(0.01)
                raise

        handle = runtime.spawn(slow_cleanup(), name="winding-down")
        assert started.wait(timeout=2.0)
        handle.cancel()

        try:
            assert runtime.spawned_names() == ["winding-down"]
        finally:
            release.set()

        deadline = time.monotonic() + 2.0
        while runtime.spawned_names() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runtime.spawned_names() == []


class TestSpawnDuringShutdown:
    """A spawn racing ``stop()`` must not outlive the cleanup hooks.

    ``stop()`` publishes ``_stopped`` and then leaves the loop running for the
    whole of ``_shutdown``. A spawn accepted in that window creates its task
    after the ``all_tasks()`` sweep has already run, so it starts fresh once
    ``TalkClient.aclose`` has closed the client it is about to use — the exact
    ordering the shutdown path exists to prevent, reached from the other side.
    """

    def test_a_spawn_after_stop_is_refused(self):
        rt = AsyncRuntime()
        rt.start()
        rt.stop()

        async def work():
            pass

        coro = work()
        with pytest.raises(RuntimeError):
            rt.spawn(coro, name="late")
        assert coro.cr_frame is None

    def test_a_spawn_from_inside_a_cleanup_hook_is_refused(self):
        """The window that ``loop.is_running()`` alone does not close."""
        rt = AsyncRuntime()
        rt.start()
        seen = {}

        async def never_runs():
            seen["ran"] = True

        async def hook():
            coro = never_runs()
            try:
                rt.spawn(coro, name="during-shutdown")
                seen["accepted"] = True
            except RuntimeError as exc:
                seen["refused"] = str(exc)
                coro.close()

        rt.add_cleanup_hook(hook)
        rt.stop()

        assert "accepted" not in seen, (
            "a spawn was accepted while the cleanup hooks were running"
        )
        assert "stopping" in seen.get("refused", "")
        assert "ran" not in seen
