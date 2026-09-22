"""Pool lifecycle and independent browser connections."""
import importlib
import json
from urllib.parse import parse_qs, urlsplit, unquote
import sys
import threading
import time
import types
from pathlib import Path
from unittest import mock

import pytest

from tests.support.sleep_spy import sleep_spy


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent / "docker/browser"))
    if "patchright.sync_api" not in sys.modules:
        api = types.ModuleType("patchright.sync_api")
        api.sync_playwright = mock.Mock()
        monkeypatch.setitem(sys.modules, "patchright", types.ModuleType("patchright"))
        monkeypatch.setitem(sys.modules, "patchright.sync_api", api)
    pool = importlib.import_module("pool")
    chrome = importlib.import_module("chrome")
    monkeypatch.setattr(pool, "PROFILE_ROOT", str(tmp_path))
    monkeypatch.setattr(pool, "RUNTIME_DIR", tmp_path / "runtime", raising=False)
    monkeypatch.setattr(pool, "_instances", {})
    monkeypatch.setattr(chrome, "_connections", {})
    monkeypatch.setattr(chrome, "_pw", None)
    monkeypatch.setattr(chrome, "_driver_thread_id", None)
    processes = []

    def popen(argv, **kwargs):
        proc = mock.Mock(pid=4000 + len(processes))
        proc.poll.return_value = None
        processes.append((argv, kwargs, proc))
        return proc

    monkeypatch.setattr(pool.subprocess, "Popen", popen)
    monkeypatch.setattr(pool.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(pool, "_wait_for_vnc", lambda inst: None)
    monkeypatch.setattr(chrome, "_wait_for_chrome_ready", lambda inst: None)
    monkeypatch.setattr(chrome, "_signal_group", lambda *a: False)
    driver = mock.Mock()
    driver.chromium.connect_over_cdp.side_effect = lambda *a, **k: mock.Mock(contexts=[mock.Mock(pages=[])])
    start = mock.Mock(return_value=types.SimpleNamespace(start=mock.Mock(return_value=driver)))
    monkeypatch.setattr(chrome, "sync_playwright", start)
    yield pool, chrome, processes, start
    for inst in pool.live():
        pool.release_slot(inst)


def test_independent_instances_share_one_driver(runtime):
    pool, chrome, processes, start = runtime
    alice, bob = pool.acquire("alice"), pool.acquire("bob")
    assert (alice.display, alice.cdp_port, alice.vnc_port) == (":100", 9300, 5900)
    assert (bob.display, bob.cdp_port, bob.vnc_port) == (":101", 9301, 5901)
    assert Path(alice.profile_dir).name == "alice"
    assert Path(bob.profile_dir).name == "bob"
    assert Path(alice.profile_dir).stat().st_mode & 0o777 == 0o700
    assert alice.proc is not bob.proc
    assert alice.pw_context is not bob.pw_context
    assert start.call_count == 1
    launches = [(argv, kw) for argv, kw, _ in processes if "--no-first-run" in argv]
    assert len(launches) == 2
    for inst, (argv, kw) in zip((alice, bob), launches):
        assert f"--user-data-dir={inst.profile_dir}" in argv
        assert f"--remote-debugging-port={inst.cdp_port}" in argv
        assert kw["env"]["DISPLAY"] == inst.display
        assert "--disk-cache-size=104857600" in argv
    chrome.disconnect_cdp(alice)
    chrome.get_context(bob)
    assert not chrome.is_cdp_connected(alice)
    assert chrome.is_cdp_connected(bob)
    chrome._pw.stop.assert_not_called()


def test_reuse_and_release_preserve_profile(runtime):
    pool, _, processes, _ = runtime
    alice = pool.acquire("alice")
    assert pool.acquire("alice") is alice
    assert len(processes) == 3
    assert pool.instance_for("absent") is None
    handles = [alice.proc, alice.xvfb_proc, alice.x11vnc_proc]
    pool.release_slot(alice)
    assert pool.live() == []
    assert Path(alice.profile_dir).is_dir()
    for proc in handles:
        proc.wait.assert_called()
    assert pool.acquire("bob").slot == 0


def test_failed_launch_cleans_every_child(runtime, monkeypatch):
    pool, chrome, processes, _ = runtime
    monkeypatch.setattr(chrome, "connect_cdp", mock.Mock(side_effect=RuntimeError("connect failed")))
    with pytest.raises(RuntimeError, match="connect failed"):
        pool.acquire("alice")
    assert pool.live() == []
    for _, _, proc in processes:
        proc.wait.assert_called()


def test_capacity_refuses_busy_instances_without_eviction(runtime, monkeypatch):
    pool, _, _, _ = runtime
    monkeypatch.setattr(pool, "MAX_INSTANCES", 1)
    alice = pool.acquire("alice")
    with pytest.raises(pool.PoolFull):
        pool.acquire("bob", exclude={"alice"})
    assert pool.live() == [alice]


def test_shared_driver_refuses_a_second_thread(runtime):
    pool, chrome, _, _ = runtime
    alice = pool.acquire("alice")
    bob = pool.BrowserInstance("bob", "/unused/bob", ":101", 9301, 5901, 1)
    errors = []

    def connect():
        try:
            chrome.connect_cdp(bob)
        except RuntimeError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=connect)
    thread.start()
    thread.join(timeout=2)
    assert errors and "thread" in errors[0]
    assert chrome.is_cdp_connected(alice)


