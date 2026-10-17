"""Remote profiles: each has a website data store of its own.

Two loopback servers stand in for two remote installs on one host, at two
ports. Cookies are scoped by host and not by port, so in one shared store a
cookie the first install set would be sent to the second. The app is launched
twice from a state directory naming both as remote profiles: first with the
first one open, whose page sets a cookie and shows it was kept by fetching
again; then with the second one open, whose requests must carry no cookie.

The stores are `WKWebsiteDataStore(forIdentifier:)`, persistent, and kept by
WebKit outside the state directory (`~/Library/WebKit/<bundle id>/`); the
module removes the two it made, found by the identifier the app derives
(`profiles::data_store_id`, computed again here).

Control: `scripts/test-desktop.sh --control stores` builds the
`stores-negative-control` feature, which puts every remote window on the
default store; `test_the_second_install_sees_no_cookie_of_the_first` must go
red.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.desktop.stores import data_store_id, on_disk, remove

pytestmark = pytest.mark.desktop

INSTANCE = "istota-dt"
PAGE = """<!doctype html>
<meta charset="utf-8">
<title>profile probe</title>
<script>
fetch('/report?cookie=' + encodeURIComponent(document.cookie), { cache: 'no-store' });
</script>
"""


def test_the_store_identifier_is_the_apps():
    """Pinned in profiles.rs's tests too, so the two cannot drift apart."""
    assert data_store_id(INSTANCE, "dt-a").hex == "13f0730808848d93bf90807f8ac58ca6"


class _Install:
    """One remote install: a page that sets a cookie, and every Cookie header seen."""

    def __init__(self, sets: str | None):
        self.cookies: list[tuple[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server's name)
                path = self.path.split("?", 1)[0]
                outer.cookies.append((path, self.headers.get("Cookie") or ""))
                body = PAGE.encode() if path == "/page" else b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8" if path == "/page" else "text/plain")
                if sets and path == "/page":
                    self.send_header("Set-Cookie", f"{sets}; Max-Age=600; Path=/")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/page"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reported(self) -> bool:
        return any(path == "/report" for path, _ in self.cookies)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _launch(app_executable: Path, state_dir: Path, profile: str, install: _Install) -> str:
    state = json.loads((state_dir / "state.json").read_text())
    state["last_profile"] = profile
    (state_dir / "state.json").write_text(json.dumps(state))
    env = {k: v for k, v in os.environ.items() if k in ("HOME", "USER", "LOGNAME", "TMPDIR", "PATH", "LANG")}
    env.update({
        "ISTOTA_DESKTOP_STATE_DIR": str(state_dir),
        "ISTOTA_DESKTOP_INSTANCE": INSTANCE,
        "ISTOTA_DESKTOP_OPENER": "/usr/bin/true",
    })
    app = subprocess.Popen([str(app_executable)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and app.poll() is None and not install.reported():
            time.sleep(0.25)
        # WebKit's network process writes cookies to disk shortly after a change.
        time.sleep(5)
    finally:
        if app.poll() is None:
            app.send_signal(signal.SIGTERM)
            try:
                app.wait(timeout=10)
            except subprocess.TimeoutExpired:
                app.kill()
                app.wait()
    return app.stderr.read().decode(errors="replace") if app.stderr else ""


@pytest.fixture(scope="module")
def two_installs(app_executable: Path):
    first, second = _Install("istota_dt=from-the-first"), _Install(None)
    state_dir = Path(tempfile.mkdtemp(prefix="istota-desktop-profiles-"))
    (state_dir / "state.json").write_text(json.dumps({
        "profiles": [
            {"id": "local", "kind": "local"},
            {"id": "dt-a", "kind": "remote", "url": first.url},
            {"id": "dt-b", "kind": "remote", "url": second.url},
        ],
        "last_profile": "dt-a",
    }))
    ids = (data_store_id(INSTANCE, "dt-a"), data_store_id(INSTANCE, "dt-b"))
    try:
        stderr = _launch(app_executable, state_dir, "dt-a", first)
        stderr += _launch(app_executable, state_dir, "dt-b", second)
        stores = [p.name for p in on_disk(*ids)]
        lima = sorted(p.name for p in (state_dir / "lima").iterdir()) if (state_dir / "lima").exists() else []
        log = (state_dir / "logs" / "app.log").read_text() if (state_dir / "logs" / "app.log").exists() else ""
    finally:
        first.close()
        second.close()
        remove(*ids)
        shutil.rmtree(state_dir, ignore_errors=True)
    return {"first": first.cookies, "second": second.cookies, "stores": stores, "lima": lima,
            "log": log, "stderr": stderr}


class TestEachRemoteProfileHasItsOwnStore:
    def test_the_first_install_kept_its_cookie(self, two_installs):
        reports = [cookie for path, cookie in two_installs["first"] if path == "/report"]
        assert reports, f"the first install's page never reported; stderr:\n{two_installs['stderr'][-2000:]}"
        assert "istota_dt=from-the-first" in reports[0], two_installs["first"]

    def test_the_second_install_sees_no_cookie_of_the_first(self, two_installs):
        assert any(path == "/report" for path, _ in two_installs["second"]), (
            f"the second install's page never loaded; log:\n{two_installs['log'][-2000:]}")
        assert all("istota_dt" not in cookie for _, cookie in two_installs["second"]), two_installs["second"]

    def test_each_store_is_the_identifier_the_app_derives(self, two_installs):
        assert len(two_installs["stores"]) == 2, two_installs["stores"]

    def test_remote_profiles_make_no_vm(self, two_installs):
        assert "$ istota-stack" not in two_installs["log"]
        assert two_installs["lima"] == []
