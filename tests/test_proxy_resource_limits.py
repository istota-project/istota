"""What one task can make the host do through its proxies (#890).

Each limit is exceeded modestly against the real server: a refusal comes back
promptly, host threads stay bounded, and an unrelated task's proxy still works.
"""

import errno
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

from istota.sandbox import network_proxy as network_proxy_module
from istota.sandbox import skill_proxy as skill_proxy_module
from istota.sandbox import unix_server as unix_server_module
from istota.sandbox.network_proxy import NetworkProxy
from istota.sandbox.process_group import OutputLimitExceeded, run_capture
from istota.sandbox.skill_proxy import SkillProxy
from istota.sandbox.unix_server import UnixSocketServer


# `fake_skill` replaces `sys.executable`, which the proxy spawns.
REAL_PYTHON = sys.executable


@pytest.fixture
def sock_path():
    # AF_UNIX paths are short on macOS, so not tmp_path.
    path = Path(tempfile.gettempdir()) / f"istota-limit-{uuid.uuid4().hex[:8]}.sock"
    yield path
    path.unlink(missing_ok=True)


def _connect(path: Path) -> socket.socket:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(5)
    conn.connect(str(path))
    return conn


def _read_all(conn: socket.socket) -> bytes:
    data = b""
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                return data
            data += chunk
    except OSError:
        return data


