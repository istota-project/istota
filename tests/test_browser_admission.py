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
            browser_request("post", "http://browser/browse", queue_timeout=0.01, timeout=120,
                            headers={"X-Istota-User": "alice"})
    assert calls == []
    with pytest.raises(RuntimeError, match="fetch failed"):
        browser_request("post", "http://browser/browse", timeout=120,
                        headers={"X-Istota-User": "alice"})
    assert calls == [120]
    with browser_admission(queue_timeout=0.01):
        pass


def test_browse_cli_process_waits_for_daemon_lock(tmp_path, monkeypatch):
    from istota.browser_admission import browser_admission

    db_path = tmp_path / "istota.db"
    monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
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
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
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


@pytest.mark.parametrize("headers", [None, {}, {"X-Istota-User": ""}])
def test_a_request_naming_no_user_is_refused_before_it_is_sent(tmp_path, monkeypatch, headers):
    """The API refuses it 400 and every caller read that as an empty page."""
    from istota.browser_admission import (
        BrowserIdentityMissing, browser_admission, browser_request,
    )
    import httpx

    monkeypatch.setenv("ISTOTA_DB_PATH", str(tmp_path / "istota.db"))
    sent = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: sent.append(url))
    kwargs = {} if headers is None else {"headers": headers}
    with pytest.raises(BrowserIdentityMissing, match="X-Istota-User"):
        browser_request("post", "http://browser/render", timeout=5, **kwargs)
    assert sent == []
    assert issubclass(BrowserIdentityMissing, ValueError)
    with browser_admission(queue_timeout=0.01):
        pass


def _no_config_anywhere(tmp_path, monkeypatch):
    """Put every `load_config` candidate out of reach but `/etc`, which a
    developer host does not carry."""
    monkeypatch.delenv("ISTOTA_CONFIG_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def test_no_database_path_is_refused_and_creates_nothing(tmp_path, monkeypatch):
    """A cwd-relative lock coordinates with nobody and left ``data/`` behind (#572)."""
    from istota.browser_admission import BrowserAdmissionUnconfigured, browser_admission

    _no_config_anywhere(tmp_path, monkeypatch)
    monkeypatch.delenv("ISTOTA_DB_PATH", raising=False)
    with pytest.raises(BrowserAdmissionUnconfigured, match="ISTOTA_DB_PATH"):
        with browser_admission(queue_timeout=0.01):
            pytest.fail("admitted with no database path")
    assert list(tmp_path.iterdir()) == []


def test_an_empty_database_path_is_refused(tmp_path, monkeypatch):
    from istota.browser_admission import BrowserAdmissionUnconfigured, browser_admission

    _no_config_anywhere(tmp_path, monkeypatch)
    monkeypatch.setenv("ISTOTA_DB_PATH", "  ")
    with pytest.raises(BrowserAdmissionUnconfigured):
        with browser_admission(db_path="", queue_timeout=0.01):
            pass
    assert list(tmp_path.iterdir()) == []


def test_finviz_does_not_retry_an_unconfigured_admission(tmp_path, monkeypatch):
    """A missing path is not transient; retrying it only sleeps 15 seconds."""
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    _no_config_anywhere(tmp_path, monkeypatch)
    monkeypatch.delenv("ISTOTA_DB_PATH", raising=False)
    import time
    from istota.skills.markets import finviz

    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    assert finviz.fetch_finviz_data(api_url="http://browser", retries=2) is None
    assert slept == []
    assert list(tmp_path.iterdir()) == []


def test_the_loaded_config_names_the_lock_when_the_proxy_withholds_the_path(
    tmp_path, monkeypatch,
):
    """Proxy off: the executor keeps ISTOTA_DB_PATH from the model's env, and
    the CLI takes the same lock as the daemon through the config file."""
    from istota.browser_admission import browser_admission

    _no_config_anywhere(tmp_path, monkeypatch)
    monkeypatch.delenv("ISTOTA_DB_PATH", raising=False)
    state = tmp_path / "state"
    cfg = tmp_path / "istota.toml"
    cfg.write_text(f'db_path = "{state / "istota.db"}"\n')
    monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(cfg))
    with browser_admission(queue_timeout=0.01):
        pass
    assert (state / "browser-admission.lock").is_file()
    assert not (tmp_path / "data").exists()


def test_a_relative_db_path_in_the_config_is_refused(tmp_path, monkeypatch):
    """Relative to the daemon's cwd, which this process cannot know."""
    from istota.browser_admission import BrowserAdmissionUnconfigured, browser_admission

    _no_config_anywhere(tmp_path, monkeypatch)
    monkeypatch.delenv("ISTOTA_DB_PATH", raising=False)
    cfg = tmp_path / "istota.toml"
    cfg.write_text('db_path = "data/istota.db"\n')
    monkeypatch.setenv("ISTOTA_CONFIG_PATH", str(cfg))
    with pytest.raises(BrowserAdmissionUnconfigured):
        with browser_admission(queue_timeout=0.01):
            pass
    assert not (tmp_path / "data").exists()
