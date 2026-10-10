"""The supervisor's half of `devbox reset`: one exit status stops the container.

`istota-exec-run` respawns the exec server whatever it exits with, except the
status the server uses after answering a `restart` request. On that one it
exits itself, which takes the container down so `restart: unless-stopped` can
bring it back. Executed here against a stub server, on the host.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from istota.devbox.exec_protocol import RESTART_EXIT_STATUS

REPO = Path(__file__).resolve().parent.parent
SUPERVISOR = REPO / "docker" / "devbox" / "scripts" / "istota-exec-run"


def _start(tmp_path: Path, server_exit: int, *, stderr=subprocess.PIPE) -> subprocess.Popen:
    stub = tmp_path / "stub-server"
    stub.write_text(f"#!/bin/sh\nexit {server_exit}\n")
    stub.chmod(0o755)

    body = SUPERVISOR.read_text().replace('\nmain "$@"\n', "\n")
    assert 'main "$@"' not in body
    stripped = tmp_path / "supervisor.sh"
    stripped.write_text(body)

    driver = tmp_path / "driver.sh"
    driver.write_text(
        f". {stripped}\n"
        f'SERVER="{stub}"\n'
        f'HOME_DIR="{tmp_path}/no-such-home"\n'
        f'STAGING_DIR="{tmp_path}/staging"\n'
        f'SOCKET="{tmp_path}/exec.sock"\n'
        f'REPOS_ROOT="{tmp_path}"\n'
        "main\n"
        'echo "MAIN_RETURNED $?"\n'
    )
    return subprocess.Popen(
        ["sh", str(driver)], stdout=subprocess.PIPE, stderr=stderr,
        text=True, env=dict(os.environ),
    )


def test_the_restart_status_makes_the_supervisor_exit(tmp_path):
    proc = _start(tmp_path, RESTART_EXIT_STATUS)
    out, err = proc.communicate(timeout=30)

    assert proc.returncode == 0, err
    assert "MAIN_RETURNED 0" in out
    assert "restart requested over the exec transport" in err


def test_any_other_status_is_respawned(tmp_path):
    log = tmp_path / "supervisor.log"
    with open(log, "w") as handle:
        proc = _start(tmp_path, 1, stderr=handle)
        try:
            # Polled rather than slept: under a loaded xdist run the first
            # server exit can take longer than any fixed pause.
            deadline = time.monotonic() + 30
            while "exited with status 1" not in log.read_text():
                assert proc.poll() is None, "the supervisor exited on an ordinary crash"
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.1)
            assert proc.poll() is None, "the supervisor exited on an ordinary crash"
        finally:
            proc.kill()
            proc.communicate(timeout=10)


def test_the_supervisor_names_the_same_status_as_the_protocol():
    match = re.search(r"^RESTART_EXIT_STATUS=(\d+)$", SUPERVISOR.read_text(), re.M)
    assert match, "istota-exec-run no longer declares RESTART_EXIT_STATUS"
    assert int(match.group(1)) == RESTART_EXIT_STATUS