def _wait_until(predicate, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _skill_call(path: Path, skill: str = "fake", args=()) -> dict:
    with _connect(path) as conn:
        conn.settimeout(30)
        try:
            conn.sendall(json.dumps({"skill": skill, "args": list(args)}).encode() + b"\n")
        except BrokenPipeError:
            pass  # Refused before the request went; the refusal is still readable.
        return json.loads(_read_all(conn))


@pytest.fixture
def fake_skill(tmp_path, monkeypatch):
    """Make the proxy's spawn run a script of the test's, for real."""
    def install(body: str) -> None:
        script = tmp_path / "fake-skill"
        script.write_text("#!/bin/sh\n" + body + "\n")
        script.chmod(0o755)
        monkeypatch.setattr(skill_proxy_module.sys, "executable", str(script))
    return install


class TestTheServerBoundsItsHandlers:
    def test_a_connection_past_the_limit_is_refused_at_once(self, sock_path):
        release = threading.Event()
        peak = []

        def handler(conn):
            peak.append(threading.active_count())
            release.wait(10)
            conn.close()

        server = UnixSocketServer(
            sock_path, handler, name="limit-test", socket_mode=0o600,
            max_connections=2, busy_reply=b"busy\n",
        )
        held = []
        with server:
            try:
                held = [_connect(sock_path), _connect(sock_path)]
                assert _wait_until(lambda: len(peak) == 2)
                threads = threading.active_count()

                started = time.monotonic()
                for _ in range(10):
                    with _connect(sock_path) as refused:
                        assert _read_all(refused) == b"busy\n"
                assert time.monotonic() - started < 2
                assert threading.active_count() <= threads
            finally:
                release.set()
                for conn in held:
                    conn.close()

    def test_a_finished_handler_gives_its_slot_back(self, sock_path):
        def handler(conn):
            conn.sendall(b"served\n")
            conn.close()

        server = UnixSocketServer(
            sock_path, handler, name="limit-test", socket_mode=0o600,
            max_connections=1, busy_reply=b"busy\n",
        )
        with server:
            for _ in range(5):
                with _connect(sock_path) as conn:
                    assert _read_all(conn) == b"served\n"
                assert _wait_until(lambda: not server._live)

    def test_running_out_of_descriptors_does_not_end_the_listener(self, sock_path, monkeypatch):
        """Another task's load can cause this, so it must not end this task's server."""
        monkeypatch.setattr(unix_server_module, "NO_DESCRIPTORS_RETRY_DELAY_S", 0.001)

        def handler(conn):
            conn.sendall(b"served\n")
            conn.close()

        server = UnixSocketServer(sock_path, handler, name="limit-test", socket_mode=0o600)
        failures = {"left": unix_server_module.MAX_ACCEPT_FAILURES + 5}
        real_accept = socket.socket.accept

        def accept(self):
            if failures["left"] > 0:
                failures["left"] -= 1
                raise OSError(errno.EMFILE, "Too many open files")
            return real_accept(self)

        monkeypatch.setattr(socket.socket, "accept", accept)
        with server:
            with _connect(sock_path) as conn:
                assert _read_all(conn) == b"served\n"

    def test_stop_wakes_a_handler_blocked_on_its_connection(self, sock_path):
        finished = threading.Event()

        def handler(conn):
            try:
                conn.recv(1)
            finally:
                conn.close()
                finished.set()

        server = UnixSocketServer(
            sock_path, handler, name="limit-test", socket_mode=0o600,
        )
        server.start()
        client = _connect(sock_path)
        try:
            assert _wait_until(lambda: len(server._live) == 1)
            server.stop()
            assert finished.wait(2)
        finally:
            client.close()


class TestTheOpenFileLimit:
    def _run(self, monkeypatch, soft, hard):
        import resource

        from istota import scheduler

        set_to = []
        monkeypatch.setattr(resource, "getrlimit", lambda _kind: (soft, hard))
        monkeypatch.setattr(resource, "setrlimit", lambda _kind, value: set_to.append(value))
        scheduler._raise_open_file_limit()
        return set_to

    def test_docker_s_low_soft_limit_is_raised(self, monkeypatch):
        assert self._run(monkeypatch, 1024, 524288) == [(16384, 524288)]

    def test_the_hard_limit_is_never_passed(self, monkeypatch):
        assert self._run(monkeypatch, 1024, 4096) == [(4096, 4096)]

    def test_a_higher_limit_is_left_alone(self, monkeypatch):
        assert self._run(monkeypatch, 1048576, 1048576) == []


class TestRunCaptureBoundsOutput:
    def test_output_past_the_cap_kills_the_group_and_raises(self, tmp_path):
        marker = tmp_path / "child.pid"
        script = f"sleep 60 & echo $! > {marker}; while :; do echo xxxxxxxx; done"
        with pytest.raises(OutputLimitExceeded):
            run_capture(
                ["/bin/sh", "-c", script], timeout=30, env={"PATH": "/usr/bin:/bin"},
                max_output_bytes=4096,
            )
        child = int(marker.read_text())
        assert _wait_until(lambda: not _alive(child))

    def test_output_under_the_cap_is_text_as_usual(self):
        result = run_capture(
            ["/bin/sh", "-c", "printf out; printf err >&2; exit 3"],
            timeout=10, env={"PATH": "/usr/bin:/bin"}, max_output_bytes=4096,
        )
        assert (result.stdout, result.stderr, result.returncode) == ("out", "err", 3)

    def test_the_timeout_is_absolute_against_a_steady_writer(self):
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            run_capture(
                ["/bin/sh", "-c", "while :; do echo x; sleep 0.05; done"],
                timeout=0.5, env={"PATH": "/usr/bin:/bin"},
                max_output_bytes=1024 * 1024,
            )
        assert time.monotonic() - started < 5

    def test_a_grandchild_holding_the_pipe_does_not_outlast_the_timeout(self):
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            run_capture(
                ["/bin/sh", "-c", "sleep 60 & exit 0"],
                timeout=0.5, env={"PATH": "/usr/bin:/bin"}, max_output_bytes=4096,
            )
        assert time.monotonic() - started < 5


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # A killed child of an exited parent may linger as a zombie for a moment.
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


class TestSkillProxyLimits:
    def test_an_oversized_request_is_refused_without_buffering_it(self, sock_path):
        with SkillProxy(sock_path, {}, {}, allowed_skills=frozenset()):
            with _connect(sock_path) as conn:
                chunk = b"x" * 65536
                sent = 0
                try:
                    while sent <= skill_proxy_module.MAX_REQUEST_BYTES + len(chunk):
                        conn.sendall(chunk)
                        sent += len(chunk)
                except OSError:
                    pass
                reply = json.loads(_read_all(conn))
        assert reply["returncode"] == 1
        assert "too large" in reply["stderr"]

    def test_a_dripping_request_runs_out_of_time(self, sock_path, monkeypatch):
        monkeypatch.setattr(skill_proxy_module, "REQUEST_DEADLINE_SECONDS", 0.3)
        with SkillProxy(sock_path, {}, {}, allowed_skills=frozenset()):
            with _connect(sock_path) as conn:
                started = time.monotonic()
                try:
                    while time.monotonic() - started < 3:
                        conn.sendall(b"x")
                        time.sleep(0.05)
                except OSError:
                    pass
                elapsed = time.monotonic() - started
        assert elapsed < 2

    def test_output_past_the_cap_ends_the_call(self, sock_path, fake_skill):
        fake_skill("while :; do echo xxxxxxxxxxxxxxxx; done")
        with SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin:/bin"},
            allowed_skills=frozenset({"fake"}), max_output_bytes=8192,
        ):
            reply = _skill_call(sock_path)
        assert reply["returncode"] == 1
        assert reply["stdout"] == ""
        assert "8192" in reply["stderr"]

    def test_concurrent_skills_queue_at_the_limit(self, sock_path, fake_skill, tmp_path):
        running = tmp_path / "running"
        running.mkdir()
        fake_skill(
            f'touch "{running}/$$"; ls "{running}" | wc -l; sleep 0.3; rm "{running}/$$"'
        )
        replies = []
        with SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin:/bin"},
            allowed_skills=frozenset({"fake"}), max_concurrent_skills=2,
        ):
            threads = [
                threading.Thread(target=lambda: replies.append(_skill_call(sock_path)))
                for _ in range(6)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        assert len(replies) == 6
        assert all(reply["returncode"] == 0 for reply in replies)
        assert max(int(reply["stdout"]) for reply in replies) <= 2

    def test_a_queued_call_gives_up_once_its_queue_budget_is_spent(
        self, sock_path, fake_skill, monkeypatch,
    ):
        fake_skill("sleep 2")
        monkeypatch.setattr(
            skill_proxy_module.SkillProxy, "_queue_budget", lambda self, timeout: 0.1,
        )
        replies = []
        with SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin:/bin"},
            allowed_skills=frozenset({"fake"}), max_concurrent_skills=1, trusted_roots={os.getpid()},
        ) as proxy:
            first = threading.Thread(target=lambda: replies.append(_skill_call(sock_path)))
            first.start()
            assert _wait_until(lambda: proxy._live_children)
            queued = _skill_call(sock_path)
            first.join(10)
        assert queued["returncode"] == 1
        assert "busy" in queued["stderr"]

    def test_connections_past_the_limit_get_a_skill_shaped_refusal(
        self, sock_path, fake_skill,
    ):
        fake_skill("sleep 2")
        with SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin:/bin"},
            allowed_skills=frozenset({"fake"}), max_connections=1,
        ) as proxy:
            first = threading.Thread(target=_skill_call, args=(sock_path,))
            first.start()
            assert _wait_until(lambda: proxy._live_children)
            reply = _skill_call(sock_path)
            first.join(10)
        assert reply["returncode"] == 1
        assert "busy" in reply["stderr"]

    def test_the_client_prints_the_refusal_as_one_line(self, sock_path, fake_skill):
        fake_skill("sleep 60")
        env = {"PATH": "/usr/bin:/bin", "ISTOTA_SKILL_PROXY_SOCK": str(sock_path)}
        with SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin:/bin"},
            allowed_skills=frozenset({"fake"}), max_connections=1,
        ) as proxy:
            caller = threading.Thread(
                target=lambda: _read_all(_send_only(sock_path)), daemon=True,
            )
            caller.start()
            assert _wait_until(lambda: proxy._live_children)
            result = subprocess.run(
                [REAL_PYTHON, "-m", "istota.skill_client", "fake"],
                env=env, capture_output=True, text=True, timeout=30,
            )
        assert result.returncode == 1
        assert "busy" in result.stderr or "closed the connection" in result.stderr
        assert "Traceback" not in result.stderr

    def test_stop_kills_a_running_skill_and_its_descendants(
        self, sock_path, fake_skill, tmp_path,
    ):
        marker = tmp_path / "child.pid"
        fake_skill(f"sleep 60 & echo $! > {marker}; wait")
        proxy = SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin:/bin"},
            allowed_skills=frozenset({"fake"}), trusted_roots={os.getpid()},
        )
        proxy.start()
        caller = threading.Thread(
            target=lambda: _read_all(_send_only(sock_path)), daemon=True,
        )
        caller.start()
        assert _wait_until(marker.exists)
        assert _wait_until(lambda: marker.read_text().strip() != "")
        child = int(marker.read_text())
        assert _alive(child)

        proxy.stop()

        assert _wait_until(lambda: not _alive(child))
        assert _wait_until(lambda: not proxy._live_children)

    def test_no_skill_starts_after_stop(self, sock_path, fake_skill, tmp_path):
        marker = tmp_path / "ran"
        fake_skill(f"touch {marker}; sleep 1")
        proxy = SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin:/bin"},
            allowed_skills=frozenset({"fake"}), max_concurrent_skills=1, trusted_roots={os.getpid()},
        )
        proxy.start()
        holder = threading.Thread(target=lambda: _read_all(_send_only(sock_path)), daemon=True)
        holder.start()
        assert _wait_until(marker.exists)
        marker.unlink()
        queued = threading.Thread(target=lambda: _read_all(_send_only(sock_path)), daemon=True)
        queued.start()
        assert _wait_until(lambda: len(proxy._server._live) == 2)
        proxy.stop()
        time.sleep(1.2)
        assert not marker.exists()

    def test_a_queued_call_whose_client_left_never_runs(
        self, sock_path, fake_skill, tmp_path,
    ):
        log = tmp_path / "runs"
        fake_skill(f'echo "$$" >> {log}; sleep 0.5')
        with SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin:/bin"},
            allowed_skills=frozenset({"fake"}), max_concurrent_skills=1,
            trusted_roots={os.getpid()},
        ) as proxy:
            holder = threading.Thread(target=_skill_call, args=(sock_path,))
            holder.start()
            assert _wait_until(log.exists)
            abandoned = _send_only(sock_path)
            assert _wait_until(lambda: len(proxy._server._live) == 2)
            abandoned.close()
            holder.join(10)
            assert _skill_call(sock_path)["returncode"] == 0
        assert len(log.read_text().splitlines()) == 2

    def test_a_burst_of_forty_is_all_served_through_the_slots(
        self, sock_path, fake_skill,
    ):
        """#876's burst, past the concurrency limit and through the queue."""
        fake_skill("sleep 0.05; echo ok")
        replies = []
        with SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin:/bin"},
            allowed_skills=frozenset({"fake"}), trusted_roots={os.getpid()},
        ):
            threads = [
                threading.Thread(target=lambda: replies.append(_skill_call(sock_path)))
                for _ in range(40)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
        assert [reply["returncode"] for reply in replies] == [0] * 40

    def test_a_skill_joins_the_task_cgroup(self, sock_path, fake_skill, tmp_path):
        """On a fixture directory: the child's own write of `0` is the placement,
        and it lands in the file a real cgroup's `cgroup.procs` would be."""
        cgroup = tmp_path / "task-1-0"
        cgroup.mkdir()
        procs = cgroup / "cgroup.procs"
        procs.write_text("")
        fake_skill("exit 0")
        with SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin:/bin"},
            allowed_skills=frozenset({"fake"}), trusted_roots={os.getpid()},
        ) as proxy:
            proxy.task_cgroup = cgroup
            assert _skill_call(sock_path)["returncode"] == 0
        assert procs.read_text().startswith("0")