@pytest.mark.parametrize("display", [":100", ":101"])
def test_x11_commands_reach_the_requested_display(runtime, monkeypatch, display):
    import xdotool

    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs["env"]["DISPLAY"])
        if "search" in argv:
            return types.SimpleNamespace(stdout="123\n")
        return types.SimpleNamespace(stdout="WIDTH=1440\nHEIGHT=900\n")

    monkeypatch.setattr(xdotool.subprocess, "run", run)
    assert xdotool.chrome_wid(display=display) == "123"
    xdotool.xdo_key("Tab", display=display)
    assert calls and set(calls) == {display}


def test_display_start_failure_reaps_its_child(runtime, monkeypatch):
    pool, _, processes, _ = runtime
    monkeypatch.setattr(pool, "_wait_for_display", mock.Mock(side_effect=RuntimeError("display failed")))
    with pytest.raises(RuntimeError, match="display failed"):
        pool.acquire("alice")
    assert pool.live() == []
    assert len(processes) == 1
    processes[0][2].wait.assert_called()


def test_dead_driver_is_replaced_and_all_its_connections_are_invalidated(runtime, monkeypatch):
    pool, chrome, _, start = runtime
    alice = pool.acquire("alice")
    bob = pool.acquire("bob")
    dead = chrome._pw
    dead._impl_obj._connection._transport = types.SimpleNamespace(
        _proc=types.SimpleNamespace(returncode=1),
        on_error_future=types.SimpleNamespace(done=lambda: True),
    )
    alice.pw_browser.new_browser_cdp_session.side_effect = RuntimeError("driver closed")
    dead.chromium.connect_over_cdp.side_effect = RuntimeError("driver closed")
    replacement = mock.Mock()
    replacement.chromium.connect_over_cdp.side_effect = lambda *a, **k: mock.Mock(contexts=[mock.Mock(pages=[])])
    start.return_value.start.return_value = replacement
    sleep_spy(monkeypatch, chrome, record=False)

    chrome.connect_cdp(alice)

    assert chrome._pw is replacement
    assert start.call_count == 2
    assert bob.pw_browser is None
    assert bob.pw_context is None
    assert bob.pw_thread_id is None
    chrome.get_context(bob)
    assert bob.pw_context is not alice.pw_context


def test_one_failed_browser_connection_does_not_reset_a_healthy_driver(runtime, monkeypatch):
    pool, chrome, _, start = runtime
    alice = pool.acquire("alice")
    driver = chrome._pw
    driver._impl_obj._connection._transport = types.SimpleNamespace(
        _proc=types.SimpleNamespace(returncode=None),
        on_error_future=types.SimpleNamespace(done=lambda: False),
    )
    context = alice.pw_context
    driver.chromium.connect_over_cdp.side_effect = RuntimeError("CDP port refused")
    sleep_spy(monkeypatch, chrome, record=False)
    with pytest.raises(RuntimeError, match="CDP port refused"):
        pool.acquire("bob")
    assert chrome._pw is driver
    assert start.call_count == 1
    driver.stop.assert_not_called()
    assert chrome.get_context(alice) is context


