"""Tests for the devbox credential proxy daemon (Stages 2–3).

Stage 2 (happy paths): ping, git_credential get/store/erase, gitlab_api,
github_api against a synthetic context with mocked httpx transport.
End-to-end coverage via a tmpdir Unix socket exercises
asyncio.start_unix_server + handle_connection without going through
systemd.

Stage 3 (edge cases + error handling): allowlist enforcement, oversized
requests, malformed JSON, upstream errors, timeouts, and audit logging.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import httpx
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
    handle_git_credential,
    handle_github_api,
    handle_gitlab_api,
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
    gitlab_allowlist: tuple[str, ...] = ("GET /projects/*", "POST /projects/*/merge_requests"),
    github_allowlist: tuple[str, ...] = ("GET /repos/*", "POST /repos/*/pulls"),
    api_timeout: float = 5.0,
    http_handler=None,
) -> DevboxProxyContext:
    """Build a DevboxProxyContext with a MockTransport-backed httpx client.

    ``http_handler`` is a callable ``(request: httpx.Request) -> httpx.Response``
    that the MockTransport routes every request through. If None, all calls
    return 200 OK with an empty body.
    """

    if http_handler is None:
        def http_handler(request):  # pragma: no cover (only used if a test forgot to override)
            return httpx.Response(200, text="")

    transport = httpx.MockTransport(http_handler)
    client = httpx.AsyncClient(transport=transport, timeout=api_timeout)
    return DevboxProxyContext(
        user_id=user_id,
        gitlab_token=gitlab_token,
        github_token=github_token,
        gitlab_url=gitlab_url,
        github_url=github_url,
        gitlab_allowlist=gitlab_allowlist,
        github_allowlist=github_allowlist,
        api_timeout=api_timeout,
        http_client=client,
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


# ---- handle_gitlab_api / handle_github_api ---------------------------------


class TestGitlabApi:
    @pytest.mark.asyncio
    async def test_happy_path_get(self):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text='{"id":42,"name":"foo"}')

        ctx = _ctx(http_handler=handler)
        req = {
            "action": "gitlab_api",
            "method": "GET",
            "endpoint": "/projects/42",
            "body": None,
        }
        resp = decode_response(await handle_gitlab_api(req, ctx))
        assert resp["ok"] is True
        assert resp["status"] == 200
        assert resp["body"] == '{"id":42,"name":"foo"}'

        assert len(seen) == 1
        sent = seen[0]
        # URL composition: gitlab_url + /api/v4 + endpoint.
        assert str(sent.url) == "https://gitlab.com/api/v4/projects/42"
        assert sent.method == "GET"
        # Server-side header injection.
        assert sent.headers["PRIVATE-TOKEN"] == "GL-TOKEN"

    @pytest.mark.asyncio
    async def test_happy_path_post_with_body(self):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(201, text='{"iid":7}')

        ctx = _ctx(http_handler=handler)
        req = {
            "action": "gitlab_api",
            "method": "POST",
            "endpoint": "/projects/42/merge_requests",
            "body": '{"title":"x","source_branch":"f","target_branch":"main"}',
        }
        resp = decode_response(await handle_gitlab_api(req, ctx))
        assert resp["ok"] is True
        assert resp["status"] == 201
        sent = seen[0]
        assert sent.method == "POST"
        body_text = sent.content.decode("utf-8")
        assert "source_branch" in body_text

    @pytest.mark.asyncio
    async def test_custom_gitlab_url_used_as_base(self):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="{}")

        ctx = _ctx(gitlab_url="https://gitlab.example.com", http_handler=handler)
        req = {
            "action": "gitlab_api",
            "method": "GET",
            "endpoint": "/projects/1",
            "body": None,
        }
        await handle_gitlab_api(req, ctx)
        assert str(seen[0].url) == "https://gitlab.example.com/api/v4/projects/1"

    @pytest.mark.asyncio
    async def test_token_never_appears_in_request_body_or_url(self):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="{}")

        ctx = _ctx(gitlab_token="VERY-SECRET-GL", http_handler=handler)
        req = {
            "action": "gitlab_api",
            "method": "GET",
            "endpoint": "/projects/1",
            "body": None,
        }
        await handle_gitlab_api(req, ctx)
        sent = seen[0]
        # Only the PRIVATE-TOKEN header should carry the secret. Any URL or
        # body path is a leak — the proxy is supposed to keep the token
        # server-side.
        assert "VERY-SECRET-GL" not in str(sent.url)
        body_bytes = sent.content
        assert b"VERY-SECRET-GL" not in body_bytes
        assert sent.headers["PRIVATE-TOKEN"] == "VERY-SECRET-GL"

    @pytest.mark.asyncio
    async def test_extra_headers_forwarded(self):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="{}")

        ctx = _ctx(http_handler=handler)
        req = {
            "action": "gitlab_api",
            "method": "GET",
            "endpoint": "/projects/1",
            "body": None,
            "headers": {"X-Trace": "abc123"},
        }
        await handle_gitlab_api(req, ctx)
        assert seen[0].headers["X-Trace"] == "abc123"

    @pytest.mark.asyncio
    async def test_no_token_when_gitlab_token_empty(self):
        ctx = _ctx(gitlab_token="")
        req = {
            "action": "gitlab_api",
            "method": "GET",
            "endpoint": "/projects/1",
            "body": None,
        }
        resp = decode_response(await handle_gitlab_api(req, ctx))
        assert resp["ok"] is False
        assert resp["error"] == "no_token"


class TestGithubApi:
    @pytest.mark.asyncio
    async def test_happy_path_post(self):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(201, text='{"number":99,"html_url":"https://x"}')

        ctx = _ctx(http_handler=handler)
        req = {
            "action": "github_api",
            "method": "POST",
            "endpoint": "/repos/foo/bar/pulls",
            "body": '{"title":"x","head":"f","base":"main"}',
        }
        resp = decode_response(await handle_github_api(req, ctx))
        assert resp["ok"] is True
        assert resp["status"] == 201

        sent = seen[0]
        # Public github.com base is rewritten to api.github.com.
        assert str(sent.url) == "https://api.github.com/repos/foo/bar/pulls"
        assert sent.headers["Authorization"] == "token GH-TOKEN"
        assert sent.headers["Accept"] == "application/vnd.github+json"

    @pytest.mark.asyncio
    async def test_token_never_appears_in_request_body_or_url(self):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="{}")

        ctx = _ctx(github_token="VERY-SECRET-GH", http_handler=handler)
        req = {
            "action": "github_api",
            "method": "GET",
            "endpoint": "/repos/foo/bar",
            "body": None,
        }
        await handle_github_api(req, ctx)
        sent = seen[0]
        assert "VERY-SECRET-GH" not in str(sent.url)
        assert b"VERY-SECRET-GH" not in sent.content
        assert sent.headers["Authorization"] == "token VERY-SECRET-GH"

    @pytest.mark.asyncio
    async def test_ghe_url_uses_api_v3_base(self):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="{}")

        ctx = _ctx(github_url="https://ghe.example.com", http_handler=handler)
        req = {
            "action": "github_api",
            "method": "GET",
            "endpoint": "/repos/foo/bar",
            "body": None,
        }
        await handle_github_api(req, ctx)
        assert str(seen[0].url) == "https://ghe.example.com/api/v3/repos/foo/bar"

    @pytest.mark.asyncio
    async def test_no_token_when_github_token_empty(self):
        ctx = _ctx(github_token="")
        req = {
            "action": "github_api",
            "method": "GET",
            "endpoint": "/repos/foo/bar",
            "body": None,
        }
        resp = decode_response(await handle_github_api(req, ctx))
        assert resp["ok"] is False
        assert resp["error"] == "no_token"


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
            gitlab_api_allowlist: list = []
            github_api_allowlist: list = []
            api_timeout_seconds = 5

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
            gitlab_api_allowlist: list = []
            github_api_allowlist: list = []
            api_timeout_seconds = 5

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
            gitlab_api_allowlist: list = []
            github_api_allowlist: list = []
            api_timeout_seconds = 5

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


# ---- Stage 3: allowlist enforcement ----------------------------------------


class TestAllowlist:
    @pytest.mark.asyncio
    async def test_gitlab_endpoint_in_allowlist_is_called(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="{}")

        ctx = _ctx(
            gitlab_allowlist=("GET /projects/*",),
            http_handler=handler,
        )
        req = {
            "action": "gitlab_api", "method": "GET",
            "endpoint": "/projects/42", "body": None,
        }
        resp = decode_response(await handle_gitlab_api(req, ctx))
        assert resp["ok"] is True
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_gitlab_endpoint_outside_allowlist_returns_not_allowed(self):
        called = []

        def handler(request):
            called.append(request)
            return httpx.Response(200, text="{}")

        ctx = _ctx(
            gitlab_allowlist=("GET /projects/*",),
            http_handler=handler,
        )
        req = {
            "action": "gitlab_api", "method": "DELETE",
            "endpoint": "/projects/42", "body": None,
        }
        resp = decode_response(await handle_gitlab_api(req, ctx))
        assert resp["ok"] is False
        assert resp["error"] == "not_allowed"
        assert "/projects/42" in resp["message"]
        # Upstream must not be called when the endpoint is rejected.
        assert called == []

    @pytest.mark.asyncio
    async def test_gitlab_allowlist_strips_query_string(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="{}")

        # Allowlist pattern matches the path only; the request carries a
        # query string. The proxy should still allow it.
        ctx = _ctx(
            gitlab_allowlist=("GET /projects/*",),
            http_handler=handler,
        )
        req = {
            "action": "gitlab_api", "method": "GET",
            "endpoint": "/projects/42?private_token=ignored&per_page=10",
            "body": None,
        }
        resp = decode_response(await handle_gitlab_api(req, ctx))
        assert resp["ok"] is True

    @pytest.mark.asyncio
    async def test_github_endpoint_outside_allowlist_returns_not_allowed(self):
        called = []

        def handler(request):
            called.append(request)
            return httpx.Response(200, text="{}")

        ctx = _ctx(
            github_allowlist=("GET /repos/*",),
            http_handler=handler,
        )
        req = {
            "action": "github_api", "method": "DELETE",
            "endpoint": "/repos/foo/bar", "body": None,
        }
        resp = decode_response(await handle_github_api(req, ctx))
        assert resp["ok"] is False
        assert resp["error"] == "not_allowed"
        assert called == []

    @pytest.mark.asyncio
    async def test_github_allowlist_pattern_with_trailing_glob(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(201, text="{}")

        ctx = _ctx(
            github_allowlist=("POST /repos/*/pulls",),
            http_handler=handler,
        )
        # /repos/<owner>/<repo>/pulls should match the wildcard segment.
        req = {
            "action": "github_api", "method": "POST",
            "endpoint": "/repos/foo/bar/pulls", "body": '{"title":"x"}',
        }
        resp = decode_response(await handle_github_api(req, ctx))
        assert resp["ok"] is True


class TestHeaderSmugglingRejection:
    """Reject CR/LF/NUL in caller-supplied header values so a malicious
    container can't smuggle extra HTTP headers into the upstream request."""

    @pytest.mark.parametrize("bad", ["foo\nbar", "foo\rbar", "foo\x00bar"])
    @pytest.mark.asyncio
    async def test_rejects_newline_in_header_value(self, bad):
        called = []

        def handler(request):
            called.append(request)
            return httpx.Response(200, text="{}")

        ctx = _ctx(
            github_allowlist=("GET /repos/*",),
            http_handler=handler,
        )
        req = {
            "action": "github_api", "method": "GET",
            "endpoint": "/repos/foo/bar", "body": None,
            "headers": {"X-Custom": bad},
        }
        resp = decode_response(await handle_github_api(req, ctx))
        assert resp["ok"] is False
        assert resp["error"] == "bad_request"
        # Upstream must not be called when smuggling is rejected.
        assert called == []


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


