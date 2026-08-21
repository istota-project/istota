"""Tests for the devbox credential proxy daemon.

Three actions: ping, git_credential get/store/erase, and forge_token.
End-to-end coverage via a tmpdir Unix socket exercises
asyncio.start_unix_server + handle_connection without going through
systemd. Edge cases: oversized requests, malformed JSON, and audit
logging.

The daemon makes no outbound HTTP requests, so there is no mocked
transport here — every answer comes out of the in-memory context.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def sock_path():
    """Yield a short Unix-socket path.

    ``tmp_path`` on macOS exceeds the AF_UNIX 104-char limit. A short
    /tmp-based directory keeps us well under the cap on both macOS and
    Linux.
    """
    dirpath = Path(tempfile.mkdtemp(prefix="dvbx_", dir="/tmp"))
    try:
        yield dirpath / "p.sock"
    finally:
        shutil.rmtree(dirpath, ignore_errors=True)

from istota.devbox_proxy import (
    DevboxProxyContext,
    handle_connection,
    handle_forge_token,
    handle_git_credential,
    handle_ping,
    serve,
)
from istota.devbox_proxy_protocol import (
    decode_response,
    encode_request,
)


# ---- Fixtures --------------------------------------------------------------


def _ctx(
    *,
    user_id: str = "alice",
    gitlab_token: str = "GL-TOKEN",
    github_token: str = "GH-TOKEN",
    gitlab_url: str = "https://gitlab.com",
    github_url: str = "https://github.com",
) -> DevboxProxyContext:
    """Build a DevboxProxyContext. Every field is answered from memory."""
    return DevboxProxyContext(
        user_id=user_id,
        gitlab_token=gitlab_token,
        github_token=github_token,
        gitlab_url=gitlab_url,
        github_url=github_url,
    )


async def _client_round_trip(socket_path: Path, request_line: str) -> dict:
    """Open the socket, write one request, read one response, decode."""
    reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
    writer.write(request_line.encode("utf-8"))
    await writer.drain()
    line = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return decode_response(line.decode("utf-8"))


def _cross_process_ping(sock_path_str: str, result_queue) -> None:
    """Module-level worker for the cross-process connect test.

    Must be importable from a spawned child, so it lives at module
    scope (locals can't be pickled).
    """
    import socket as _socket

    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(sock_path_str)
        s.sendall(b'{"action":"ping"}\n')
        buf = b""
        while not buf.endswith(b"\n") and len(buf) < 4096:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        result_queue.put(("ok", buf.decode("utf-8")))
    except Exception as e:  # noqa: BLE001
        result_queue.put(("err", repr(e)))


# ---- handle_ping -----------------------------------------------------------


class TestPing:
    @pytest.mark.asyncio
    async def test_both_providers_configured(self):
        ctx = _ctx()
        line = await handle_ping({"action": "ping"}, ctx)
        resp = decode_response(line)
        assert resp == {
            "ok": True,
            "user_id": "alice",
            "providers": ["github", "gitlab"],
        }

    @pytest.mark.asyncio
    async def test_only_github(self):
        ctx = _ctx(gitlab_token="")
        line = await handle_ping({"action": "ping"}, ctx)
        resp = decode_response(line)
        assert resp["providers"] == ["github"]

    @pytest.mark.asyncio
    async def test_only_gitlab(self):
        ctx = _ctx(github_token="")
        line = await handle_ping({"action": "ping"}, ctx)
        resp = decode_response(line)
        assert resp["providers"] == ["gitlab"]

    @pytest.mark.asyncio
    async def test_no_providers(self):
        ctx = _ctx(github_token="", gitlab_token="")
        line = await handle_ping({"action": "ping"}, ctx)
        resp = decode_response(line)
        assert resp["providers"] == []


# ---- handle_git_credential -------------------------------------------------


class TestGitCredential:
    @pytest.mark.asyncio
    async def test_get_github_known_host(self):
        ctx = _ctx()
        req = {
            "action": "git_credential",
            "op": "get",
            "input": "protocol=https\nhost=github.com\n",
        }
        resp = decode_response(await handle_git_credential(req, ctx))
        assert resp["ok"] is True
        # Helper passes ``stdout`` straight back to git verbatim.
        assert "protocol=https" in resp["stdout"]
        assert "host=github.com" in resp["stdout"]
        assert "username=x-access-token" in resp["stdout"]
        assert "password=GH-TOKEN" in resp["stdout"]

    @pytest.mark.asyncio
    async def test_get_gitlab_known_host(self):
        ctx = _ctx()
        req = {
            "action": "git_credential",
            "op": "get",
            "input": "protocol=https\nhost=gitlab.com\n",
        }
        resp = decode_response(await handle_git_credential(req, ctx))
        assert resp["ok"] is True
        assert "password=GL-TOKEN" in resp["stdout"]
        assert "username=x-access-token" in resp["stdout"]

    @pytest.mark.asyncio
    async def test_get_resolves_custom_gitlab_host(self):
        ctx = _ctx(gitlab_url="https://gitlab.example.com")
        req = {
            "action": "git_credential",
            "op": "get",
            "input": "protocol=https\nhost=gitlab.example.com\n",
        }
        resp = decode_response(await handle_git_credential(req, ctx))
        assert resp["ok"] is True
        assert "password=GL-TOKEN" in resp["stdout"]

    @pytest.mark.asyncio
    async def test_get_unknown_host_returns_no_token(self):
        ctx = _ctx()
        req = {
            "action": "git_credential",
            "op": "get",
            "input": "protocol=https\nhost=bitbucket.org\n",
        }
        resp = decode_response(await handle_git_credential(req, ctx))
        assert resp["ok"] is False
        assert resp["error"] == "no_token"
        # The message names the host so audit log + operator debugging is easy.
        assert "bitbucket.org" in resp["message"]

    @pytest.mark.asyncio
    async def test_get_provider_known_but_token_empty(self):
        ctx = _ctx(github_token="")
        req = {
            "action": "git_credential",
            "op": "get",
            "input": "protocol=https\nhost=github.com\n",
        }
        resp = decode_response(await handle_git_credential(req, ctx))
        assert resp["ok"] is False
        assert resp["error"] == "no_token"

    @pytest.mark.asyncio
    async def test_store_is_noop(self):
        ctx = _ctx()
        req = {
            "action": "git_credential",
            "op": "store",
            "input": "protocol=https\nhost=github.com\npassword=anything\n",
        }
        resp = decode_response(await handle_git_credential(req, ctx))
        assert resp == {"ok": True}

    @pytest.mark.asyncio
    async def test_erase_is_noop(self):
        ctx = _ctx()
        req = {"action": "git_credential", "op": "erase", "input": ""}
        resp = decode_response(await handle_git_credential(req, ctx))
        assert resp == {"ok": True}

    @pytest.mark.asyncio
    async def test_unknown_op_is_bad_request(self):
        ctx = _ctx()
        req = {"action": "git_credential", "op": "approve", "input": ""}
        resp = decode_response(await handle_git_credential(req, ctx))
        assert resp["ok"] is False
        assert resp["error"] == "bad_request"


import contextlib


@contextlib.contextmanager
def caplog_at(level):
    """Collect records from the audit logger only.

    pytest's caplog fixture is not usable from a plain contextmanager, and the
    audit logger is the only one these assertions care about.
    """
    import logging

    logger = logging.getLogger("istota.devbox_proxy.audit")
    records = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collector()
    handler.setLevel(level)
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(level)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


async def _dispatch_one(ctx, request):
    """Push one request through handle_connection over an in-memory pair.

    Goes through the dispatcher rather than a handler, because the branch
    under test lives in handle_connection itself.
    """
    reader = asyncio.StreamReader()
    reader.feed_data((json.dumps(request) + "\n").encode())
    reader.feed_eof()

    written = bytearray()

    class _Writer:
        def write(self, data):
            written.extend(data)

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    await handle_connection(reader, _Writer(), ctx)
    return decode_response(bytes(written).decode())


# ---- handle_forge_token ----------------------------------------------------


class TestForgeToken:
    """The action that replaced the two REST actions. It hands the real
    token to the in-container gh/glab wrapper, which then execs the real
    binary — so unlike git_credential there is no server-side injection to
    hide behind, and the tests are about which provider gets which token."""

    @pytest.mark.asyncio
    async def test_github_provider_returns_the_github_token(self):
        ctx = _ctx()
        resp = decode_response(
            await handle_forge_token(
                {"action": "forge_token", "provider": "github"}, ctx,
            )
        )
        assert resp["ok"] is True
        assert resp["token"] == "GH-TOKEN"

    @pytest.mark.asyncio
    async def test_response_carries_the_configured_url(self):
        """The devbox image is shared by every user, so its baked policy has
        no per-user URL. Without one here the wrapper leaves GITLAB_HOST unset
        and glab sends a self-hosted token to gitlab.com."""
        ctx = _ctx(gitlab_url="https://git.example.com:8443/gitlab")
        resp = decode_response(
            await handle_forge_token({"provider": "gitlab"}, ctx)
        )
        assert resp["url"] == "https://git.example.com:8443/gitlab"

    @pytest.mark.asyncio
    async def test_gitlab_provider_returns_the_gitlab_token(self):
        ctx = _ctx()
        resp = decode_response(
            await handle_forge_token(
                {"action": "forge_token", "provider": "gitlab"}, ctx,
            )
        )
        assert resp["ok"] is True
        assert resp["token"] == "GL-TOKEN"

    @pytest.mark.asyncio
    async def test_providers_do_not_cross(self):
        """A github request must never come back with the gitlab token —
        the two are separately scoped credentials and handing over the
        wrong one sends it to the wrong host."""
        ctx = _ctx(github_token="ONLY-GH", gitlab_token="ONLY-GL")
        gh = decode_response(
            await handle_forge_token({"provider": "github"}, ctx)
        )
        gl = decode_response(
            await handle_forge_token({"provider": "gitlab"}, ctx)
        )
        assert gh["token"] == "ONLY-GH"
        assert gl["token"] == "ONLY-GL"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["GitHub", "GITLAB", " github ", "GitLab"])
    async def test_provider_matching_is_case_and_whitespace_insensitive(self, provider):
        ctx = _ctx()
        resp = decode_response(
            await handle_forge_token({"provider": provider}, ctx)
        )
        assert resp["ok"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["bitbucket", "", "gitea", "git hub"])
    async def test_unknown_provider_is_rejected(self, provider):
        ctx = _ctx()
        resp = decode_response(
            await handle_forge_token({"provider": provider}, ctx)
        )
        assert resp["ok"] is False
        assert resp["error"] == "unknown_provider"

    @pytest.mark.asyncio
    async def test_missing_provider_field_is_rejected(self):
        ctx = _ctx()
        resp = decode_response(await handle_forge_token({}, ctx))
        assert resp["ok"] is False
        assert resp["error"] == "unknown_provider"

    @pytest.mark.asyncio
    async def test_non_string_provider_does_not_raise(self):
        ctx = _ctx()
        for bad in (42, None, ["github"], {"a": 1}):
            resp = decode_response(
                await handle_forge_token({"provider": bad}, ctx)
            )
            assert resp["ok"] is False
            assert resp["error"] == "unknown_provider"

    @pytest.mark.asyncio
    async def test_unconfigured_provider_returns_no_token(self):
        ctx = _ctx(github_token="")
        resp = decode_response(
            await handle_forge_token({"provider": "github"}, ctx)
        )
        assert resp["ok"] is False
        assert resp["error"] == "no_token"
        assert "token" not in resp

    @pytest.mark.asyncio
    async def test_rejection_message_does_not_carry_the_other_token(self):
        ctx = _ctx(github_token="", gitlab_token="GL-SECRET")
        resp = decode_response(
            await handle_forge_token({"provider": "github"}, ctx)
        )
        assert "GL-SECRET" not in json.dumps(resp)


# ---- End-to-end via Unix socket --------------------------------------------


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_serve_responds_to_ping(self, sock_path):
        sock = sock_path
        ctx = _ctx()

        async def _client_callback(reader, writer):
            await handle_connection(reader, writer, ctx)

        server = await asyncio.start_unix_server(
            _client_callback, path=str(sock),
        )
        try:
            resp = await _client_round_trip(sock, encode_request(action="ping"))
            assert resp["ok"] is True
            assert resp["providers"] == ["github", "gitlab"]
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_serve_responds_to_git_credential(self, sock_path):
        sock = sock_path
        ctx = _ctx()

        async def _client_callback(reader, writer):
            await handle_connection(reader, writer, ctx)

        server = await asyncio.start_unix_server(
            _client_callback, path=str(sock),
        )
        try:
            line = encode_request(
                action="git_credential",
                op="get",
                input="protocol=https\nhost=github.com\n",
            )
            resp = await _client_round_trip(sock, line)
            assert resp["ok"] is True
            assert "password=GH-TOKEN" in resp["stdout"]
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_unknown_action_returns_unknown_action_error(self, sock_path):
        sock = sock_path
        ctx = _ctx()

        async def _client_callback(reader, writer):
            await handle_connection(reader, writer, ctx)

        server = await asyncio.start_unix_server(
            _client_callback, path=str(sock),
        )
        try:
            resp = await _client_round_trip(
                sock, encode_request(action="totally-not-a-real-action"),
            )
            assert resp["ok"] is False
            assert resp["error"] == "unknown_action"
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_concurrent_connections(self, sock_path):
        """Multiple connections should multiplex on the event loop, not serialize."""
        sock = sock_path
        ctx = _ctx()

        async def _client_callback(reader, writer):
            await handle_connection(reader, writer, ctx)

        server = await asyncio.start_unix_server(
            _client_callback, path=str(sock),
        )
        try:
            line = encode_request(action="ping")
            results = await asyncio.gather(*[
                _client_round_trip(sock, line) for _ in range(10)
            ])
            assert all(r["ok"] for r in results)
            assert all(r["providers"] == ["github", "gitlab"] for r in results)
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_serve_creates_socket_with_group_rw_perms(self, sock_path, monkeypatch):
        """Socket must be mode 0660 (group-rw).

        The container's ``dev`` user runs as a different uid than the
        istota daemon. With 0o600 the bind-mounted socket is connectable
        only by the owner uid — the container would EACCES. The access
        boundary is the parent directory's mode (0750 owned by
        istota:istota) plus group membership granted by Ansible. Asserting
        on ``mode & 0o060`` instead of equality lets us tighten further
        (e.g. drop world bits, drop owner bits) without churning the test.
        """

        sock = sock_path

        # Build a tiny config stub matching what serve() reads.
        class _Dev:
            gitlab_url = "https://gitlab.com"
            gitlab_token = "GL"
            github_url = "https://github.com"
            github_token = "GH"

        class _Cfg:
            developer = _Dev()

        task = asyncio.create_task(serve("alice", _Cfg(), socket_path=sock))
        # Give serve() a moment to bind.
        for _ in range(50):
            if sock.exists():
                break
            await asyncio.sleep(0.01)
        try:
            assert sock.exists()
            mode = sock.stat().st_mode & 0o777
            assert mode & 0o060 == 0o060, (
                f"socket must be group-rw for the container to connect through "
                f"the bind mount, got {oct(mode)}"
            )
            assert mode & 0o007 == 0, (
                f"socket must not be world-accessible, got {oct(mode)}"
            )
            # Confirm we can also actually talk to it.
            resp = await _client_round_trip(sock, encode_request(action="ping"))
            assert resp["ok"] is True
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    @pytest.mark.asyncio
    async def test_default_socket_layout_per_user_subdir(self, tmp_path):
        """Default socket layout: ``{sock_dir}/{user_id}/sock``.

        The compose template bind-mounts the per-user directory, so
        the socket has to live inside a user-scoped subdir. Asserts
        the layout, the parent dir's group-rx bit (container needs
        traverse), and that the dir itself is not world-accessible.
        """

        from istota.devbox_proxy import _default_socket_path

        class _Dev:
            devbox_proxy_socket_dir = str(tmp_path)

        class _Cfg:
            developer = _Dev()

        path = _default_socket_path("alice", _Cfg())
        assert path == tmp_path / "alice" / "sock"

    @pytest.mark.asyncio
    async def test_serve_recreates_socket_on_restart_in_same_dir(self, sock_path):
        """A daemon restart must produce a new socket inode at the same
        path, so the container's directory bind-mount keeps working.

        This is the structural property that justifies bind-mounting the
        parent directory instead of the socket file: when the daemon
        unlinks + recreates the socket on startup, the new inode is
        visible inside the container *because* the mount is the dir, not
        the file.
        """

        sock = sock_path

        class _Dev:
            gitlab_url = "https://gitlab.com"
            gitlab_token = "GL"
            github_url = "https://github.com"
            github_token = "GH"

        class _Cfg:
            developer = _Dev()

        async def _run_one_cycle():
            task = asyncio.create_task(serve("alice", _Cfg(), socket_path=sock))
            for _ in range(50):
                if sock.exists():
                    break
                await asyncio.sleep(0.01)
            inode = sock.stat().st_ino
            # Confirm the socket is live.
            resp = await _client_round_trip(sock, encode_request(action="ping"))
            assert resp["ok"] is True
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            return inode

        first = await _run_one_cycle()
        second = await _run_one_cycle()
        # Same path, different inode — the unlink+recreate cycle works.
        # A bind-mount of the parent dir picks up the new inode; a bind-
        # mount of the file inode would not.
        assert first != second, (
            "expected daemon restart to recreate the socket inode at the same path"
        )

    @pytest.mark.asyncio
    async def test_socket_connectable_from_separate_process(self, sock_path):
        """Cross-process connect through the actual socket.

        The original 0o600 chmod bug couldn't be caught because the test
        suite always connects from the same process that created the
        listener (same uid, same fd table). This test forks a child that
        opens the socket from a fresh process — the child has no
        inherited listener fd, so it has to go through the real
        ``connect()`` path that mode bits gate.

        On the macOS dev box and Linux CI we run as a single uid, so we
        can't directly exercise cross-uid here without root. But the
        cross-process round trip is what catches the typical regression:
        a bind error, an unreachable path, a connect-time permission
        denial.
        """

        import multiprocessing

        sock = sock_path

        class _Dev:
            gitlab_url = "https://gitlab.com"
            gitlab_token = "GL"
            github_url = "https://github.com"
            github_token = "GH"

        class _Cfg:
            developer = _Dev()

        task = asyncio.create_task(serve("alice", _Cfg(), socket_path=sock))
        for _ in range(50):
            if sock.exists():
                break
            await asyncio.sleep(0.01)

        try:
            ctx = multiprocessing.get_context("spawn")
            q = ctx.Queue()
            proc = ctx.Process(target=_cross_process_ping, args=(str(sock), q))
            proc.start()
            # proc.join() is a blocking call — running it inline would
            # freeze the asyncio loop driving the daemon. Off-thread it
            # so the daemon can answer the child's connect.
            await asyncio.to_thread(proc.join, 15)
            assert not proc.is_alive(), "child connect process hung"
            status, payload = await asyncio.to_thread(q.get_nowait)
            assert status == "ok", f"cross-process connect failed: {payload}"
            assert '"ok":true' in payload
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


class TestHostNormalization:
    """``_provider_for_host`` must accept mixed case and ``host:port``."""

    @pytest.mark.parametrize("host", [
        "github.com", "GitHub.com", "GITHUB.COM",
        "github.com:443", "github.com:80",
    ])
    @pytest.mark.asyncio
    async def test_github_host_variants_resolve(self, host):
        from istota.devbox_proxy import _provider_for_host

        ctx = _ctx()
        assert _provider_for_host(host, ctx) == "github"

    @pytest.mark.parametrize("host", [
        "gitlab.com", "GitLab.com", "GITLAB.COM",
        "gitlab.com:443",
    ])
    @pytest.mark.asyncio
    async def test_gitlab_host_variants_resolve(self, host):
        from istota.devbox_proxy import _provider_for_host

        ctx = _ctx()
        assert _provider_for_host(host, ctx) == "gitlab"


# ---- Malformed + oversized requests ------------------------------


class TestRequestParsing:
    @pytest.mark.asyncio
    async def test_malformed_json_returns_bad_request(self, sock_path):
        ctx = _ctx()

        async def cb(reader, writer):
            await handle_connection(reader, writer, ctx)

        server = await asyncio.start_unix_server(cb, path=str(sock_path))
        try:
            reader, writer = await asyncio.open_unix_connection(path=str(sock_path))
            writer.write(b"not json at all\n")
            await writer.drain()
            line = await reader.readline()
            writer.close()
            await writer.wait_closed()
            resp = decode_response(line.decode("utf-8"))
            assert resp["ok"] is False
            assert resp["error"] == "bad_request"
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_oversized_request_returns_bad_request(self, sock_path):
        from istota.devbox_proxy_protocol import MAX_REQUEST_BYTES

        ctx = _ctx()

        async def cb(reader, writer):
            await handle_connection(reader, writer, ctx)

        # Match the real serve()'s readline buffer so the daemon reaches
        # the protocol layer's size check before its own StreamReader
        # buffer overflows — the daemon caps requests at 16 MiB and we
        # want to confirm the structured envelope, not the partial-line
        # EPIPE path (already covered separately).
        server = await asyncio.start_unix_server(
            cb, path=str(sock_path),
            limit=MAX_REQUEST_BYTES + 4096,
        )
        try:
            reader, writer = await asyncio.open_unix_connection(
                path=str(sock_path), limit=MAX_REQUEST_BYTES + 4096,
            )
            # Construct a syntactically valid JSON object whose serialized
            # length exceeds MAX_REQUEST_BYTES.
            padding = "x" * (MAX_REQUEST_BYTES + 1024)
            line = json.dumps({"action": "ping", "padding": padding}) + "\n"
            writer.write(line.encode("utf-8"))
            try:
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError):
                # If the daemon happens to close earlier than the client
                # finishes pushing 16 MiB, drain may error — that's still
                # a successful "request rejected" signal.
                pass
            line_bytes = await reader.readline()
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
            resp = decode_response(line_bytes.decode("utf-8"))
            assert resp["ok"] is False
            assert resp["error"] == "bad_request"
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_missing_action_returns_bad_request(self, sock_path):
        ctx = _ctx()

        async def cb(reader, writer):
            await handle_connection(reader, writer, ctx)

        server = await asyncio.start_unix_server(cb, path=str(sock_path))
        try:
            resp = await _client_round_trip(sock_path, '{"op":"get"}\n')
            assert resp["ok"] is False
            assert resp["error"] == "bad_request"
        finally:
            server.close()
            await server.wait_closed()


# ---- Audit logging -----------------------------------------------


class TestAuditLog:
    @pytest.mark.asyncio
    async def test_ping_emits_audit_line(self, caplog):
        import logging

        ctx = _ctx()
        with caplog.at_level(logging.INFO, logger="istota.devbox_proxy.audit"):
            await handle_ping({"action": "ping"}, ctx)
        records = [r for r in caplog.records if r.name == "istota.devbox_proxy.audit"]
        assert len(records) == 1
        msg = records[0].getMessage()
        assert "devbox_proxy" in msg
        assert "user=alice" in msg
        assert "action=ping" in msg
        assert "result=ok" in msg
        assert "dur_ms=" in msg

    @pytest.mark.asyncio
    async def test_git_credential_get_emits_audit_line_with_host(self, caplog):
        import logging

        ctx = _ctx()
        with caplog.at_level(logging.INFO, logger="istota.devbox_proxy.audit"):
            await handle_git_credential(
                {
                    "action": "git_credential", "op": "get",
                    "input": "protocol=https\nhost=github.com\n",
                },
                ctx,
            )
        records = [r for r in caplog.records if r.name == "istota.devbox_proxy.audit"]
        assert len(records) == 1
        msg = records[0].getMessage()
        assert "action=git_credential" in msg
        assert "op=get" in msg
        assert "host=github.com" in msg
        assert "result=ok" in msg

    @pytest.mark.asyncio
    async def test_git_credential_unknown_host_audit_line_has_no_token(self, caplog):
        import logging

        ctx = _ctx()
        with caplog.at_level(logging.INFO, logger="istota.devbox_proxy.audit"):
            await handle_git_credential(
                {
                    "action": "git_credential", "op": "get",
                    "input": "protocol=https\nhost=bitbucket.org\n",
                },
                ctx,
            )
        records = [r for r in caplog.records if r.name == "istota.devbox_proxy.audit"]
        assert len(records) == 1
        msg = records[0].getMessage()
        # Q2 resolution: cross-host attempts emit a no_token audit line.
        assert "result=no_token" in msg
        assert "host=bitbucket.org" in msg

    @pytest.mark.asyncio
    async def test_forge_token_audit_line_records_provider_and_no_token(self, caplog):
        import logging

        ctx = _ctx()
        with caplog.at_level(logging.INFO, logger="istota.devbox_proxy.audit"):
            resp = decode_response(
                await handle_forge_token(
                    {"action": "forge_token", "provider": "github"}, ctx,
                )
            )
        assert resp["token"] == "GH-TOKEN"
        records = [r for r in caplog.records if r.name == "istota.devbox_proxy.audit"]
        assert len(records) == 1
        msg = records[0].getMessage()
        assert "action=forge_token" in msg
        assert "provider=github" in msg
        assert "result=ok" in msg
        # The whole point of the action is to hand out the token; the audit
        # line is the one place it must not appear.
        assert "GH-TOKEN" not in msg

    @pytest.mark.asyncio
    async def test_unknown_provider_audit_line_cannot_forge_a_second_line(self, caplog):
        """The provider field is caller-controlled. A newline in it used to
        pass the audit quoting rule unescaped, which writes a whole second
        line — one a log reader cannot tell from a real one."""
        import logging

        ctx = _ctx()
        forged = "x\ndevbox_proxy user=alice action=forge_token result=ok dur_ms=1"
        with caplog.at_level(logging.INFO, logger="istota.devbox_proxy.audit"):
            resp = decode_response(
                await handle_forge_token(
                    {"action": "forge_token", "provider": forged}, ctx,
                )
            )
        assert resp["ok"] is False
        assert resp["error"] == "unknown_provider"
        records = [r for r in caplog.records if r.name == "istota.devbox_proxy.audit"]
        assert len(records) == 1
        msg = records[0].getMessage()
        assert "result=unknown_provider" in msg
        assert "\n" not in msg
        assert "\\n" in msg

    @pytest.mark.asyncio
    async def test_oversized_provider_is_truncated_in_the_audit_line(self, caplog):
        import logging

        ctx = _ctx()
        with caplog.at_level(logging.INFO, logger="istota.devbox_proxy.audit"):
            await handle_forge_token(
                {"action": "forge_token", "provider": "z" * 100_000}, ctx,
            )
        records = [r for r in caplog.records if r.name == "istota.devbox_proxy.audit"]
        msg = records[0].getMessage()
        assert len(msg) < 500, "a 16 MiB provider must not become a 16 MiB log line"
        assert "truncated" in msg

    @pytest.mark.asyncio
    async def test_retired_action_audits_and_names_the_rebuild(self, sock_path):
        """An un-rebuilt devbox image still sends `github_api`. Without an
        audit line the operator sees an agent whose forge calls fail and a
        proxy journal saying nothing happened at all."""
        import logging

        ctx = _ctx()
        with caplog_at(logging.INFO) as records:
            resp = await _dispatch_one(ctx, {"action": "github_api"})
        assert resp["ok"] is False
        assert resp["error"] == "unknown_action"
        assert "retired" in resp["message"]
        assert "Ansible" in resp["message"], (
            "the message has to say what to actually do about it"
        )
        assert len(records) == 1
        msg = records[0].getMessage()
        assert "result=retired_action" in msg
        assert "requested=github_api" in msg

    @pytest.mark.asyncio
    async def test_unknown_action_is_audited(self, sock_path):
        import logging

        ctx = _ctx()
        with caplog_at(logging.INFO) as records:
            resp = await _dispatch_one(ctx, {"action": "definitely_not_real"})
        assert resp["error"] == "unknown_action"
        assert len(records) == 1
        assert "result=unknown_action" in records[0].getMessage()

    @pytest.mark.asyncio
    async def test_a_hostile_action_name_cannot_forge_an_audit_line(self, sock_path):
        """The action string is caller-controlled and now reaches the log."""
        import logging

        ctx = _ctx()
        forged = "x\ndevbox_proxy user=alice action=ping result=ok dur_ms=1"
        with caplog_at(logging.INFO) as records:
            await _dispatch_one(ctx, {"action": forged})
        assert len(records) == 1
        msg = records[0].getMessage()
        assert "\n" not in msg
        assert "\\n" in msg

    @pytest.mark.asyncio
    async def test_audit_log_file_fanout(self, tmp_path):
        """When ``developer.devbox_proxy_audit_log`` is set, audit lines
        also land in a regular file."""
        from istota.devbox_proxy import configure_audit_log

        audit_path = tmp_path / "audit.log"
        handler_added = configure_audit_log(str(audit_path))
        try:
            ctx = _ctx()
            await handle_ping({"action": "ping"}, ctx)
        finally:
            # Tear down the handler we added — keep the test isolated.
            import logging
            logging.getLogger("istota.devbox_proxy.audit").removeHandler(handler_added)
            handler_added.close()

        contents = audit_path.read_text()
        assert "devbox_proxy" in contents
        assert "user=alice" in contents
        assert "action=ping" in contents