def test_launch_failure_has_a_distinct_exception_and_reusable_slot(runtime, monkeypatch):
    pool, chrome, _, _ = runtime
    launch = chrome.launch_chrome
    monkeypatch.setattr(chrome, "launch_chrome", mock.Mock(side_effect=FileNotFoundError("missing executable")))
    with pytest.raises(pool.LaunchFailed) as error:
        pool.acquire("alice")
    assert isinstance(error.value.__cause__, FileNotFoundError)
    assert pool.live() == []
    monkeypatch.setattr(chrome, "launch_chrome", launch)
    assert pool.acquire("bob").slot == 0


@pytest.mark.parametrize("already_reaped", [False, True])
def test_driver_exit_is_detected_before_asyncio_dispatches_its_status(runtime, monkeypatch, already_reaped):
    pool, chrome, _, start = runtime
    alice = pool.acquire("alice")
    driver = chrome._pw
    driver._impl_obj._connection._transport = types.SimpleNamespace(
        _proc=types.SimpleNamespace(pid=99999, returncode=None),
        on_error_future=types.SimpleNamespace(done=lambda: False),
    )
    # waitid observes the dead child without stealing asyncio's wait/reap.
    for name in ("P_PID", "WEXITED", "WNOHANG", "WNOWAIT"):
        monkeypatch.setattr(chrome.os, name, getattr(chrome.os, name, 1), raising=False)
    waitid = mock.Mock(
        return_value=types.SimpleNamespace(si_pid=99999),
        side_effect=ChildProcessError() if already_reaped else None,
    )
    monkeypatch.setattr(chrome.os, "waitid", waitid, raising=False)
    replacement = mock.Mock()
    replacement.chromium.connect_over_cdp.return_value = mock.Mock(contexts=[mock.Mock(pages=[])])
    start.return_value.start.return_value = replacement

    chrome.connect_cdp(alice)

    assert chrome._pw is replacement
    waitid.assert_called_once_with(
        chrome.os.P_PID, 99999,
        chrome.os.WEXITED | chrome.os.WNOHANG | chrome.os.WNOWAIT,
    )


def test_release_after_driver_exit_does_not_call_a_dead_browser(runtime):
    pool, chrome, _, _ = runtime
    alice = pool.acquire("alice")
    bob = pool.acquire("bob")
    dead_browser = alice.pw_browser
    driver = chrome._pw
    driver._impl_obj._connection._transport = types.SimpleNamespace(
        _proc=types.SimpleNamespace(returncode=1),
        on_error_future=types.SimpleNamespace(done=lambda: True),
    )
    pool.release_slot(alice)
    dead_browser.close.assert_not_called()
    assert chrome._pw is None
    assert bob.pw_context is None
    assert pool.live() == [bob]


@pytest.mark.parametrize("user_id", [" alice", "alice ", "", ".", "..", "/alice", "alice/bob", "alice/../bob", "alice\0", None, 42])
def test_invalid_profile_identity_starts_nothing(runtime, user_id):
    pool, _, processes, _ = runtime
    with pytest.raises(ValueError, match="Invalid browser user id"):
        pool.acquire(user_id)
    assert pool.live() == []
    assert processes == []
    assert not (Path(pool.PROFILE_ROOT) / "users").exists()


@pytest.mark.parametrize("user_id", ["first.last", "DOMAIN\\alice", "álîce"])
def test_contained_profile_names_keep_their_spelling(runtime, user_id):
    pool, _, _, _ = runtime
    inst = pool.acquire(user_id)
    assert Path(inst.profile_dir) == Path(pool.PROFILE_ROOT) / "users" / user_id


@pytest.mark.parametrize("link_at", ["users", "alice"])
def test_profile_symlinks_cannot_select_another_directory(runtime, tmp_path, link_at):
    pool, _, processes, _ = runtime
    other = tmp_path / "other"
    other.mkdir()
    marker = other / "SingletonLock"
    marker.write_text("retain")
    users = tmp_path / "users"
    if link_at == "users":
        users.symlink_to(other, target_is_directory=True)
    else:
        users.mkdir()
        (users / "alice").symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError):
        pool.acquire("alice")
    assert marker.read_text() == "retain"
    assert processes == []
    assert pool.live() == []


