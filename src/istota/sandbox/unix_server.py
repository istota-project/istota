"""Accept-loop lifecycle for a Unix socket server.

Stdlib-only leaf. Bind, listen, accept, hand each connection to a handler
thread, and shut down promptly. Nothing here decides who may connect or what
a connection is allowed to do: the socket mode is a required argument, and the
handler — with whatever allowlist or credential logic it carries — belongs to
the caller.

Two subtleties this exists to state once, both learned in the proxies it was
extracted from:

* Closing a socket does not reliably wake a thread blocked in ``accept()``.
  ``stop()`` nudges a socketpair instead, so teardown costs microseconds rather
  than a poll interval.
* ``accept()`` on a non-blocking listener yields a non-blocking socket on
  BSD/macOS and a blocking one on Linux. The connection is normalized to
  blocking before a handler ever sees it.
"""

import errno
import logging
import os
import selectors
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

_module_logger = logging.getLogger("istota.sandbox.unix_server")

# A failing accept() is retried rather than treated as shutdown, but a
# listener that fails forever must not spin. Give up after this many in a row.
MAX_ACCEPT_FAILURES = 20
ACCEPT_RETRY_DELAY_S = 0.05
NO_DESCRIPTORS_RETRY_DELAY_S = 0.25

# At most one refusal line per this many seconds: refusals are what a task
# asks for by connecting, so one per connection would hand it the log too.
REFUSAL_LOG_INTERVAL_S = 60

# How long stop() waits for the accept loop to notice the wake before deciding
# the thread is stuck and its sockets must be left alone.
JOIN_TIMEOUT_S = 5


