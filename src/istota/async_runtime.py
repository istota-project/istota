"""Persistent asyncio runtime for the scheduler daemon.

One long-lived event loop runs on a dedicated daemon thread for the scheduler's
lifetime. Sync call sites submit coroutines to it via ``run_coro`` / the
``AsyncRuntime.submit`` bridge instead of spinning up a fresh loop (and a fresh
``httpx.AsyncClient``) per call. This gives connection reuse to Nextcloud and
eliminates the loop-teardown leak surface of per-call ``asyncio.run``.

See ``Specs/Active/scheduler-persistent-asyncio-loop.md`` for the full design.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_REENTRY_MESSAGE = (
    "run_coro called from within the persistent loop — use await directly"
)


def _cancel_pending_tasks(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel and drain any tasks still pending when the loop stops."""
    tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))


class AsyncRuntime:
    """Long-lived asyncio loop running on a dedicated daemon thread.

    One instance per scheduler process. Thread-safe submit + result via
    ``asyncio.run_coroutine_threadsafe``; ``stop()`` runs registered cleanup
    hooks (e.g. closing the shared ``httpx.AsyncClient``), then stops the loop.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._thread_ident: int | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._cleanup_hooks: list[Callable[[], Awaitable[None]]] = []
        self._stopped = False

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The running event loop. Raises if the runtime hasn't started."""
        if self._loop is None:
            raise RuntimeError("AsyncRuntime not started")
        return self._loop

    @property
    def is_running(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and self._loop is not None
            and self._loop.is_running()
        )

    def start(self) -> None:
        """Spawn the loop thread and block until the loop is ready. Idempotent."""
        with self._lock:
            if self.is_running:
                return
            self._ready.clear()
            self._stopped = False
            # A restart of the same instance must not carry hooks from the prior
            # run — get_talk_client appends a fresh aclose hook per client, so
            # without this they'd accumulate across stop()/start() cycles.
            self._cleanup_hooks = []
            self._thread = threading.Thread(
                target=self._run, name="async-runtime", daemon=True
            )
            self._thread.start()
        if not self._ready.wait(timeout=10.0):
            raise RuntimeError("AsyncRuntime loop failed to start within 10s")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._thread_ident = threading.get_ident()
        asyncio.set_event_loop(loop)
        loop.call_soon(self._ready.set)
        try:
            loop.run_forever()
        finally:
            try:
                _cancel_pending_tasks(loop)
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    def add_cleanup_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Register a zero-arg async callable to run on the loop during stop()."""
        self._cleanup_hooks.append(hook)

    def submit(self, coro: Awaitable[T], *, timeout: float | None = None) -> T:
        """Run ``coro`` on the persistent loop and block for its result.

        ``timeout`` defaults to ``None`` (wait forever), matching ``asyncio.run``
        semantics — do not change this to a "sensible default" or long-poll
        callers will break. On timeout the scheduled coroutine is cancelled and
        ``TimeoutError`` is raised; by the time the caller observes it the
        coroutine has been *requested* to cancel but may not have finished
        cleanup (same guarantee as ``asyncio.wait_for``).
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            if asyncio.iscoroutine(coro):
                coro.close()
            raise RuntimeError("AsyncRuntime not started")
        if threading.get_ident() == self._thread_ident:
            # run_coroutine_threadsafe from the loop's own thread deadlocks.
            # Close the coroutine to avoid a "never awaited" warning, fail loud.
            if asyncio.iscoroutine(coro):
                coro.close()
            raise RuntimeError(_REENTRY_MESSAGE)
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError(
                f"coroutine did not complete within {timeout}s"
            ) from exc

    async def _shutdown(
        self, hooks: list[Callable[[], Awaitable[None]]]
    ) -> None:
        """Cancel in-flight coroutines, then run cleanup hooks. Runs on the loop.

        Ordering matters: in-flight coroutines (e.g. an active long-poll awaiting
        ``self._client.get(...)``) are cancelled *first* so a cleanup hook like
        ``TalkClient.aclose`` doesn't close the shared client out from under a
        live request. Doing it the other way round surfaces as a spurious
        "client closed" error on the awaited request instead of a clean
        ``CancelledError``.
        """
        current = asyncio.current_task()
        pending = [
            t for t in asyncio.all_tasks() if t is not current and not t.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for hook in hooks:
            try:
                await hook()
            except Exception as exc:  # noqa: BLE001 — never let a hook block stop
                logger.warning("AsyncRuntime cleanup hook failed: %s", exc)

    def stop(self, timeout: float = 10.0) -> None:
        """Cancel in-flight work, run cleanup hooks, stop the loop, join. Idempotent.

        If shutdown doesn't finish within ``timeout`` (a coroutine hung on a
        network op or swallowing cancellation), log a warning and return —
        daemon shutdown is not blocked.
        """
        with self._lock:
            if self._stopped:
                return
            loop = self._loop
            thread = self._thread
            self._stopped = True
        if loop is None or thread is None or not thread.is_alive():
            return

        hooks = list(self._cleanup_hooks)
        shutdown_budget = timeout / 2.0
        try:
            fut = asyncio.run_coroutine_threadsafe(self._shutdown(hooks), loop)
            fut.result(timeout=shutdown_budget)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            logger.warning(
                "AsyncRuntime shutdown did not finish within %.1fs", shutdown_budget
            )
        except Exception as exc:  # noqa: BLE001 — never let shutdown block stop
            logger.warning("AsyncRuntime shutdown failed: %s", exc)

        loop.call_soon_threadsafe(loop.stop)
        join_budget = timeout - shutdown_budget
        thread.join(timeout=join_budget)
        if thread.is_alive():
            logger.warning(
                "AsyncRuntime loop thread did not stop within %.1fs", timeout
            )


_RUNTIME: AsyncRuntime | None = None
_RUNTIME_LOCK = threading.Lock()


def get_async_runtime() -> AsyncRuntime:
    """Return the process-global runtime, lazily starting it on first use.

    Lazy start is a convenience for tests and CLI ergonomics; the long-running
    daemon still calls ``start``/``stop`` explicitly in ``run_daemon``.
    """
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = AsyncRuntime()
        if not _RUNTIME.is_running:
            _RUNTIME.start()
        return _RUNTIME


def run_coro(coro: Awaitable[T], *, timeout: float | None = None) -> T:
    """Submit ``coro`` to the process-global persistent loop and block for it."""
    return get_async_runtime().submit(coro, timeout=timeout)


def reset_async_runtime() -> None:
    """Stop and discard the process-global runtime. Test-teardown helper.

    Stopping runs the runtime's cleanup hooks, which closes the persistent
    TalkClient; the talk-client singleton reference is then cleared so a
    subsequent get_talk_client() rebuilds it on the next (fresh) runtime.
    """
    global _RUNTIME
    with _RUNTIME_LOCK:
        rt = _RUNTIME
        _RUNTIME = None
    if rt is not None:
        rt.stop()
    reset_talk_client()


# --- Persistent TalkClient singleton --------------------------------------
#
# Lives here (not in talk.py) so its lifecycle is centralized with the runtime
# it is bound to. TalkClient is imported lazily inside the helpers to keep this
# module transport-agnostic and free of import cycles.

_TALK_CLIENT = None  # type: ignore[var-annotated]
_TALK_CLIENT_LOCK = threading.Lock()


def get_talk_client(config):
    """Return the process-global persistent TalkClient, bound to the runtime loop.

    All Talk delivery paths (the transport seam, the event consumers,
    notifications) pull from this singleton so they share one connection pool
    to Nextcloud.

    This is a *synchronous* accessor and must stay reentry-safe: it is called
    from inside Talk coroutines that themselves run on the persistent loop
    (e.g. ``TalkTransport.deliver`` invoked via ``run_coro``), so it must not
    call ``run_coro`` itself (that would trip the within-the-loop guard). The
    underlying ``httpx.AsyncClient`` is therefore opened lazily by the first
    awaited method call (``TalkClient._ensure_open``) — which runs on the
    persistent loop because every Talk call site goes through ``run_coro`` —
    not eagerly here. ``get_async_runtime()`` ensures the runtime is started so
    the registered ``aclose`` cleanup hook will fire on ``stop()``.
    """
    global _TALK_CLIENT
    with _TALK_CLIENT_LOCK:
        if _TALK_CLIENT is not None and not _TALK_CLIENT.is_closed:
            return _TALK_CLIENT
        from .talk import TalkClient

        client = TalkClient(config)
        get_async_runtime().add_cleanup_hook(client.aclose)
        _TALK_CLIENT = client
        return _TALK_CLIENT


def reset_talk_client() -> None:
    """Drop the talk-client singleton reference. Test-teardown helper.

    Does not itself close the client — closing happens via the runtime cleanup
    hook on stop(). This only clears the cached reference.
    """
    global _TALK_CLIENT
    with _TALK_CLIENT_LOCK:
        _TALK_CLIENT = None
