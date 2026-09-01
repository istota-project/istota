"""``istota serve`` must stop on Ctrl-C while an SSE stream is open.

The web app serves SSE (``/chat/stream``, the task stream, the admin log tail)
whose generators poll until the *client* disconnects. Two things then bite, and
neither is visible to a test that stubs uvicorn out:

* uvicorn's graceful shutdown waits for every open connection, unbounded by
  default — so one browser tab on the chat wedges the first Ctrl-C forever.
* On Python 3.12+ a repeat Ctrl-C does not rescue it either. ``force_exit``
  breaks uvicorn's own wait loops and it then blocks in
  ``asyncio.Server.wait_closed()``, which since 3.12 waits for the same
  connections. Only SIGKILL ended the process.

Both of those are backstops, and both are loud: running the graceful window out
ends in uvicorn cancelling the ASGI task, and uvicorn logs any exception out of
an ASGI app — ``CancelledError`` included — as ``ERROR: Exception in ASGI
application`` with a full traceback. So the ordinary path is the third case
here: the stream sees the stop signal (``istota.web_shutdown``) and ends itself,
leaving the shutdown nothing to wait on and nothing to cancel.

So these run a real uvicorn in a subprocess with a real SSE client attached and
signal it the way a person does. Each case is given a graceful window that the
*other* mechanisms could not satisfy, so none passes on another's work.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("uvicorn")
pytest.importorskip("starlette")

_CHILD = Path(__file__).parent / "support" / "serve_sse_child.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_exit(proc: subprocess.Popen, timeout: float) -> int | None:
    """Return the exit status, or ``None`` if it is still running at ``timeout``."""
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


@contextlib.contextmanager
def _serving(graceful: int, aware: bool = False):
    port = _free_port()
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [sys.executable, str(_CHILD), str(port), str(graceful), "1" if aware else "0"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
    )
    sse: socket.socket | None = None
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
                raise AssertionError(f"child exited before serving:\n{out}")
            try:
                sse = socket.create_connection(("127.0.0.1", port), timeout=5)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise AssertionError("child never accepted a connection")
                time.sleep(0.1)

        sse.sendall(
            b"GET /stream HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n"
        )
        # Read far enough to know the stream is open and uvicorn is holding the
        # connection — the precondition both cases depend on.
        assert b"200" in sse.recv(4096)
        yield proc
    finally:
        if sse is not None:
            sse.close()
        if proc.poll() is None:  # pragma: no cover - only on a failure
            proc.kill()
            proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()


def test_one_interrupt_stops_within_the_graceful_window():
    """A single Ctrl-C must not wait on a stream that never ends."""
    graceful = 2
    with _serving(graceful) as proc:
        proc.send_signal(signal.SIGINT)
        status = _wait_exit(proc, graceful + 15)
    assert status is not None, (
        "one SIGINT left the process running with an SSE stream open — "
        "the graceful shutdown wait is unbounded"
    )


def test_repeat_interrupt_forces_the_quit():
    """The second Ctrl-C must not wait out the graceful window.

    The window here is far longer than the assertion's deadline, so this can
    only pass by force-quitting: without it the process sits in
    ``wait_closed()`` until SIGKILL.
    """
    graceful = 300
    with _serving(graceful) as proc:
        proc.send_signal(signal.SIGINT)
        # The first signal has to land before the second, or uvicorn reads them
        # as one and never sets force_exit.
        assert _wait_exit(proc, 1.5) is None
        proc.send_signal(signal.SIGINT)
        status = _wait_exit(proc, 20)
    assert status is not None, (
        "a repeat SIGINT did not quit — force_exit was set and the process "
        "still blocked in asyncio.Server.wait_closed()"
    )


def test_a_shutdown_aware_stream_ends_itself_and_logs_no_traceback():
    """The ordinary path: nothing is left for the shutdown to wait on.

    Both mechanisms above are backstops, and both are loud — running the
    graceful window out ends with uvicorn cancelling the ASGI task, and uvicorn
    logs *any* exception out of an ASGI app, `CancelledError` included, as
    ``ERROR: Exception in ASGI application`` with a full traceback. A person
    pressing Ctrl-C got that every time.

    The window here is far longer than the deadline, so this can only pass by
    the stream ending itself on the signal.
    """
    graceful = 300
    with _serving(graceful, aware=True) as proc:
        proc.send_signal(signal.SIGINT)
        # Asserted before the output is read, and inside the `with`: reading to
        # EOF on a process that has not exited blocks until it does, which on
        # this window is five minutes. A failure here has to fail now.
        assert _wait_exit(proc, 20) is not None, (
            "a shutdown-aware stream did not end on SIGINT — the process sat "
            "in the graceful window"
        )
        out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""

    assert "Exception in ASGI application" not in out, (
        f"the stream was cancelled rather than ending itself:\n{out}"
    )
    assert "timeout graceful shutdown exceeded" not in out, (
        f"the graceful window ran out with a stream still open:\n{out}"
    )
