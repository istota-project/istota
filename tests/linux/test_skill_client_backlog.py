"""`istota-skill` against a listen backlog the kernel reports as full (#876).

On Linux a connect to a full `AF_UNIX` backlog on a socket with a timeout
armed fails at once with `EAGAIN`, which CPython raises as `BlockingIOError`
without polling. The default suite simulates that error, because macOS reports
the same state as `ECONNREFUSED`; this file produces it for real and runs the
client as the separate process it is in a task.

Run with `scripts/test-linux.sh`. Carries the `linux` marker; needs no bwrap.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.linux


def _full_backlog_server(sock_path):
    """A listener with `listen(1)` and its queue filled by idle connections."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    fillers = []
    while True:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1)
        try:
            probe.connect(str(sock_path))
        except BlockingIOError:
            probe.close()
            break
        fillers.append(probe)
        assert len(fillers) < 16, "the backlog never filled"
    return server, fillers


def _client(sock_path, wait):
    return subprocess.Popen(
        [sys.executable, "-m", "istota.skill_client", "kv", "namespaces"],
        env={
            "PATH": "/usr/bin:/bin",
            "ISTOTA_SKILL_PROXY_SOCK": str(sock_path),
            "ISTOTA_SKILL_CLIENT_WAIT": str(wait),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_the_client_waits_for_room_and_gets_its_answer(tmp_path):
    sock_path = tmp_path / "s.sock"
    server, fillers = _full_backlog_server(sock_path)
    answered = threading.Event()

    def drain_then_answer():
        time.sleep(0.5)
        while not answered.is_set():
            conn, _ = server.accept()
            request = conn.recv(65536)
            if request:
                response = {"stdout": "served\n", "stderr": "", "returncode": 0}
                conn.sendall((json.dumps(response) + "\n").encode())
                answered.set()
            conn.close()

    try:
        client = _client(sock_path, wait=10)
        time.sleep(0.2)
        thread = threading.Thread(target=drain_then_answer, daemon=True)
        thread.start()
        for filler in fillers:
            filler.close()
        out, err = client.communicate(timeout=30)
    finally:
        server.close()

    assert client.returncode == 0, err
    assert out == "served\n"
    assert "Traceback" not in err


def test_a_backlog_that_never_drains_is_one_clean_line(tmp_path):
    sock_path = tmp_path / "s.sock"
    server, fillers = _full_backlog_server(sock_path)
    try:
        out, err = _client(sock_path, wait=1).communicate(timeout=30)
    finally:
        for filler in fillers:
            filler.close()
        server.close()

    assert err == "Skill proxy busy: could not connect within 1s\n"
    assert out == ""