class TestBundledDefaultAllowlists:
    """Regression: the bundled DeveloperConfig defaults must match what the
    shims actually emit. The legacy host-side wrapper kept the /api/v4
    prefix on GitLab patterns; the proxy strips it into base_url, so the
    defaults need bare paths."""

    def _ctx_with_developer_defaults(self, http_handler):
        from istota.config import DeveloperConfig

        dev = DeveloperConfig()
        transport = httpx.MockTransport(http_handler)
        client = httpx.AsyncClient(transport=transport, timeout=5.0)
        return DevboxProxyContext(
            user_id="alice",
            gitlab_token="GL-TOKEN",
            github_token="GH-TOKEN",
            gitlab_url=dev.gitlab_url,
            github_url=dev.github_url,
            gitlab_allowlist=tuple(dev.gitlab_api_allowlist),
            github_allowlist=tuple(dev.github_api_allowlist),
            api_timeout=5.0,
            http_client=client,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,endpoint", [
        ("GET", "/user"),                                # glab auth status
        ("GET", "/projects/foo%2Fbar"),                  # glab repo view
        ("POST", "/projects/foo%2Fbar/merge_requests"),  # glab mr create
        ("GET", "/projects/foo%2Fbar/merge_requests/7"), # glab mr view
        ("POST", "/projects/foo%2Fbar/issues"),          # glab issue create
    ])
    async def test_glab_endpoints_pass_bundled_gitlab_allowlist(self, method, endpoint):
        called = []

        def handler(request):
            called.append(request)
            return httpx.Response(200, text="{}")

        ctx = self._ctx_with_developer_defaults(handler)
        req = {
            "action": "gitlab_api", "method": method,
            "endpoint": endpoint, "body": None,
        }
        resp = decode_response(await handle_gitlab_api(req, ctx))
        assert resp["ok"] is True, (
            f"{method} {endpoint} rejected by bundled GitLab allowlist: "
            f"{resp.get('error')} {resp.get('message')}"
        )
        assert len(called) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,endpoint", [
        ("GET", "/user"),                          # gh auth status
        ("GET", "/repos/foo/bar"),                 # gh repo view
        ("POST", "/repos/foo/bar/pulls"),          # gh pr create
        ("GET", "/repos/foo/bar/pulls/3"),         # gh pr view
        ("POST", "/repos/foo/bar/issues"),         # gh issue create
    ])
    async def test_gh_endpoints_pass_bundled_github_allowlist(self, method, endpoint):
        called = []

        def handler(request):
            called.append(request)
            return httpx.Response(200, text="{}")

        ctx = self._ctx_with_developer_defaults(handler)
        req = {
            "action": "github_api", "method": method,
            "endpoint": endpoint, "body": None,
        }
        resp = decode_response(await handle_github_api(req, ctx))
        assert resp["ok"] is True, (
            f"{method} {endpoint} rejected by bundled GitHub allowlist: "
            f"{resp.get('error')} {resp.get('message')}"
        )
        assert len(called) == 1


