"""Whether this web process is stopping, in one place the SSE generators can see.

The web app's long-lived streams (``/chat/stream``, the task stream, the admin
log tail) poll until the *client* goes away, so nothing server-side ever ends
them. uvicorn's shutdown therefore always runs out its graceful window and then
cancels the ASGI task, which surfaces as an ``ERROR: Exception in ASGI
application`` with a ``CancelledError`` traceback on every Ctrl-C and every
deploy restart — a stack trace for the most ordinary event there is.

uvicorn offers the generator nothing to observe: ``connection.shutdown()`` on a
connection mid-response only clears keep-alive, so ``request.is_disconnected()``
stays False, and the lifespan shutdown event fires *after* the connection wait,
too late to be the signal. So the signal is made here instead. The handler
uvicorn installed for SIGINT/SIGTERM is wrapped once at startup
(:func:`install_signal_hook`, called from the web app's lifespan, which is the
one place that covers both ``istota serve`` and a plain
``uvicorn istota.web_app:app``), and a stream that sleeps through
:func:`sleep_unless_shutdown` wakes immediately and returns. Its response then
completes normally, so there is nothing left for uvicorn to cancel.

**Nothing on the notice path may take a lock**, and that is the constraint the
whole module is shaped around. A Python signal handler runs on the main thread
between bytecodes of whatever that thread was doing — and under both deployment
shapes the main thread is the event loop, which is where the streams run. So a
``begin_shutdown`` that acquired a lock could be entered *while the interrupted
code held it*, on a non-reentrant lock, from a handler that has not yet called
uvicorn's own ``handle_exit``: the process would then hang with the stop signal
swallowed, unkillable except by SIGKILL, which is the exact failure this module
exists to remove. So the state is a plain flag plus a plain list, published and
read in an order that closes the register/signal race without one (see
:func:`sleep_unless_shutdown`), and ``loop.call_soon_threadsafe`` — documented
by asyncio as the one loop method safe to call from a signal handler — does the
waking.

This is a notice, not a boundary: a stream that ignores it is still cancelled at
the graceful timeout exactly as before, which is why nothing here raises and why
a failure to install the hook is a logged line rather than a refusal to serve.
It is also **signal-driven**, so a shutdown started any other way — `serve.py`'s
supervisor tripping on a dead scheduler thread, an in-process ``should_exit`` —
has to say so itself by calling :func:`begin_shutdown`, or it runs the graceful
window out as before.

stdlib-only leaf — imported from the web app's request path, so it pulls in
nothing of the package.
"""

from __future__ import annotations

import asyncio
import logging
import signal

logger = logging.getLogger("istota.web_shutdown")

_shutting_down = False
# One entry per stream currently sleeping. Each carries its own loop, so the
# wake works from a signal handler on the main thread whatever loop the stream
# is on, and so a test can drive this with no loop running at all. A plain list
# mutated with `append` / `remove` and copied with `list()`, each of which is a
# single atomic operation under the GIL — see the module docstring for why it
# cannot be a lock instead.
_waiters: list[tuple[asyncio.Event, asyncio.AbstractEventLoop]] = []


def is_shutting_down() -> bool:
    """True once a stop signal has been seen by this process."""
    return _shutting_down


def begin_shutdown() -> None:
    """Record that the process is stopping and wake every sleeping stream.

    Safe to call from a signal handler, from any thread, and more than once —
    and safe there specifically because it takes no lock (module docstring).
    The flag is published *before* the waiter list is copied, which is the half
    of the race this end owns: a waiter registered after the copy is taken will
    see the flag on its own re-check and never sleep at all.

    The wake is deferred onto each waiter's own loop rather than done here:
    ``Event.set`` schedules callbacks on the loop, and this may be running
    between two bytecodes of that loop's own thread.
    """
    global _shutting_down
    _shutting_down = True
    for event, loop in list(_waiters):
        try:
            loop.call_soon_threadsafe(event.set)
        except Exception:  # noqa: BLE001 - a closed loop is a stream already gone
            continue


async def sleep_unless_shutdown(seconds: float) -> bool:
    """Sleep for ``seconds``; return False as soon as shutdown begins.

    A drop-in for ``asyncio.sleep`` in a polling stream: ``if not await
    sleep_unless_shutdown(poll): return``. True means the full interval elapsed
    with the process still running.
    """
    if _shutting_down:
        return False
    entry = (asyncio.Event(), asyncio.get_running_loop())
    _waiters.append(entry)
    try:
        # Re-checked after the append, which is this end's half of the race:
        # a `begin_shutdown` that published the flag and copied the list before
        # the append would wake every waiter but this one, and this stream would
        # then sleep out its whole interval.
        if _shutting_down:
            return False
        await asyncio.wait_for(entry[0].wait(), seconds)
        return False
    except (asyncio.TimeoutError, TimeoutError):
        return True
    finally:
        try:
            _waiters.remove(entry)
        except ValueError:  # pragma: no cover - defensive
            pass


def install_signal_hook() -> bool:
    """Wrap this process's stop signals so the streams learn about a shutdown.

    Called from the web app's lifespan, which runs *after* uvicorn has installed
    its own ``handle_exit`` for SIGINT/SIGTERM (``capture_signals`` wraps
    ``startup()``), so the handler found here is uvicorn's and it is delegated
    to unchanged. Returns True if anything was wrapped.

    **Starting a server is also the reset point.** uvicorn restores the
    pre-server handlers when it returns, so the hook is removed on its own, but
    the latch is not — and a second server in the same process would then serve
    every stream a response that ends at its first loop check, silently and for
    the life of the process. Nothing does that today; it costs two lines to make
    it impossible.

    A handler that is not callable (``SIG_DFL``/``SIG_IGN``) is left alone: the
    default disposition for SIGINT ends the process outright, and replacing it
    with a wrapper that merely takes a note would break Ctrl-C rather than
    improve it. Signals can only be installed from the main thread, so a caller
    on a worker thread gets False and a debug line rather than an exception —
    this is a notice, not a boundary.
    """
    global _shutting_down
    _shutting_down = False
    _waiters.clear()

    installed = False
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            current = signal.getsignal(sig)
        except (ValueError, OSError):  # pragma: no cover - defensive
            continue
        if not callable(current) or getattr(current, "_istota_shutdown_hook", False):
            continue

        def handler(signum, frame, _original=current):
            # Guarded so that nothing in this module can eat a stop signal: the
            # delegation below is what actually stops the server, and this is a
            # notice on top of it.
            try:
                begin_shutdown()
            except Exception:  # noqa: BLE001 - never raise out of a signal handler
                pass
            _original(signum, frame)

        handler._istota_shutdown_hook = True
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError) as exc:
            logger.debug("Could not wrap %s for stream shutdown: %s", sig, exc)
            continue
        installed = True
    return installed


def reset_for_tests() -> None:
    """Clear the module state. Tests only — the app resets on a server start."""
    global _shutting_down
    _shutting_down = False
    _waiters.clear()