@pytest.mark.linux
def test_a_skill_is_really_in_the_task_cgroup_and_dies_with_it(
    sock_path, fake_skill, tmp_path,
):
    """A real delegated cgroup: membership, and `cgroup.kill` at release reaching
    a descendant that left the skill's process group."""
    from istota.sandbox import cgroup as task_cgroup

    env_root = os.environ.get("ISTOTA_TEST_CGROUP_ROOT")
    if not env_root:
        pytest.skip("ISTOTA_TEST_CGROUP_ROOT unset: scripts/test-linux.sh builds the subtree")
    root = Path(env_root)
    path = task_cgroup.create(
        890, task_cgroup.CgroupLimits(), attempt=os.getpid(), root=root,
    )
    assert path is not None
    marker = tmp_path / "escaped.pid"
    fake_skill(
        f"setsid sleep 60 > /dev/null 2>&1 < /dev/null & echo $! > {marker}; "
        "cat /proc/self/cgroup"
    )
    try:
        with SkillProxy(
            sock_path, {}, {"PATH": "/usr/bin:/bin"},
            allowed_skills=frozenset({"fake"}), trusted_roots={os.getpid()},
        ) as proxy:
            proxy.task_cgroup = path
            reply = _skill_call(sock_path)
        assert reply["returncode"] == 0, reply
        assert path.name in reply["stdout"]
        escaped = int(marker.read_text())
        assert _alive(escaped)
    finally:
        task_cgroup.destroy(path)
    assert _wait_until(lambda: not _alive(escaped))