def arm_until(sock: socket.socket, deadline: float) -> None:
    """Make the next operation on ``sock`` end by ``deadline`` (monotonic).

    A socket timeout restarts on every read, so on its own it never ends a peer
    that keeps sending. `TimeoutError` once the deadline has passed.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("deadline passed")
    sock.settimeout(remaining)


def drain_queued(conn: socket.socket) -> None:
    """Read what has already arrived, without waiting for more.

    Unread bytes at close make Linux reset the connection, which can discard a
    refusal before the peer reads it. Leaves ``conn`` non-blocking.
    """
    conn.setblocking(False)
    try:
        for _ in range(16):
            if not conn.recv(65536):
                break
    except OSError:
        pass


def peer_has_closed(conn: socket.socket) -> bool:
    """Whether the peer is gone, without waiting or consuming anything.

    Asks for readability first: `recv` on a socket with a timeout waits for data
    whatever flags it is given.
    """
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(conn, selectors.EVENT_READ)
            if not selector.select(0):
                return False
        return conn.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


class UnixSocketServer:
    """Listen on ``socket_path`` and run ``handler`` per connection, off-thread.

    ``socket_mode`` is required and keyword-only. It is an access-control
    decision — on a Unix socket the filesystem mode is the only gate before the
    handler runs — so every caller states its own rather than inheriting one
    from here. It is applied after ``bind()`` and before ``listen()``: the path
    exists after the bind but a ``connect()`` is refused until the listen, so
    there is no window in which the socket is both reachable and world-writable.

    ``name`` is the thread name (handler threads get ``f"{name}-handler"``).
    ``label`` is the human prefix in log lines and defaults to ``name``.
    ``logger`` defaults to this module's, so a caller that wants its own
    logger's level and handlers applied to lifecycle messages passes it in.

    ``max_connections`` bounds the handler threads. Past it a connection is
    answered ``busy_reply`` and closed from the accept loop, so a refusal costs
    no thread and cannot stall the listener (#890). ``stop()`` shuts down every
    live connection, which wakes a handler blocked on it.
    """

    def __init__(
        self,
        socket_path: Path,
        handler: Callable[[socket.socket], None],
        *,
        name: str,
        socket_mode: int,
        backlog: int = 64,
        label: str | None = None,
        logger: logging.Logger | None = None,
        max_connections: int | None = None,
        busy_reply: bytes = b"",
    ):
        self.socket_path = socket_path
        self.handler = handler
        self.name = name
        self.socket_mode = socket_mode
        self.backlog = backlog
        self.label = label if label is not None else name
        self.logger = logger if logger is not None else _module_logger
        self._server_sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake_r: socket.socket | None = None
        self._wake_w: socket.socket | None = None
        self._accept_failures = 0
        self.max_connections = max_connections
        self.busy_reply = busy_reply
        # A dup of each connection, because a handler may detach the socket it
        # was given: `wrap_socket` does, which leaves that object no fd to shut.
        self._live: set[socket.socket] = set()
        self._live_lock = threading.Lock()
        self._last_refusal_log = 0.0

    def start(self) -> None:
        # A second start() over a live one would orphan the first listener and
        # its thread: stop() only knows about the newest, so the old thread
        # runs forever on a path that has since been rebound, holding both fds.
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError(
                f"{self.label} is already running on {self.socket_path}"
            )

        # Clean up a stale socket file left by a process that did not stop.
        if self.socket_path.exists():
            self.socket_path.unlink()

        self._stop_event.clear()
        self._accept_failures = 0
        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._server_sock.bind(str(self.socket_path))
            # Between bind() and listen(): the path exists after the bind, but
            # a connect() is refused until the listen, so there is no moment
            # when the socket is both reachable and at mkstemp-style defaults.
            os.chmod(str(self.socket_path), self.socket_mode)
            self._server_sock.listen(self.backlog)
            self._server_sock.setblocking(False)
            # Closing a socket does not reliably wake a thread blocked in
            # accept(), so stop() nudges this pair instead of the loop polling
            # on a timeout.
            self._wake_r, self._wake_w = socket.socketpair()
            self._wake_r.setblocking(False)

            self._thread = threading.Thread(
                target=self._accept_loop, daemon=True, name=self.name,
            )
            self._thread.start()
        except BaseException:
            # chmod, listen and Thread.start can all fail after the bind has
            # already taken the path. Nothing has entered the loop yet, so the
            # fds are ours to close; dropping them instead would hold a bound
            # listener until a gc pass and leave the path behind.
            self._close_sockets()
            self._server_sock = self._wake_r = self._wake_w = None
            self._thread = None
            try:
                self.socket_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def stop(self) -> None:
        self._stop_event.set()
        if self._wake_w:
            try:
                self._wake_w.sendall(b"\x00")
            except OSError:
                pass
        stuck = False
        if self._thread:
            self._thread.join(timeout=JOIN_TIMEOUT_S)
            stuck = self._thread.is_alive()
        self._thread = None
        self._shut_live_connections()

        if stuck:
            # Never close a socket the accept loop may still be selecting on:
            # epoll and kqueue drop a closed fd from the interest set silently,
            # so the thread would block forever on numbers the OS is free to
            # hand to unrelated code. The listener and the wake-read end are
            # held by the loop's own selector and survive the clearing below;
            # the wake-write end is not, and its close is what leaves the loop
            # a permanent EOF to wake on. Say so rather than closing anything.
            self.logger.warning(
                "%s accept loop did not exit within %ds; leaving its "
                "sockets open rather than closing them underneath it",
                self.label, JOIN_TIMEOUT_S,
            )
        else:
            self._close_sockets()
        self._server_sock = self._wake_r = self._wake_w = None
        try:
            self.socket_path.unlink(missing_ok=True)
        except OSError:
            pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def _shut_live_connections(self) -> None:
        with self._live_lock:
            live = list(self._live)
        for handle in live:
            try:
                handle.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _refuse(self, conn: socket.socket) -> None:
        """Answer from the accept loop: nothing here may block."""
        now = time.monotonic()
        if now - self._last_refusal_log >= REFUSAL_LOG_INTERVAL_S:
            self._last_refusal_log = now
            self.logger.warning(
                "%s at its limit of %d connections; refusing",
                self.label, self.max_connections,
            )
        try:
            drain_queued(conn)
            if self.busy_reply:
                conn.send(self.busy_reply)
        except OSError:
            pass
        finally:
            conn.close()

    def _serve(self, conn: socket.socket, handle: socket.socket) -> None:
        try:
            self.handler(conn)
        finally:
            with self._live_lock:
                self._live.discard(handle)
            handle.close()

    def _close_sockets(self) -> None:
        for sock in (self._server_sock, self._wake_r, self._wake_w):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

    def _accept_loop(self) -> None:
        # Bound once and used in place of the attributes for the life of the
        # loop. stop() clears those the moment it gives up on the join, and a
        # loop still inside select() would then fail to recognize its own wake
        # socket and dereference None where the listener used to be — an
        # AttributeError that ends the thread by crashing it rather than by
        # the clean exit the wake is there to produce.
        server_sock = self._server_sock
        wake_r = self._wake_r
        with selectors.DefaultSelector() as sel:
            sel.register(server_sock, selectors.EVENT_READ)
            sel.register(wake_r, selectors.EVENT_READ)
            while not self._stop_event.is_set():
                if not self._accept_once(sel, server_sock, wake_r):
                    break

    def _accept_once(
        self,
        sel: selectors.BaseSelector,
        server_sock: socket.socket,
        wake_r: socket.socket,
    ) -> bool:
        """Wait for one readiness event. False means the loop should stop."""
        events = sel.select()
        # Shutdown wins over a connection that became ready in the same call.
        # Accepting it here would give a handler thread — and whatever the
        # handler is authorized to reach — a lifetime past the stop() meant to
        # end that authority.
        if any(key.fileobj is wake_r for key, _ in events):
            return False

        try:
            conn, _ = server_sock.accept()
        except BlockingIOError:
            return True
        except OSError as exc:
            # Out of descriptors clears when something closes, so it never
            # counts toward giving up: another task's load must not end this
            # task's listener for good. Slower than the retry below, since
            # the connection stays queued and the listener stays readable.
            if exc.errno in (errno.EMFILE, errno.ENFILE):
                now = time.monotonic()
                if now - self._last_refusal_log >= REFUSAL_LOG_INTERVAL_S:
                    self._last_refusal_log = now
                    self.logger.warning("%s accept failed: %s", self.label, exc)
                time.sleep(NO_DESCRIPTORS_RETRY_DELAY_S)
                return True
            # One failed accept must not end the server. The listening socket
            # stays bound either way, so a dead loop turns every later connect
            # into a hang in the backlog instead of a clean refusal.
            self._accept_failures += 1
            if self._accept_failures > MAX_ACCEPT_FAILURES:
                self.logger.error("%s accept failed %d times, stopping: %s",
                                  self.label, self._accept_failures, exc)
                return False
            self.logger.warning("%s accept failed: %s", self.label, exc)
            time.sleep(ACCEPT_RETRY_DELAY_S)
            return True

        self._accept_failures = 0
        with self._live_lock:
            full = (
                self.max_connections is not None
                and len(self._live) >= self.max_connections
            )
        if full:
            self._refuse(conn)
            return True
        # accept() on a non-blocking listener yields a non-blocking socket on
        # BSD/macOS and a blocking one on Linux. Normalize, so a handler never
        # depends on which platform it woke up on.
        conn.setblocking(True)
        # One thread per connection, so several can be served at once and a
        # slow handler does not deafen the listener.
        try:
            handle = conn.dup()
        except OSError as exc:
            self.logger.error("%s could not track a connection: %s", self.label, exc)
            conn.close()
            return True
        with self._live_lock:
            self._live.add(handle)
        try:
            threading.Thread(
                target=self._serve, args=(conn, handle),
                daemon=True, name=f"{self.name}-handler",
            ).start()
        except RuntimeError as exc:
            self.logger.error(
                "%s could not start a handler thread: %s", self.label, exc,
            )
            with self._live_lock:
                self._live.discard(handle)
            handle.close()
            conn.close()
        return True
