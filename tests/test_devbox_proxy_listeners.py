"""The credential proxy's identity is the listener, fixed when it is bound.

The one deployment shape runs the proxy inside the istota container, which has
no Docker socket and no host PID namespace, so the `docker inspect` plus cgroup
comparison the bare-metal proxy made on every connection cannot work there.
What replaces it is placement: user U's socket lives in the volume
`devbox-cred-U`, which only U's devbox and the istota container mount, and no
task sandbox binds any credential socket directory. So a listener answers as
the user it was bound for, whoever connects, and never as anyone else.

These tests connect from the test process itself, a peer in no container at
all. Under the old check every one of them was refused `forbidden`.
"""

from __future__ import annotations

import asyncio
import json
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from istota.devbox import proxy


def _config(sock_dir: Path, users: list[str], **developer) -> SimpleNamespace:
    return SimpleNamespace(
        developer=SimpleNamespace(
            devbox_proxy_enabled=True,
            devbox_proxy_socket_dir=str(sock_dir),
            devbox_proxy_audit_log="",
            github_token=developer.get("github_token", "gh-token"),
            gitlab_token=developer.get("gitlab_token", ""),
            github_url="https://github.com",
            gitlab_url="https://gitlab.com",
        ),
        devbox=SimpleNamespace(enabled=True, users=list(users)),
    )


@pytest.fixture
def sock_dir():
    # Under /tmp, not pytest's tmp_path: a macOS tmp_path is long enough to
    # push a socket path past sun_path's 104 bytes.
    with tempfile.TemporaryDirectory(prefix="dbcred_", dir="/tmp") as directory:
        yield Path(directory)


async def _ask(path: Path, payload: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        return json.loads(line)
    finally:
        writer.close()


async def _wait_for(path: Path) -> None:
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{path} never appeared")


class TestTheSingleUserListener:
    """The `--user` entry point the bare-metal unit still runs."""

    async def test_a_peer_outside_any_container_is_served_as_the_listeners_user(
        self, sock_dir
    ):
        path = sock_dir / "alice" / "sock"
        server = asyncio.create_task(
            proxy.serve("alice", _config(sock_dir, ["alice"]), socket_path=path)
        )
        try:
            await _wait_for(path)
            reply = await _ask(path, {"action": "ping"})
            assert reply.get("ok") is True, reply
            assert reply["user_id"] == "alice"
        finally:
            server.cancel()
            with pytest.raises(asyncio.CancelledError):
                await server

    async def test_a_claimed_user_in_the_request_changes_nothing(self, sock_dir):
        path = sock_dir / "alice" / "sock"
        server = asyncio.create_task(
            proxy.serve("alice", _config(sock_dir, ["alice"]), socket_path=path)
        )
        try:
            await _wait_for(path)
            reply = await _ask(path, {"action": "ping", "user_id": "bob"})
            assert reply["user_id"] == "alice"
        finally:
            server.cancel()
            with pytest.raises(asyncio.CancelledError):
                await server


class TestOneListenerPerUser:
    """`start_listeners`: what the daemon runs in the istota container."""

    async def test_each_configured_user_gets_a_socket_that_answers_as_them(
        self, sock_dir
    ):
        listeners = await proxy.start_listeners(_config(sock_dir, ["alice", "bob"]))
        try:
            for user in ("alice", "bob"):
                path = sock_dir / user / "sock"
                assert path.exists(), f"no listener for {user}"
                reply = await _ask(path, {"action": "ping"})
                assert reply["user_id"] == user, reply
        finally:
            await proxy.stop_listeners(listeners)

    async def test_a_user_not_listed_gets_no_socket(self, sock_dir):
        listeners = await proxy.start_listeners(_config(sock_dir, ["alice"]))
        try:
            assert (sock_dir / "alice" / "sock").exists()
            assert not (sock_dir / "bob").exists()
        finally:
            await proxy.stop_listeners(listeners)

    async def test_the_socket_is_not_world_reachable(self, sock_dir):
        listeners = await proxy.start_listeners(_config(sock_dir, ["alice"]))
        try:
            mode = stat.S_IMODE((sock_dir / "alice" / "sock").stat().st_mode)
            assert mode & 0o007 == 0, oct(mode)
            parent = stat.S_IMODE((sock_dir / "alice").stat().st_mode)
            assert parent & 0o007 == 0, oct(parent)
        finally:
            await proxy.stop_listeners(listeners)

    async def test_stopping_removes_every_socket(self, sock_dir):
        listeners = await proxy.start_listeners(_config(sock_dir, ["alice", "bob"]))
        await proxy.stop_listeners(listeners)
        assert not (sock_dir / "alice" / "sock").exists()
        assert not (sock_dir / "bob" / "sock").exists()

    async def test_a_user_id_that_would_leave_the_socket_directory_is_refused(
        self, sock_dir
    ):
        listeners = await proxy.start_listeners(
            _config(sock_dir, ["alice", "../escape", ""])
        )
        try:
            assert (sock_dir / "alice" / "sock").exists()
            assert not (sock_dir.parent / "escape").exists()
            assert [listener.user_id for listener in listeners] == ["alice"]
        finally:
            await proxy.stop_listeners(listeners)

    async def test_the_forge_token_comes_from_the_users_listener(self, sock_dir):
        listeners = await proxy.start_listeners(
            _config(sock_dir, ["alice"], github_token="gh-alice")
        )
        try:
            reply = await _ask(
                sock_dir / "alice" / "sock",
                {"action": "forge_token", "provider": "github"},
            )
            assert reply["ok"] is True
            assert reply["token"] == "gh-alice"
        finally:
            await proxy.stop_listeners(listeners)


class TestTheDaemonDecidesWhetherToRunThem:
    def test_no_listeners_without_users(self, sock_dir):
        assert proxy.wanted_users(_config(sock_dir, [])) == []

    def test_no_listeners_with_the_devbox_off(self, sock_dir):
        config = _config(sock_dir, ["alice"])
        config.devbox.enabled = False
        assert proxy.wanted_users(config) == []

    def test_no_listeners_with_the_proxy_off(self, sock_dir):
        config = _config(sock_dir, ["alice"])
        config.developer.devbox_proxy_enabled = False
        assert proxy.wanted_users(config) == []

    def test_the_listed_users_otherwise(self, sock_dir):
        assert proxy.wanted_users(_config(sock_dir, ["alice", "bob"])) == ["alice", "bob"]