def _send_only(path: Path) -> socket.socket:
    conn = _connect(path)
    conn.settimeout(30)
    conn.sendall(json.dumps({"skill": "fake", "args": []}).encode() + b"\n")
    return conn


class TestNetworkProxyLimits:
    def test_a_request_past_the_head_cap_is_refused(self, sock_path):
        with NetworkProxy(sock_path, {"example.com:443"}, trusted_roots={os.getpid()}):
            with _connect(sock_path) as conn:
                conn.sendall(b"x" * (network_proxy_module.MAX_REQUEST_HEAD_BYTES + 1024))
                reply = _read_all(conn)
        assert reply.startswith(b"HTTP/1.1 431")

    def test_headers_past_the_head_cap_are_refused(self, sock_path):
        with NetworkProxy(sock_path, {"example.com:443"}, trusted_roots={os.getpid()}):
            with _connect(sock_path) as conn:
                conn.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n")
                conn.sendall(b"X: " + b"y" * (network_proxy_module.MAX_REQUEST_HEAD_BYTES + 1024))
                reply = _read_all(conn)
        assert reply.startswith(b"HTTP/1.1 431")

    def test_a_dripping_request_is_cut_off_at_the_deadline(self, sock_path, monkeypatch):
        monkeypatch.setattr(network_proxy_module, "REQUEST_DEADLINE_SECONDS", 0.3)
        with NetworkProxy(sock_path, {"example.com:443"}, trusted_roots={os.getpid()}):
            with _connect(sock_path) as conn:
                started = time.monotonic()
                try:
                    for _ in range(100):
                        conn.sendall(b"x")
                        time.sleep(0.05)
                except OSError:
                    pass
                reply = _read_all(conn)
                elapsed = time.monotonic() - started
        assert elapsed < 3
        assert reply.startswith(b"HTTP/1.1 408")

    def test_a_dripping_client_hello_is_cut_off_at_the_deadline(self):
        left, right = socket.socketpair()
        left.settimeout(30)
        started = time.monotonic()
        hello = network_proxy_module.read_client_hello(
            left, deadline=time.monotonic() + 0.2,
        )
        left.close()
        right.close()
        assert hello is None
        assert time.monotonic() - started < 2

    def test_connections_past_the_limit_get_503_and_a_neighbour_is_served(self):
        sock_a = Path(tempfile.gettempdir()) / f"istota-limit-{uuid.uuid4().hex[:8]}.sock"
        sock_b = Path(tempfile.gettempdir()) / f"istota-limit-{uuid.uuid4().hex[:8]}.sock"
        held = []
        with NetworkProxy(
            sock_a, set(), trusted_roots={os.getpid()}, max_connections=4,
        ) as noisy, NetworkProxy(sock_b, set(), trusted_roots={os.getpid()}):
            try:
                held = [_connect(sock_a) for _ in range(4)]
                assert _wait_until(lambda: len(noisy._server._live) == 4)
                baseline = threading.active_count()
                for _ in range(20):
                    with _connect(sock_a) as refused:
                        assert _read_all(refused).startswith(b"HTTP/1.1 503")
                assert threading.active_count() <= baseline

                with _connect(sock_b) as neighbour:
                    neighbour.sendall(b"CONNECT blocked.example:443 HTTP/1.1\r\n\r\n")
                    assert _read_all(neighbour).startswith(b"HTTP/1.1 403")
            finally:
                for conn in held:
                    conn.close()

    def test_a_finished_tunnel_leaves_no_thread_behind_its_slot(self, sock_path, monkeypatch):
        """The upstream closes and the client holds on. Linux's `close` does not
        wake the thread still in `recv` on the client, so without the shutdown
        each round adds threads while the connection count reads empty. macOS
        wakes it either way, so this one discriminates on the Linux tier."""
        real_join = threading.Thread.join

        def short_join(self, timeout=None):
            # `_bridge`'s five-second grace, so the rounds below take seconds.
            real_join(self, None if timeout is None else min(timeout, 0.1))

        monkeypatch.setattr(threading.Thread, "join", short_join)
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(16)
        port = upstream.getsockname()[1]
        stop_upstream = threading.Event()

        def serve_upstream():
            upstream.settimeout(0.1)
            while not stop_upstream.is_set():
                try:
                    accepted, _ = upstream.accept()
                except OSError:
                    continue
                accepted.settimeout(5)
                try:
                    accepted.recv(4096)
                finally:
                    accepted.close()

        server_thread = threading.Thread(target=serve_upstream, daemon=True)
        server_thread.start()
        held = []
        try:
            with NetworkProxy(
                sock_path, {f"127.0.0.1:{port}"}, trusted_roots={os.getpid()},
                private_hosts={"127.0.0.1"}, max_connections=4,
            ) as proxy:
                baseline = threading.active_count()
                for _ in range(3):
                    for _ in range(4):
                        client = _connect(sock_path)
                        held.append(client)
                        client.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
                        assert client.recv(4096).startswith(b"HTTP/1.1 200")
                        client.sendall(_client_hello_without_sni())
                    assert _wait_until(lambda: not proxy._server._live)
                    assert _wait_until(lambda: threading.active_count() <= baseline)
        finally:
            stop_upstream.set()
            for client in held:
                client.close()
            server_thread.join(2)
            upstream.close()

    def test_stop_ends_a_tunnel_whose_upstream_is_silent(self, sock_path):
        """The upstream half has no timeout, so nothing else wakes it."""
        upstream_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream_server.bind(("127.0.0.1", 0))
        upstream_server.listen(1)
        port = upstream_server.getsockname()[1]
        proxy = NetworkProxy(
            sock_path, {f"127.0.0.1:{port}"}, trusted_roots={os.getpid()},
            private_hosts={"127.0.0.1"},
        )
        proxy.start()
        client = _connect(sock_path)
        accepted = None
        try:
            client.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
            assert client.recv(4096).startswith(b"HTTP/1.1 200")
            client.sendall(_client_hello_without_sni())
            upstream_server.settimeout(5)
            accepted, _ = upstream_server.accept()
            accepted.settimeout(5)
            assert accepted.recv(4096)
            assert len(proxy._upstreams) == 1

            proxy.stop()

            assert _wait_until(lambda: not proxy._server._live)
            assert _wait_until(lambda: not proxy._upstreams)
            assert accepted.recv(4096) == b""
        finally:
            client.close()
            if accepted is not None:
                accepted.close()
            upstream_server.close()


def _client_hello_without_sni() -> bytes:
    """A minimal ClientHello: an IP-literal CONNECT may carry no server name."""
    body = (
        b"\x03\x03" + b"\x00" * 32  # legacy_version, random
        + b"\x00"  # session id
        + b"\x00\x02\x13\x01"  # cipher suites
        + b"\x01\x00"  # compression methods
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake
