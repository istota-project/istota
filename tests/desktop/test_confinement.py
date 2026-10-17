"""Row M9: web UI content cannot call into the Mac app.

A probe page, served on this Mac's loopback, is opened in the app as a remote
profile, through the same window code the local install's web UI uses. It
reports what of Tauri's IPC it can see, then follows a link to another origin.
The witness: `window.__TAURI_INTERNALS__`, `window.__TAURI__`, `window.ipc` and
a WebKit message handler are all absent, the link does not move the window,
and the app opens it in the default browser instead (a browser tab opens).

The other half of the row, the same probe against a page served by the local
stack in an appliance VM, is not run here: it needs a release directory and a
VM made from it (`tests/vm/test_appliance.py`'s builder).

Control: `scripts/test-desktop.sh --control m9` builds the app with the
`m9-negative-control` feature, which puts the web UI in a stock Tauri webview;
the probe then sees `__TAURI_INTERNALS__` and the first test goes red.
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
from urllib.parse import parse_qs, urlparse

import pytest

from tests.support import parity

pytestmark = pytest.mark.desktop

PROBE = """<!doctype html>
<meta charset="utf-8">
<title>M9 probe</title>
<a id="off" href="{off}/link">a link to another origin</a>
<script>
function report(stage) {{
  const handlers = window.webkit && window.webkit.messageHandlers;
  const q = new URLSearchParams({{
    stage: stage,
    internals: typeof window.__TAURI_INTERNALS__,
    tauri: typeof window.__TAURI__,
    ipc: typeof window.ipc,
    webkit_handlers: typeof handlers,
    href: location.href,
  }});
  return fetch('/report?' + q, {{ cache: 'no-store' }});
}}
report('load').then(() => {{
  document.getElementById('off').click();
  setTimeout(() => report('after'), 2000);
}});
</script>
"""


class _Server:
    def __init__(self, page: str | None):
        self.reports: list[dict[str, str]] = []
        self.hits: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server's name)
                url = urlparse(self.path)
                outer.hits.append(url.path)
                if url.path == "/report":
                    outer.reports.append({k: v[0] for k, v in parse_qs(url.query).items()})
                    body, kind = b"ok", "text/plain"
                elif page is not None and url.path == "/probe":
                    body, kind = page.encode(), "text/html; charset=utf-8"
                else:
                    body, kind = b"elsewhere", "text/plain"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(scope="module")
def probe_run(app_executable: Path):
    off = _Server(None)
    off_origin = f"http://127.0.0.1:{off.port}"
    probe = _Server(PROBE.format(off=off_origin))
    state_dir = Path(tempfile.mkdtemp(prefix="istota-desktop-m9-"))
    (state_dir / "state.json").write_text(json.dumps({
        "profiles": [{"id": "probe", "kind": "remote", "url": f"http://127.0.0.1:{probe.port}/probe"}],
        "last_profile": "probe",
    }))
    env = {k: v for k, v in os.environ.items() if k in ("HOME", "USER", "LOGNAME", "TMPDIR", "PATH", "LANG")}
    env["ISTOTA_DESKTOP_STATE_DIR"] = str(state_dir)
    app = subprocess.Popen([str(app_executable)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and app.poll() is None:
            if any(r.get("stage") == "after" for r in probe.reports):
                break
            time.sleep(0.25)
        time.sleep(1)
        exited = app.poll()
    finally:
        if app.poll() is None:
            app.send_signal(signal.SIGTERM)
            try:
                app.wait(timeout=10)
            except subprocess.TimeoutExpired:
                app.kill()
                app.wait()
        probe.close()
        off.close()
    log_path = state_dir / "logs" / "app.log"
    result = {
        "reports": probe.reports,
        "log": log_path.read_text() if log_path.exists() else "",
        "exited_early": exited,
        "stderr": app.stderr.read().decode(errors="replace") if app.stderr else "",
        "off_origin": off_origin,
        "probe_url": f"http://127.0.0.1:{probe.port}/probe",
        "lima_home": sorted(p.name for p in (state_dir / "lima").iterdir()) if (state_dir / "lima").exists() else [],
    }
    shutil.rmtree(state_dir, ignore_errors=True)
    return result


def _report(run, stage: str) -> dict[str, str]:
    found = [r for r in run["reports"] if r.get("stage") == stage]
    assert found, f"the probe sent no '{stage}' report (exited early: {run['exited_early']}); stderr:\n{run['stderr'][-2000:]}"
    return found[0]


@parity.witness("M9")
class TestWebUIContentCannotCallIntoTheApp:
    def test_the_page_sees_no_tauri_ipc(self, probe_run):
        loaded = _report(probe_run, "load")
        assert loaded["internals"] == "undefined", loaded
        assert loaded["tauri"] == "undefined", loaded
        assert loaded["ipc"] == "undefined", loaded
        assert loaded["webkit_handlers"] == "undefined", loaded

    def test_an_off_origin_link_opens_in_the_default_browser(self, probe_run):
        after = _report(probe_run, "after")
        assert after["href"] == probe_run["probe_url"], "the window followed the link"
        assert f"opened in the default browser: {probe_run['off_origin']}/link" in probe_run["log"]

    def test_a_remote_profile_makes_no_vm(self, probe_run):
        assert "$ istota-stack" not in probe_run["log"]
        assert probe_run["lima_home"] == []
