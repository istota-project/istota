"""Browser admission coordinates daemon threads and independent skill processes."""

import os
import subprocess
import sys

import pytest


def test_processes_share_admission_and_release_on_exit(tmp_path, monkeypatch):
    from istota.browser_admission import browser_admission, BrowserQueueTimeout

    db_path = tmp_path / "istota.db"
    monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
    child = subprocess.Popen(
        [sys.executable, "-c", "from istota.browser_admission import browser_admission; "
         "import time; "
         "\nwith browser_admission():\n print('locked', flush=True)\n time.sleep(30)"],
        stdout=subprocess.PIPE, text=True, env=os.environ.copy(),
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(BrowserQueueTimeout, match="waiting"):
            with browser_admission(db_path=db_path, queue_timeout=0.02):
                pytest.fail("entered while another process held admission")
    finally:
        child.kill()
        child.wait(timeout=5)
        child.stdout.close()
    with browser_admission(db_path=db_path, queue_timeout=0.2):
        pass


def test_request_waits_before_http_and_releases_on_exception(tmp_path, monkeypatch):
    from istota.browser_admission import browser_admission, browser_request, BrowserQueueTimeout
    import httpx

    monkeypatch.setenv("ISTOTA_DB_PATH", str(tmp_path / "istota.db"))
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["timeout"])
        raise RuntimeError("fetch failed")

    monkeypatch.setattr(httpx, "post", post)
    with browser_admission():
        with pytest.raises(BrowserQueueTimeout):
            browser_request("post", "http://browser/browse", queue_timeout=0.01, timeout=120)
    assert calls == []
    with pytest.raises(RuntimeError, match="fetch failed"):
        browser_request("post", "http://browser/browse", timeout=120)
    assert calls == [120]
    with browser_admission(queue_timeout=0.01):
        pass


def test_browse_cli_process_waits_for_daemon_lock(tmp_path, monkeypatch):
    from istota.browser_admission import browser_admission

    db_path = tmp_path / "istota.db"
    monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
    code = '''
import httpx
from istota.skills.browse import main

def post(url, **kwargs):
    print("HTTP admitted", flush=True)
    assert kwargs["timeout"] == 120
    return httpx.Response(200, json={"status": "ok", "text": "page"})

httpx.post = post
print("ready", flush=True)
main(["get", "https://example.com"])
'''
    import select
    with browser_admission(db_path=db_path):
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=os.environ.copy())
        try:
            assert child.stdout.readline().strip() == "ready"
            assert not select.select([child.stdout], [], [], 0.15)[0]
        except BaseException:
            child.kill()
            child.communicate(timeout=5)
            raise
    output, error = child.communicate(timeout=5)
    assert child.returncode == 0, error
    assert "HTTP admitted" in output


def test_finviz_queue_timeout_is_not_retried(tmp_path, monkeypatch, caplog):
    from istota.browser_admission import BrowserQueueTimeout
    from istota.skills.markets import finviz

    calls = []

    def busy(*args, **kwargs):
        calls.append(args)
        raise BrowserQueueTimeout("Browser busy: timed out waiting for admission")

    monkeypatch.setattr(finviz, "browser_request", busy)
    assert finviz.fetch_finviz_data(retries=2) is None
    assert len(calls) == 1
    assert "waiting for admission" in caplog.text
