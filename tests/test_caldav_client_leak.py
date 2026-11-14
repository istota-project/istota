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

That watchdog was urllib3-future's, which caldav 2.x pulled in through
niquests. caldav 1.x (pinned since ISSUE-944) sits on requests and plain
urllib3, which start no such thread, so counting threads could no longer
fail. The test now checks the invariant itself: every DAVClient that
discovery opens is closed.
"""

from __future__ import annotations

import http.server
import threading

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


def test_discover_calendars_closes_every_client_it_opens(caldav_server, monkeypatch):
    """``discover_calendars_for_task`` closes each DAVClient, failed requests included.

    The stub answers 401, so every discovery fails inside the client:
    the path where a missing ``close()`` used to leak.
    """
    import caldav

    from istota import executor

    opened = []
    closed = []
    real_init = caldav.DAVClient.__init__
    real_close = caldav.DAVClient.close

    def counting_init(self, *args, **kwargs):
        opened.append(self)
        real_init(self, *args, **kwargs)

    def counting_close(self):
        closed.append(self)
        real_close(self)

    monkeypatch.setattr(caldav.DAVClient, "__init__", counting_init)
    monkeypatch.setattr(caldav.DAVClient, "close", counting_close)

    class _Task:
        user_id = "alice"

    config = Config(
        nextcloud=NextcloudConfig(
            url=f"http://127.0.0.1:{caldav_server}",
            username="x",
            app_password="y",
        )
    )

    for _ in range(10):
        executor.discover_calendars_for_task(_Task(), config)

    assert len(opened) == 10
    assert {id(c) for c in closed} == {id(c) for c in opened}, (
        "A DAVClient opened by discover_calendars_for_task was not closed; see ISSUE-101."
    )
