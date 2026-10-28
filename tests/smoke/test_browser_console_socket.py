"""The admin browser console's path through the running stack.

`web` reaches each live browser's x11vnc through a Unix socket in the
`browser-console` volume, which compose mounts in `browser` and `web` only. This
holds the two halves of that design against the shipped compose file: `web`
reads an RFB banner from a live instance's socket, as the uid the web process
runs as, and `web` still has no TCP route to the browser API (ISSUE-699's
network split). The image tier's own socket test checks the permissions with
one container; this one checks the volume wiring between two services.
"""

from __future__ import annotations

import json
import time

import pytest

from istota.lib.browser_console import console_socket_name

from .test_run_contract import _connect

pytestmark = [pytest.mark.smoke, pytest.mark.profile("browser")]

CONSOLE_DIR = "/run/istota-browser-console"
CONSOLE_USER = "console-smoke"
WEB_UID = "10001:10001"

BANNER_PROBE = r"""
import json, socket, sys
conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
conn.settimeout(10)
out = {}
try:
    conn.connect(sys.argv[1])
    out["banner"] = conn.recv(12).decode("ascii", "replace")
except OSError as exc:
    out["error"] = type(exc).__name__
finally:
    conn.close()
print("PROBE=" + json.dumps(out))
"""


def _start_instance(stack) -> None:
    """Ask the browser for a page as `CONSOLE_USER`, which starts that user's instance."""
    deadline = time.monotonic() + 180
    listed = None
    while time.monotonic() < deadline:
        listed = stack.exec(["curl", "-sf", "http://localhost:9223/instances"], service="browser")
        if listed.returncode == 0:
            break
        time.sleep(2)
    assert listed is not None and listed.returncode == 0, listed

    browsed = stack.exec(
        ["curl", "-s", "-X", "POST", "http://localhost:9223/browse",
         "-H", "Content-Type: application/json", "-H", f"X-Istota-User: {CONSOLE_USER}",
         "-d", json.dumps({"url": "http://localhost:9224/live", "timeout": 60})],
        service="browser", timeout=300,
    )
    listed = stack.exec(["curl", "-sf", "http://localhost:9223/instances"], service="browser")
    users = [row.get("user") for row in json.loads(listed.stdout or "{}").get("instances", [])]
    assert CONSOLE_USER in users, f"no live instance: {listed.stdout}\nbrowse: {browsed.stdout[-2000:]}"


def test_web_reads_a_live_instances_banner_through_the_socket(stack):
    _start_instance(stack)
    path = f"{CONSOLE_DIR}/{console_socket_name(CONSOLE_USER)}"

    result = stack.exec(["python3", "-c", BANNER_PROBE, path], service="web", user=WEB_UID)

    line = next((row for row in result.stdout.splitlines() if row.startswith("PROBE=")), "")
    assert line, f"exit {result.returncode}\n{result.stdout}\n{result.stderr}"
    report = json.loads(line[len("PROBE="):])
    assert report.get("banner", "").startswith("RFB 003.0"), report


def test_web_cannot_reach_the_browser_api(stack):
    """The API by name, and the browser's network by address.

    The daemon reaching the same targets proves they are up. The address
    probe uses the liveness port, 9224, because the API's 9223 is published
    on the host's loopback, and Docker Desktop routes one bridge network to
    another container's published port by address (row 17's docstring has
    the measurement; Docker Engine on Debian 13 does not). An unpublished
    port on the same address is what shows there is no route.
    """
    resolved = stack.exec(["getent", "hosts", "browser"]).stdout.split()
    assert resolved, "browser does not resolve from the daemon container"
    targets = ["browser:9223", f"{resolved[0]}:9224"]

    deadline = time.monotonic() + 180
    from_daemon = {}
    while time.monotonic() < deadline:
        from_daemon = _connect(stack, "daemon", targets)
        if set(from_daemon.values()) == {"reached"}:
            break
        time.sleep(2)
    assert set(from_daemon.values()) == {"reached"}, from_daemon

    from_web = _connect(stack, "web", targets + ["istota-browser:9223"])

    reached = sorted(target for target, outcome in from_web.items() if outcome == "reached")
    assert reached == [], f"web reached the browser API: {from_web}"
