"""Tests for the shared Unix socket accept-loop lifecycle.

The two proxies that use it — ``network_proxy`` and ``skill_proxy`` — keep
their own handlers, allowlists and credential logic; this covers only bind,
listen, accept, the socketpair wake and teardown.
"""

import fcntl
import os
import selectors
import socket
import stat
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from istota.unix_server import (
    ACCEPT_RETRY_DELAY_S,
    MAX_ACCEPT_FAILURES,
    UnixSocketServer,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "istota"


@pytest.fixture
def sock_path():
    """/tmp keeps the path inside the ~104 char AF_UNIX limit on macOS."""
    path = Path(tempfile.gettempdir()) / f"istota-test-unixsrv-{os.getpid()}.sock"
    yield path
    path.unlink(missing_ok=True)


def _echo(conn: socket.socket) -> None:
    try:
        data = conn.recv(4096)
        conn.sendall(b"ok:" + data)
    finally:
        conn.close()


def _roundtrip(path: Path, payload: bytes = b"hi", timeout: float = 10.0) -> bytes:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
        sock.sendall(payload)
        return sock.recv(4096)
    finally:
        sock.close()


class TestLifecycle:
    def test_start_binds_and_stop_unlinks(self, sock_path):
        server = UnixSocketServer(sock_path, _echo, name="t", socket_mode=0o600)
        server.start()
        assert sock_path.exists()
        assert _roundtrip(sock_path) == b"ok:hi"
        server.stop()
        assert not sock_path.exists()

    def test_context_manager(self, sock_path):
        with UnixSocketServer(sock_path, _echo, name="t", socket_mode=0o600):
            assert sock_path.exists()
        assert not sock_path.exists()

    def test_stale_socket_file_is_replaced(self, sock_path):
        sock_path.write_text("stale")
        with UnixSocketServer(sock_path, _echo, name="t", socket_mode=0o600):
            assert _roundtrip(sock_path) == b"ok:hi"

    def test_stop_without_start_is_a_noop(self, sock_path):
        UnixSocketServer(sock_path, _echo, name="t", socket_mode=0o600).stop()
        assert not sock_path.exists()

    def test_double_stop_is_safe(self, sock_path):
        server = UnixSocketServer(sock_path, _echo, name="t", socket_mode=0o600)
        server.start()
        server.stop()
        server.stop()

    def test_restart_after_stop_serves_again(self, sock_path):
        """start() must clear the stop state, or the second run is silently deaf."""
        server = UnixSocketServer(sock_path, _echo, name="t", socket_mode=0o600)
        server.start()
        server.stop()
        server.start()
        try:
            assert _roundtrip(sock_path) == b"ok:hi"
        finally:
            server.stop()

    def test_serves_several_connections(self, sock_path):
        """One connection per readiness event; the loop must not go deaf."""
        with UnixSocketServer(sock_path, _echo, name="t", socket_mode=0o600):
            for i in range(3):
                assert _roundtrip(sock_path, str(i).encode()) == f"ok:{i}".encode()


class TestSocketMode:
    """The mode is the caller's decision, passed in, not baked into the loop."""

    def test_mode_is_applied(self, sock_path):
        with UnixSocketServer(sock_path, _echo, name="t", socket_mode=0o600):
            assert sock_path.stat().st_mode & 0o777 == 0o600

    def test_a_different_mode_is_applied(self, sock_path):
        with UnixSocketServer(sock_path, _echo, name="t", socket_mode=0o660):
            assert sock_path.stat().st_mode & 0o777 == 0o660

    def test_mode_is_required(self, sock_path):
        with pytest.raises(TypeError):
            UnixSocketServer(sock_path, _echo, name="t")

    def test_mode_is_applied_before_the_listen(self, sock_path):
        """A client must never reach a socket that is still 0o777-visible.

        bind() creates the path; listen() is what makes a connect() succeed
        rather than be refused. chmod between the two is what closes the
        window, so assert the order rather than only the end state.
        """
        calls = []
        real_chmod = os.chmod
        real_listen = socket.socket.listen

        def spy_chmod(path, mode, *a, **kw):
            if str(path) == str(sock_path):
                calls.append("chmod")
            return real_chmod(path, mode, *a, **kw)

        def spy_listen(self, *a, **kw):
            if self.family == socket.AF_UNIX:
                calls.append("listen")
            return real_listen(self, *a, **kw)

        with patch.object(os, "chmod", spy_chmod), \
                patch.object(socket.socket, "listen", spy_listen):
            with UnixSocketServer(sock_path, _echo, name="t", socket_mode=0o600):
                pass
        assert calls[:2] == ["chmod", "listen"], calls


class TestHandler:
    def test_handler_gets_a_blocking_socket(self, sock_path):
        """accept() on a non-blocking listener yields a non-blocking socket on
        BSD/macOS and a blocking one on Linux. The loop normalizes it.

        Asserted on ``O_NONBLOCK``, not on ``gettimeout()``. Measured on
        macOS: the accepted socket reports ``gettimeout() is None`` — Python
        thinks it is blocking — while the descriptor it wraps carries
        ``O_NONBLOCK``, so a handler's first ``recv`` raises
        ``BlockingIOError``. A timeout-based assertion passes with the
        normalization removed and proves nothing.
        """
        seen = {}

        def handler(conn):
            flags = fcntl.fcntl(conn.fileno(), fcntl.F_GETFL)
            seen["nonblock"] = bool(flags & os.O_NONBLOCK)
            seen["data"] = conn.recv(4096)
            conn.sendall(b"ok")
            conn.close()

        with UnixSocketServer(sock_path, handler, name="t", socket_mode=0o600):
            _roundtrip(sock_path)
        assert seen["nonblock"] is False, "handler got an O_NONBLOCK descriptor"
        assert seen["data"] == b"hi"

    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )
    def test_handler_exception_does_not_kill_the_loop(self, sock_path):
        seen = {"n": 0}

        def handler(conn):
            seen["n"] += 1
            conn.recv(4096)
            if seen["n"] == 1:
                conn.close()
                raise RuntimeError("boom")
            conn.sendall(b"ok:second")
            conn.close()

        with UnixSocketServer(sock_path, handler, name="t", socket_mode=0o600):
            try:
                _roundtrip(sock_path, timeout=2)
            except OSError:
                pass
            assert _roundtrip(sock_path) == b"ok:second"

    def test_handler_runs_off_the_accept_loop(self, sock_path):
        """Two connections must be in flight at once.

        The barrier is what makes this able to fail: a loop that ran handlers
        inline would never get a second one to the barrier, so the first times
        out and raises rather than the test passing on ordering.
        """
        barrier = threading.Barrier(2, timeout=5)

        def handler(conn):
            conn.recv(4096)
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                conn.sendall(b"alone")
            else:
                conn.sendall(b"ok:both")
            conn.close()

        with UnixSocketServer(sock_path, handler, name="t", socket_mode=0o600):
            first = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            first.settimeout(10)
            first.connect(str(sock_path))
            first.sendall(b"a")
            second = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            second.settimeout(10)
            second.connect(str(sock_path))
            second.sendall(b"b")
            try:
                assert first.recv(4096) == b"ok:both"
                assert second.recv(4096) == b"ok:both"
            finally:
                first.close()
                second.close()


