"""The devbox credential socket refuses same-uid host processes."""

import asyncio
import json
import os
import subprocess
from functools import partial
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from istota.devbox_proxy import serve
@pytest.fixture
def sock_path():
    with tempfile.TemporaryDirectory(prefix="dbpeer_", dir="/tmp") as directory:
        yield Path(directory) / "s.sock"


@pytest.mark.parametrize("payload", [
    {"action": "ping"},
    {"action": "forge_token", "provider": "github"},
    {"action": "git_credential", "op": "get", "input": "host=github.com\n"},
])
async def test_same_uid_host_process_is_refused(sock_path, payload):
    config = SimpleNamespace(developer=SimpleNamespace(github_token="victim-token"))
    server = asyncio.create_task(serve("alice", config, socket_path=sock_path))
    try:
        for _ in range(100):
            if sock_path.exists():
                break
            await asyncio.sleep(0.01)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import socket,sys; s=socket.socket(socket.AF_UNIX); "
            "s.settimeout(5); s.connect(sys.argv[1]); "
            "s.sendall(sys.argv[2].encode()+b'\\n'); "
            "print(s.makefile().readline())",
            str(sock_path), json.dumps(payload),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
        assert proc.returncode == 0, err.decode()
        reply = json.loads(out)
        assert reply["ok"] is False
        assert reply["error"] == "forbidden"
        assert "victim-token" not in out.decode()
    finally:
        server.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server


@pytest.fixture
def docker_peer(tmp_path, monkeypatch):
    from istota import devbox_peer
    container_id = "a" * 64
    root = f"/system.slice/docker-{container_id}.scope"
    info = {"id": container_id, "pid": 987654, "running": True, "user": "alice"}
    def cgroup(pid, path):
        directory = tmp_path / str(pid)
        directory.mkdir(exist_ok=True)
        (directory / "cgroup").write_text(f"0::{path}\n")
    cgroup(info["pid"], root)
    cgroup(os.getpid(), root)
    calls = []
    def inspect(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(info).encode(), b"")
    monkeypatch.setattr(devbox_peer, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(devbox_peer.subprocess, "run", inspect)
    return info, root, cgroup, calls


def check_peer(tmp_path):
    from istota import devbox_peer
    from istota.sandbox import peer_process
    pid = os.getpid()
    return devbox_peer.peer_in_devbox(
        pid, peer_process.start_time(pid), user_id="alice",
        container_name="custom-alice", docker_cli="/usr/bin/docker",
        proc_root=tmp_path,
    )


@pytest.mark.parametrize("suffix", ["", "/build"])
def test_container_and_nested_cgroup_are_allowed(tmp_path, docker_peer, suffix):
    _, root, cgroup, calls = docker_peer
    cgroup(os.getpid(), root + suffix)
    assert check_peer(tmp_path)
    assert calls[0][-2:] == ["--", "custom-alice"]


@pytest.mark.parametrize("change", ["sibling", "lookalike", "owner", "stopped", "host", "gone"])
def test_wrong_or_missing_container_identity_is_refused(tmp_path, docker_peer, change):
    info, root, cgroup, _ = docker_peer
    if change == "sibling":
        cgroup(os.getpid(), root.replace("a" * 64, "b" * 64))
    elif change == "lookalike":
        cgroup(os.getpid(), "/other-user" + root)
    elif change == "owner":
        info["user"] = "bob"
    elif change == "stopped":
        info["running"] = False
    elif change == "host":
        cgroup(info["pid"], "/")
        cgroup(os.getpid(), "/")
    else:
        (tmp_path / str(info["pid"]) / "cgroup").unlink()
    assert not check_peer(tmp_path)


def test_container_recreation_does_not_authorize_old_cgroup(tmp_path, docker_peer):
    info, _, cgroup, _ = docker_peer
    assert check_peer(tmp_path)
    info["id"] = "b" * 64
    cgroup(info["pid"], "/docker/" + info["id"])
    assert not check_peer(tmp_path)
    cgroup(os.getpid(), "/docker/" + info["id"])
    assert check_peer(tmp_path)


def test_recycled_peer_pid_is_refused(tmp_path, docker_peer):
    from istota import devbox_peer
    from istota.sandbox import peer_process
    assert not devbox_peer.peer_in_devbox(
        os.getpid(), peer_process.start_time(os.getpid()) - 1,
        user_id="alice", container_name="custom-alice",
        docker_cli="/usr/bin/docker", proc_root=tmp_path,
    )


@pytest.mark.parametrize("failure", ["missing", "timeout", "malformed"])
def test_docker_failure_is_refused(tmp_path, docker_peer, monkeypatch, failure):
    from istota import devbox_peer
    def inspect(argv, **kwargs):
        if failure == "missing":
            raise FileNotFoundError()
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 5)
        return subprocess.CompletedProcess(argv, 0, b"invalid", b"")
    monkeypatch.setattr(devbox_peer.subprocess, "run", inspect)
    assert not check_peer(tmp_path)


async def test_real_socket_peer_uses_configured_container(sock_path, tmp_path, docker_peer, monkeypatch):
    from istota import devbox_peer
    from tests.test_devbox_proxy import _client_round_trip
    _, _, _, calls = docker_peer
    monkeypatch.setattr(
        "istota.devbox_proxy.peer_in_devbox",
        partial(devbox_peer.peer_in_devbox, proc_root=tmp_path),
    )
    config = SimpleNamespace(
        developer=SimpleNamespace(github_token="container-token"),
        devbox=SimpleNamespace(container_prefix="custom-", docker_cli="/usr/bin/docker"),
    )
    server = asyncio.create_task(serve("alice", config, socket_path=sock_path))
    try:
        for _ in range(100):
            if sock_path.exists():
                break
            await asyncio.sleep(0.01)
        reply = await _client_round_trip(sock_path, json.dumps({
            "action": "forge_token", "provider": "github", "user_id": "bob",
        }) + "\n")
        assert reply["token"] == "container-token"
        assert calls[0][-1] == "custom-alice"
    finally:
        server.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server