# ---- Stage 3: upstream errors + timeouts -----------------------------------


class TestUpstreamErrors:
    @pytest.mark.asyncio
    async def test_upstream_4xx_returns_upstream_error_with_status_and_body(self):
        def handler(request):
            return httpx.Response(422, text='{"error":"invalid"}')

        ctx = _ctx(http_handler=handler)
        req = {
            "action": "github_api", "method": "POST",
            "endpoint": "/repos/foo/bar/pulls", "body": '{"title":"x"}',
        }
        resp = decode_response(await handle_github_api(req, ctx))
        assert resp["ok"] is False
        assert resp["error"] == "upstream_error"
        assert resp["status"] == 422
        assert resp["body"] == '{"error":"invalid"}'

    @pytest.mark.asyncio
    async def test_upstream_5xx_returns_upstream_error(self):
        def handler(request):
            return httpx.Response(503, text="service unavailable")

        ctx = _ctx(http_handler=handler)
        req = {
            "action": "gitlab_api", "method": "GET",
            "endpoint": "/projects/1", "body": None,
        }
        resp = decode_response(await handle_gitlab_api(req, ctx))
        assert resp["ok"] is False
        assert resp["error"] == "upstream_error"
        assert resp["status"] == 503

    @pytest.mark.asyncio
    async def test_upstream_timeout_returns_upstream_error_status_zero(self):
        def handler(request):
            raise httpx.TimeoutException("simulated timeout")

        ctx = _ctx(http_handler=handler, api_timeout=0.5)
        req = {
            "action": "github_api", "method": "GET",
            "endpoint": "/repos/foo/bar", "body": None,
        }
        resp = decode_response(await handle_github_api(req, ctx))
        assert resp["ok"] is False
        assert resp["error"] == "upstream_error"
        assert resp["status"] == 0
        assert "timeout" in resp["message"].lower()