class TestStopWake:
    def test_stop_returns_promptly(self, sock_path):
        """The accept loop is woken by the socketpair, not polled out.

        It used to sit in accept() on a 1s socket timeout, so every teardown
        paid a full second waiting for the loop to notice the stop event.
        """
        server = UnixSocketServer(sock_path, _echo, name="t", socket_mode=0o600)
        server.start()
        started = time.monotonic()
        server.stop()
        elapsed = time.monotonic() - started
        assert elapsed < 0.25, f"stop() took {elapsed:.2f}s"

    def test_stop_joins_the_accept_thread(self, sock_path):
        server = UnixSocketServer(sock_path, _echo, name="t", socket_mode=0o600)
        server.start()
        thread = server._thread
        assert thread is not None and thread.is_alive()
        server.stop()
        assert not thread.is_alive(), "accept loop still running after stop()"

    def test_a_stuck_loop_keeps_its_sockets_open(self, sock_path):
        """Never close a socket the accept loop may still be selecting on:
        epoll and kqueue drop a closed fd from the interest set silently, so
        the thread would block forever on numbers the OS may reassign.

        Checked through the raw descriptor rather than the socket object,
        because holding the object is itself what would keep it open — a test
        that keeps a reference cannot tell the two cases apart.
        """
        in_select = threading.Event()
        gate = threading.Event()
        real_select = selectors.DefaultSelector.select

        def wedged_select(self, timeout=None):
            in_select.set()
            gate.wait(20)
            return real_select(self, timeout)

        server = UnixSocketServer(sock_path, _echo, name="t", socket_mode=0o600)
        with patch.object(selectors.DefaultSelector, "select", wedged_select):
            server.start()
            thread = server._thread
            listener_fd = server._server_sock.fileno()
            assert in_select.wait(5), "loop never reached select()"

            with patch.object(
                threading.Thread, "join", lambda self, timeout=None: None,
            ):
                server.stop()
            assert thread.is_alive(), "loop was not wedged; the case is untested"
            assert server._server_sock is None

            try:
                info = os.fstat(listener_fd)
            except OSError as exc:
                pytest.fail(f"listener fd was closed under the loop: {exc}")
            assert stat.S_ISSOCK(info.st_mode)

            gate.set()
            thread.join(timeout=5)
            assert not thread.is_alive()

    def test_shutdown_wins_over_a_pending_connection(self, sock_path):
        """A connection that becomes ready in the same select() as the stop must
        not get a handler thread with a lifetime past stop()."""
        started = threading.Event()
        handled = threading.Event()
        gate = threading.Event()

        def handler(conn):
            handled.set()
            try:
                conn.close()
            except OSError:
                pass

        def slow_select(self, timeout=None):
            started.set()
            gate.wait(10)
            return _real_select(self, timeout)

        _real_select = selectors.DefaultSelector.select
        server = UnixSocketServer(sock_path, handler, name="t", socket_mode=0o600)
        with patch.object(selectors.DefaultSelector, "select", slow_select):
            server.start()
            assert started.wait(5)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(sock_path))
            stopper = threading.Thread(target=server.stop)
            stopper.start()
            time.sleep(0.05)
            gate.set()
            stopper.join(10)
            client.close()
        assert not handled.is_set(), "a handler was started after stop() was called"