@pytest.mark.parametrize("user_id", ["alice", "DOMAIN\\alice", "álîce", "alice: team", "#alice", "alice: localhost:5999\nbob", "alice%20bob"])
def test_console_routes_preserve_identity_without_token_syntax(runtime, user_id):
    pool, _, _, _ = runtime
    inst = pool.acquire(user_id)
    routes = list((pool.RUNTIME_DIR / "vnc-tokens").iterdir())
    assert len(routes) == 1
    lines = routes[0].read_text().splitlines()
    assert len(lines) == 1
    token, target = lines[0].split(": ")
    assert unquote(token) == user_id
    assert not token.startswith("#")
    assert target == "localhost:5900"
    index = json.loads((pool.RUNTIME_DIR / "web/instances.json").read_text())
    assert index[0]["user"] == user_id
    assert index[0]["slot"] == 0
    path = parse_qs(urlsplit(index[0]["url"]).query)["path"][0]
    routed_token = parse_qs(urlsplit(path).query)["token"][0]
    assert routed_token == token
    assert 0 <= time.time() - index[0]["last_used"] < 2
    pool.release_slot(inst)
    assert list((pool.RUNTIME_DIR / "vnc-tokens").iterdir()) == []
    assert json.loads((pool.RUNTIME_DIR / "web/instances.json").read_text()) == []


def test_console_release_and_slot_reuse_do_not_route_to_previous_user(runtime):
    pool, _, _, _ = runtime
    alice, bob = pool.acquire("alice"), pool.acquire("bob")
    pool.release_slot(alice)
    carol = pool.acquire("carol")
    assert carol.slot == 0
    routes = "".join(p.read_text() for p in (pool.RUNTIME_DIR / "vnc-tokens").iterdir())
    assert "alice:" not in routes
    assert "bob: localhost:5901" in routes
    assert "carol: localhost:5900" in routes
    assert {item["user"] for item in json.loads((pool.RUNTIME_DIR / "web/instances.json").read_text())} == {"bob", "carol"}
    assert pool.instance_for("bob") is bob


def test_console_publication_failure_cleans_processes_and_registry(runtime, monkeypatch):
    pool, _, processes, _ = runtime
    publish = pool._publish_instances
    monkeypatch.setattr(pool, "_publish_instances", mock.Mock(side_effect=OSError("disk full")))
    with pytest.raises(pool.LaunchFailed):
        pool.acquire("alice")
    assert pool.live() == []
    assert list((pool.RUNTIME_DIR / "vnc-tokens").iterdir()) == []
    for _, _, proc in processes:
        proc.wait.assert_called()
    monkeypatch.setattr(pool, "_publish_instances", publish)
    assert pool.acquire("bob").slot == 0


@pytest.mark.parametrize("base", [
    "",
    "https://console.example/vnc.html?resize=scale&path=old#view",
    "https://console.example?resize=scale&path=old#view",
    "https://console.example/?resize=scale&path=old#view",
])
def test_console_url_preserves_configuration(runtime, base):
    pool, _, _, _ = runtime
    inst = pool.acquire("alice")
    url = pool.console_url(inst, base)
    if not base:
        assert url == ""
    else:
        parts = urlsplit(url)
        assert parts.scheme == "https"
        assert parts.netloc == "console.example"
        assert parts.path == "/vnc.html"
        assert parts.fragment == "view"
        assert parse_qs(parts.query) == {"resize": ["scale"], "path": ["websockify/?token=alice"]}


@pytest.mark.parametrize("path,expected", [
    ("/console/", "/console/vnc.html"),
    ("/console/vnc_lite.html", "/console/vnc_lite.html"),
])
def test_console_url_preserves_viewer_prefix_and_filename(runtime, path, expected):
    pool, _, _, _ = runtime
    inst = pool.acquire("alice")
    url = pool.console_url(inst, "https://console.example" + path)
    assert urlsplit(url).path == expected



