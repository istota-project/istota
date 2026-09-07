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

import logging
import os
import selectors
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

_module_logger = logging.getLogger("istota.unix_server")

# A failing accept() is retried rather than treated as shutdown, but a
# listener that fails forever must not spin. Give up after this many in a row.
MAX_ACCEPT_FAILURES = 20
ACCEPT_RETRY_DELAY_S = 0.05

# How long stop() waits for the accept loop to notice the wake before deciding
# the thread is stuck and its sockets must be left alone.
JOIN_TIMEOUT_S = 5


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
        # accept() on a non-blocking listener yields a non-blocking socket on
        # BSD/macOS and a blocking one on Linux. Normalize, so a handler never
        # depends on which platform it woke up on.
        conn.setblocking(True)
        # One thread per connection, so several can be served at once and a
        # slow handler does not deafen the listener.
        try:
            threading.Thread(
                target=self.handler, args=(conn,),
                daemon=True, name=f"{self.name}-handler",
            ).start()
        except RuntimeError as exc:
            self.logger.error(
                "%s could not start a handler thread: %s", self.label, exc,
            )
            conn.close()
        return True