class TestAcceptFailures:
    def test_transient_accept_failure_is_retried(self, sock_path):
        real_accept = socket.socket.accept
        calls = {"n": 0}

        def flaky_accept(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionAbortedError("simulated ECONNABORTED")
            return real_accept(self)

        with patch.object(socket.socket, "accept", flaky_accept):
            with UnixSocketServer(sock_path, _echo, name="t", socket_mode=0o600):
                assert _roundtrip(sock_path) == b"ok:hi"
        assert calls["n"] >= 2, "accept() was not retried after the failure"

    def test_a_listener_that_fails_forever_gives_up(self, sock_path):
        """A pending connection keeps the listener readable, so a listener that
        can never accept would spin at full speed without the ceiling."""
        calls = {"n": 0}

        def dead_accept(self):
            calls["n"] += 1
            raise OSError("simulated permanent failure")

        server = UnixSocketServer(sock_path, _echo, name="t", socket_mode=0o600)
        client = None
        with patch.object(socket.socket, "accept", dead_accept):
            server.start()
            thread = server._thread
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(sock_path))
            thread.join(
                timeout=(MAX_ACCEPT_FAILURES + 5) * ACCEPT_RETRY_DELAY_S + 5
            )
            assert not thread.is_alive(), "accept loop spun forever"
        client.close()
        assert calls["n"] == MAX_ACCEPT_FAILURES + 1
        server.stop()


class TestNoSecondCopy:
    """Pin: the accept-loop-with-socketpair-wake shape lives in one module."""

    def test_only_unix_server_carries_the_accept_loop(self):
        offenders = []
        for path in sorted(SRC.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "socketpair" in text and "DefaultSelector" in text:
                offenders.append(path.relative_to(SRC).as_posix())
        assert offenders == ["unix_server.py"], offenders

    def test_the_proxies_no_longer_declare_one(self):
        for name in ("network_proxy.py", "skill_proxy.py"):
            text = (SRC / name).read_text(encoding="utf-8")
            assert "socketpair" not in text, name
            assert "DefaultSelector" not in text, name
            assert "UnixSocketServer" in text, name