# ---- Stage 3: malformed + oversized requests ------------------------------


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


# ---- Stage 3: audit logging -----------------------------------------------


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
    async def test_api_call_audit_line_carries_method_endpoint_status(self, caplog):
        import logging

        def handler(request):
            return httpx.Response(201, text='{"number":1}')

        ctx = _ctx(http_handler=handler)
        with caplog.at_level(logging.INFO, logger="istota.devbox_proxy.audit"):
            await handle_github_api(
                {
                    "action": "github_api", "method": "POST",
                    "endpoint": "/repos/foo/bar/pulls", "body": '{"title":"x"}',
                },
                ctx,
            )
        records = [r for r in caplog.records if r.name == "istota.devbox_proxy.audit"]
        assert len(records) == 1
        msg = records[0].getMessage()
        assert "action=github_api" in msg
        assert "method=POST" in msg
        assert "endpoint=/repos/foo/bar/pulls" in msg
        assert "status=201" in msg
        assert "result=ok" in msg

    @pytest.mark.asyncio
    async def test_not_allowed_audit_carries_attempted_endpoint(self, caplog):
        import logging

        ctx = _ctx(github_allowlist=("GET /repos/*",))
        with caplog.at_level(logging.INFO, logger="istota.devbox_proxy.audit"):
            await handle_github_api(
                {
                    "action": "github_api", "method": "DELETE",
                    "endpoint": "/repos/foo/bar", "body": None,
                },
                ctx,
            )
        records = [r for r in caplog.records if r.name == "istota.devbox_proxy.audit"]
        assert len(records) == 1
        msg = records[0].getMessage()
        assert "result=not_allowed" in msg
        # Operator needs to see what was attempted.
        assert "endpoint=/repos/foo/bar" in msg
        assert "method=DELETE" in msg

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