def test_unremovable_route_does_not_prevent_shutdown_or_reach_reused_slot(runtime, monkeypatch):
    pool, _, processes, _ = runtime
    alice = pool.acquire("alice")
    unlink = Path.unlink

    def fail_route_removal(path, *args, **kwargs):
        if path.parent.name == "vnc-tokens":
            raise PermissionError("route removal refused")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_route_removal)
    pool.release_slot(alice)
    assert pool.live() == []
    for _, _, proc in processes:
        proc.wait.assert_called()
    with pytest.raises(pool.LaunchFailed):
        pool.acquire("bob")
    assert len(processes) == 3
    assert pool.live() == []


def test_capacity_evicts_lru_idle_instance(runtime):
    pool, _, _, _ = runtime
    alice, bob = pool.acquire("alice"), pool.acquire("bob")
    alice.last_used, bob.last_used = 1, 2
    carol = pool.acquire("carol")
    assert carol.slot == alice.slot
    assert pool.live() == [bob, carol]
    assert Path(alice.profile_dir).is_dir()


def test_reap_idle_keeps_live_sessions_and_reuses_slot(runtime, monkeypatch):
    pool, _, _, _ = runtime
    alice, bob = pool.acquire("alice"), pool.acquire("bob")
    alice.last_used = bob.last_used = 0
    monkeypatch.setattr(pool, "INSTANCE_IDLE_S", 100)
    assert pool.reap_idle(101, exclude={"bob"}) == ["alice"]
    assert pool.live() == [bob]
    assert pool.acquire("carol").slot == alice.slot


def test_memory_rejection_starts_nothing(runtime):
    pool, _, processes, _ = runtime
    with pytest.raises(pool.MemoryRejected):
        pool.acquire("alice", memory_pct=lambda: 99)
    assert processes == []
    assert pool.live() == []


def test_memory_reclaims_idle_instance_before_launch(runtime):
    pool, _, _, _ = runtime
    alice = pool.acquire("alice")
    readings = iter([99, 20])
    bob = pool.acquire("bob", memory_pct=lambda: next(readings))
    assert pool.live() == [bob]
    assert Path(alice.profile_dir).is_dir()


def test_released_instance_cannot_be_resurrected_by_late_watchdog(runtime):
    pool, chrome, processes, _ = runtime
    alice = pool.acquire("alice")
    pool.release_slot(alice)
    bob = pool.acquire("bob")
    count = len(processes)
    chrome.recover_wedged_chrome(alice)
    assert len(processes) == count
    assert pool.live() == [bob]
    assert alice.proc is None


def test_watchdog_recovers_only_its_instance(runtime):
    pool, chrome, _, _ = runtime
    alice, bob = pool.acquire("alice"), pool.acquire("bob")
    bob_proc, bob_generation, bob_context = bob.proc, bob.launch_generation, bob.pw_context
    errors = []

    def recover():
        try:
            chrome.recover_wedged_chrome(alice)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=recover)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    assert errors == []
    assert len(alice.wedge_recoveries) == 1
    assert bob.wedge_recoveries == []
    assert (bob.proc, bob.launch_generation, bob.pw_context) == (bob_proc, bob_generation, bob_context)


def test_capacity_eviction_kills_chrome_before_waiting_for_cdp_close(runtime, monkeypatch):
    pool, _, _, _ = runtime
    monkeypatch.setattr(pool, "MAX_INSTANCES", 1)
    alice = pool.acquire("alice")
    stopped = threading.Event()
    alice.proc.wait.side_effect = lambda **kwargs: stopped.set()
    observed = []
    alice.pw_browser.close.side_effect = lambda: observed.append(stopped.wait(0.05))
    bob = pool.acquire("bob")
    assert observed == [True]
    assert pool.live() == [bob]


def test_release_attempts_display_cleanup_when_cdp_teardown_raises(runtime, monkeypatch):
    pool, chrome, processes, _ = runtime
    alice = pool.acquire("alice")
    monkeypatch.setattr(chrome, "disconnect_cdp", mock.Mock(side_effect=RuntimeError("driver unavailable")))
    pool.release_slot(alice)
    assert pool.live() == []
    for _, _, proc in processes:
        proc.wait.assert_called()
