"""Container stop reaches the API and gives every profile time to flush."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time
from unittest import mock

import pytest

from tests.test_browser_pool import runtime  # noqa: F401 -- shared browser process fixture

BROWSER = Path(__file__).resolve().parents[1] / "docker/browser"


@pytest.mark.parametrize("repeat_signal", [False, True])
def test_sigterm_runs_cleanup_on_the_main_thread(tmp_path, repeat_signal):
    harness = tmp_path / "api.py"
    harness.write_text(textwrap.dedent("""
        import os, pathlib, runpy, signal, sys, threading, time, types
        root, ready, cleaned = map(pathlib.Path, sys.argv[1:])
        sys.path.insert(0, str(root))
        class App:
            def __init__(self, *args): pass
            def route(self, *args, **kwargs): return lambda fn: fn
            def before_request(self, fn): return fn
            def after_request(self, fn): return fn
            def teardown_request(self, fn): return fn
        flask = types.ModuleType('flask')
        flask.Flask, flask.Response, flask.request = App, object, object()
        flask.jsonify = lambda value: value
        sys.modules['flask'] = flask
        for name in ('browsing', 'render', 'visual'):
            sys.modules[name] = types.ModuleType(name)
        sys.modules['chrome'] = types.SimpleNamespace(
            PROFILE_ROOT=str(root), migrate_legacy_profile=lambda root: None)
        def cleanup():
            cleaned.write_text("started")
            time.sleep(0.2)
            cleaned.write_text(str(threading.current_thread() is threading.main_thread()))
        sys.modules['pool'] = types.SimpleNamespace(cleanup=cleanup)
        import browser_server
        make_server = browser_server.make_browser_server
        def make_test_server(app):
            server, pending = make_server(app, host="127.0.0.1", port=0)
            ready.touch()
            return server, pending
        browser_server.make_browser_server = make_test_server
        os.environ['BROWSER_LIVENESS_PORT'] = '0'
        os.environ['BROWSE_WATCHDOG_DEADLINE_S'] = '0'
        runpy.run_path(str(root / 'browse_api.py'), run_name='__main__')
    """))
    ready, cleaned = tmp_path / "ready", tmp_path / "cleaned"
    proc = subprocess.Popen([sys.executable, str(harness), str(BROWSER), str(ready), str(cleaned)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), proc.communicate(timeout=1)
        proc.send_signal(signal.SIGTERM)
        if repeat_signal:
            deadline = time.monotonic() + 5
            while not cleaned.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert cleaned.exists()
            proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, (stdout, stderr)
        assert cleaned.read_text() == "True"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_retirement_asks_browser_to_flush_before_signalling_renderers(runtime, monkeypatch):  # noqa: F811 -- shared fixture
    pool, chrome, _, _ = runtime
    alice = pool.acquire("alice")
    signals = mock.Mock(return_value=True)
    monkeypatch.setattr(chrome, "_signal_group", signals)
    proc = alice.proc
    chrome.cleanup(alice)
    proc.terminate.assert_called_once()
    proc.wait.assert_called_once_with(timeout=5)
    signals.assert_not_called()


def test_retirement_escalates_a_browser_that_will_not_exit(runtime, monkeypatch):  # noqa: F811 -- shared fixture
    pool, chrome, _, _ = runtime
    alice = pool.acquire("alice")
    signals = mock.Mock(return_value=True)
    monkeypatch.setattr(chrome, "_signal_group", signals)
    proc = alice.proc
    proc.wait.side_effect = [subprocess.TimeoutExpired("chrome", 5), None]
    chrome.cleanup(alice)
    proc.terminate.assert_called_once()
    assert proc.wait.call_count == 2
    signals.assert_called_once_with(proc, signal.SIGKILL)


def test_entrypoint_exec_preserves_identity_and_quoted_profile(tmp_path):
    # Exercise the exec command as a shell program. The rest of entrypoint is
    # root-only filesystem preparation, covered by the image runtime probe.
    entrypoint = (BROWSER / "entrypoint.sh").read_text()
    launch = entrypoint[entrypoint.index("exec "):]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "tini": 'import os, sys; assert sys.argv[1] == "--"; os.execvp(sys.argv[2], sys.argv[2:])',
        "setpriv": 'import os, sys; args = sys.argv[1:]; assert args[:3] == ["--reuid=browser", "--regid=browser", "--init-groups"]; os.execvp(args[3], args[3:])',
        "su": 'raise SystemExit(42)',
        "python": 'import json, os; print(json.dumps({k: os.environ.get(k) for k in ("HOME", "USER", "LOGNAME", "SHELL", "LANG", "TZ", "BROWSER_PROFILE_DIR", "BROWSER_MAX_INSTANCES")}))',
    }.items():
        program = bin_dir / name
        program.write_text(f"#!{sys.executable}\n{body}\n")
        program.chmod(0o755)
    profile = str(tmp_path / "profile with spaces;$literal")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", PROFILE_DIR=profile,
               BROWSER_PROFILE_DIR=profile, LANG="en_US.UTF-8", TZ="Etc/UTC", BROWSER_MAX_INSTANCES="3")
    result = subprocess.run(["bash", "-c", launch], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    import json
    assert json.loads(result.stdout) == {
        "HOME": "/home/browser", "USER": "browser", "LOGNAME": "browser", "SHELL": "/bin/bash",
        "LANG": "en_US.UTF-8", "TZ": "Etc/UTC", "BROWSER_PROFILE_DIR": profile,
        "BROWSER_MAX_INSTANCES": "3",
    }


def test_pool_shutdown_signals_every_browser_before_waiting_for_cdp(runtime):  # noqa: F811 -- shared fixture
    pool, _, _, _ = runtime
    instances = [pool.acquire("alice"), pool.acquire("bob")]
    procs = [inst.proc for inst in instances]
    observed = []
    instances[0].pw_browser.close.side_effect = lambda: observed.append(
        all(proc.terminate.called for proc in procs) and all(inst.retired for inst in instances)
    )
    pool.cleanup()
    assert observed == [True]
    assert pool.live() == []
    for proc in procs:
        proc.terminate.assert_called_once()
        proc.wait.assert_called()
