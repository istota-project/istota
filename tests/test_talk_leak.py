"""Repro + regression test for ISSUE-101 root cause.

The scheduler's long-poll pattern (``poll_talk_conversations`` uses
``asyncio.wait(FIRST_COMPLETED)`` + cancel-the-rest) routinely cancels
in-flight ``httpx.AsyncClient`` requests. If ``__aexit__`` of
``async with httpx.AsyncClient(...)`` is itself interrupted by the
cancellation, ``aclose()`` never finishes and the underlying TCP socket
stays open. On production over ~3 days this accumulated 6000+
``CLOSE-WAIT`` sockets to Nextcloud, exhausting RAM.

These tests spin up a tiny local TCP server that accepts connections,
reads the HTTP request, then holds the socket open without responding.
They drive ``TalkClient`` against it under the same cancel pattern and
count leaked connections. The legacy ``async with httpx.AsyncClient(...)``
path leaks deterministically; the shielded-close helper does not.
"""

from __future__ import annotations

import asyncio
import socket
import threading

import psutil
import pytest

from istota.config import Config, NextcloudConfig
from istota.talk import TalkClient


def _start_long_poll_server(
    *, server_close_after_s: float = 0.5,
) -> tuple[int, threading.Event]:
    """Accept TCP connections, read one HTTP request, then close after a delay.

    Sending FIN from the server mimics Nextcloud's long-poll timeout: the
    server gets bored and closes. If the client never called close() on
    its end (because cancellation interrupted httpx's aclose), the socket
    sits in CLOSE-WAIT on the client side — exactly the prod state.

    Returns ``(port, stop_event)``.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(128)
    srv.settimeout(0.2)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def serve_one(conn: socket.socket) -> None:
        try:
            conn.settimeout(0.5)
            buf = b""
            try:
                while b"\r\n\r\n" not in buf and len(buf) < 8192:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            except (socket.timeout, OSError):
                pass
            # Hold the connection like a long-poll, then close from our end.
            if not stop.wait(server_close_after_s):
                try:
                    conn.sendall(b"HTTP/1.1 304 Not Modified\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    pass
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    def loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=serve_one, args=(conn,), daemon=True).start()
        try:
            srv.close()
        except OSError:
            pass

    threading.Thread(target=loop, daemon=True).start()
    return port, stop


def _count_client_conns(port: int) -> int:
    """Count TCP connections from this process to the test server port."""
    proc = psutil.Process()
    n = 0
    for c in proc.net_connections(kind="tcp"):
        if c.raddr and c.raddr.port == port:
            n += 1
    return n


async def _cancel_cycle(client: TalkClient, parallel: int) -> None:
    """Mimic ``poll_talk_conversations``: spawn N long-polls, cancel all."""
    tasks = [
        asyncio.create_task(
            client.poll_messages(
                conversation_token=f"room{i}",
                last_known_message_id=1,
                timeout=30,
            )
        )
        for i in range(parallel)
    ]
    # Give every task time to actually open its TCP connection and send the
    # request. asyncio.sleep yields control so the coroutines progress past
    # the connect+send phase into the long-poll wait.
    await asyncio.sleep(0.3)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.integration
def test_long_poll_cancellation_does_not_leak_sockets():
    """Cancelling httpx requests mid-flight must close the TCP socket.

    Drives 5 cycles × 4 concurrent cancelled long-polls = 20 requests.
    Pre-fix: ~20 ESTABLISHED sockets remain. Post-fix: 0.
    """
    port, stop = _start_long_poll_server(server_close_after_s=0.5)
    try:
        config = Config(
            nextcloud=NextcloudConfig(
                url=f"http://127.0.0.1:{port}",
                username="x",
                app_password="y",
            )
        )
        client = TalkClient(config)

        baseline = _count_client_conns(port)

        for _ in range(10):
            # Each cycle is its own event loop, matching scheduler's
            # `asyncio.run(poll_talk_conversations(...))` usage.
            asyncio.run(_cancel_cycle(client, parallel=4))

        # Give the server time to send FIN on every connection so any
        # leaked client-side fds show up as CLOSE_WAIT in our count.
        import time as _time
        _time.sleep(1.0)
        leaked = _count_client_conns(port) - baseline
    finally:
        stop.set()

    assert leaked <= 2, (
        f"Leaked {leaked} sockets after 5*4=20 cancelled long-polls. "
        "Cancellation during async-with aclose() is not closing the TCP "
        "connection — see ISSUE-101."
    )
