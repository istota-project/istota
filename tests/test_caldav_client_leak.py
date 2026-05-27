"""Regression test for ISSUE-101 root cause: leaked caldav.DAVClient.

Production diagnosis (2026-05-27): the scheduler had 6234 background
threads and 6214 CLOSE-WAIT sockets to Nextcloud after 3 days. ``py-spy
dump`` showed almost every thread was urllib3's
``idle_conn_watch_task`` — the background monitor each
``HTTPConnectionPool`` spawns on first connection. ``caldav.DAVClient``
wraps a ``requests.Session`` whose ``HTTPAdapter`` owns the urllib3
pools, and ``executor.discover_calendars_for_task`` was constructing a
fresh DAVClient per task without ever calling ``client.close()``. Every
task that issued a CalDAV request leaked one watchdog thread + one
socket.

Calling ``client.close()`` (or using DAVClient as a context manager)
closes the session, which closes the urllib3 pools, which sets each
pool's ``_background_monitoring_stop`` event and lets the watchdog
thread exit.
"""

from __future__ import annotations

import http.server
import socket
import threading
import time

import pytest

from istota.config import Config, NextcloudConfig


class _CalDAVStubHandler(http.server.BaseHTTPRequestHandler):
    """Just enough surface to make caldav's ``principal()`` issue a request.

    We don't care about correctness — we only need urllib3 to actually
    open a connection and instantiate a pool, which is what spawns the
    leaked watchdog thread. Any response (even 401) does that.
    """

    def do_PROPFIND(self):  # noqa: N802 (HTTP method casing)
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(200)
        self.send_header("DAV", "1, 2, 3, calendar-access")
        self.send_header("Allow", "OPTIONS, PROPFIND")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # quiet
        pass


@pytest.fixture
def caldav_server():
    """Local HTTP server that handles enough CalDAV verbs to trigger a pool."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CalDAVStubHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        srv.shutdown()
        srv.server_close()


def _watchdog_thread_count() -> int:
    return sum(
        1
        for t in threading.enumerate()
        if "idle_conn_watch_task" in (t.name or "")
    )


def test_discover_calendars_does_not_leak_urllib3_watchdog_threads(caldav_server):
    """``discover_calendars_for_task`` must not leak background threads.

    Each leaked ``urllib3.HTTPConnectionPool`` spawns an
    ``idle_conn_watch_task`` daemon thread that lives forever unless the
    pool is closed. In a long-running daemon this kills the process.
    """
    from istota import executor

    class _Task:
        user_id = "alice"

    config = Config(
        nextcloud=NextcloudConfig(
            url=f"http://127.0.0.1:{caldav_server}",
            username="x",
            app_password="y",
        )
    )

    baseline = _watchdog_thread_count()

    for _ in range(10):
        executor.discover_calendars_for_task(_Task(), config)

    # Watchdog threads are daemons that exit on the next wait() iteration
    # once their pool's stop event fires. Give them a beat to actually
    # die so we measure after-cleanup state.
    time.sleep(0.1)

    leaked = _watchdog_thread_count() - baseline
    assert leaked == 0, (
        f"Leaked {leaked} urllib3 idle_conn_watch_task threads after 10 "
        "discover_calendars_for_task calls. The DAVClient is not being "
        "closed — see ISSUE-101."
    )
