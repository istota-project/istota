"""Tests for TalkClient's persistent-client lifecycle + get_talk_client singleton.

Persistent-asyncio-loop spec. Covers the Stage 2 lifecycle (``_ensure_open`` /
``aclose`` / ``is_closed``), the Stage 3 proof that delivery reuses the
singleton, and the Stage 6 behaviour where the 11 TalkClient methods issue
requests on the persistent ``self._client`` (connection reuse, post-aclose
failure, per-request timeout override).

**Deliberately not converted onto `fake_talk`.** The real ``TalkClient`` and
the real ``get_talk_client`` factory are what this file is testing, so patching
the factory out would delete the subject rather than guard it — it is the one
file in the tree that must construct a live client. It mocks at the ``httpx``
layer, *below* the client, so no room registry is involved and ``"room123"`` is
a string handed to a mocked transport rather than a destination anything
resolved. There is nothing here for a room-shape parametrization to tell apart.
"""

import asyncio
import contextlib
import http.server
import json
import threading
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from istota.async_runtime import (
    get_async_runtime,
    get_talk_client,
    reset_async_runtime,
    reset_talk_client,
    run_coro,
)
from istota.config import Config, NextcloudConfig
from istota.talk import TalkClient


def _config() -> Config:
    return Config(
        nextcloud=NextcloudConfig(
            url="https://nc.example.com",
            username="bot",
            app_password="secret",
        )
    )


@pytest.fixture(autouse=True)
def _reset_singletons():
    yield
    reset_talk_client()
    reset_async_runtime()


class TestTalkClientLifecycle:
    def test_ensure_open_creates_client(self):
        client = TalkClient(_config())
        assert client._client is None

        async def go():
            c = await client._ensure_open()
            assert c is client._client
            await client.aclose()

        run_coro(go())

    def test_ensure_open_is_idempotent(self):
        client = TalkClient(_config())

        async def go():
            first = await client._ensure_open()
            second = await client._ensure_open()
            assert first is second
            await client.aclose()

        run_coro(go())

    def test_aclose_idempotent(self):
        client = TalkClient(_config())

        async def go():
            await client._ensure_open()
            await client.aclose()
            await client.aclose()  # must not raise

        run_coro(go())
        assert client.is_closed is True

    def test_ensure_open_after_close_raises(self):
        client = TalkClient(_config())

        async def go():
            await client._ensure_open()
            await client.aclose()
            with pytest.raises(RuntimeError, match="closed"):
                await client._ensure_open()

        run_coro(go())


class TestGetTalkClientSingleton:
    def test_returns_same_instance(self):
        cfg = _config()
        a = get_talk_client(cfg)
        b = get_talk_client(cfg)
        assert a is b

    def test_accessor_starts_runtime_for_cleanup_hook(self):
        # The accessor must not eagerly open the httpx pool (that would require
        # run_coro and trip the within-the-loop guard for callers already on the
        # persistent loop). The pool opens lazily on first awaited use.
        a = get_talk_client(_config())
        assert a._client is None  # not opened eagerly
        rt = get_async_runtime()
        assert rt.is_running is True  # started so the aclose hook will fire

    def test_pool_opens_on_persistent_loop_via_run_coro(self):
        client = get_talk_client(_config())

        async def open_it():
            return await client._ensure_open()

        opened = run_coro(open_it())
        assert client._client is opened
        # A second get returns the same instance with the same live pool.
        again = get_talk_client(_config())
        assert again is client
        assert again._client is opened

    def test_cleanup_hook_closes_client_on_runtime_stop(self):
        client = get_talk_client(_config())
        assert client.is_closed is False
        reset_async_runtime()  # stops runtime -> runs cleanup hooks
        assert client.is_closed is True

    def test_reset_talk_client_drops_singleton(self):
        a = get_talk_client(_config())
        reset_talk_client()
        reset_async_runtime()
        b = get_talk_client(_config())
        assert b is not a


class TestProofSiteReusesSingleton:
    """Stage 3 proof: the migrated TalkTransport.edit path, driven via run_coro,
    reuses the one persistent TalkClient across calls instead of constructing a
    fresh transient client each time."""

    def test_edit_reuses_persistent_singleton(self):
        from istota.transport.talk import TalkTransport

        cfg = _config()
        client = get_talk_client(cfg)
        client.edit_message = AsyncMock()
        transport = TalkTransport(cfg)

        run_coro(transport.edit("room1", 1, "first"))
        run_coro(transport.edit("room1", 2, "second"))

        assert client.edit_message.await_count == 2
        # Both edits resolved the same cached singleton (no per-call construction).
        assert get_talk_client(cfg) is client


class TestStage6PersistentRequests:
    """Stage 6: methods use the persistent self._client (no per-call async-with).
    Verify connection reuse, post-aclose failure, and per-request timeout
    override."""

    def _resp(self, json_value, status=200):
        resp = MagicMock()
        resp.status_code = status
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=json_value)
        return resp

    def test_send_message_reuses_one_httpx_client(self):
        client = get_talk_client(_config())
        with patch("istota.talk.httpx.AsyncClient") as MockHttp:
            inst = MockHttp.return_value
            inst.post = AsyncMock(return_value=self._resp({"ocs": {"data": {"id": 1}}}))
            inst.aclose = AsyncMock()

            async def go():
                await client.send_message("room", "a")
                first = client._client
                await client.send_message("room", "b")
                return first, client._client

            first, second = run_coro(go())

        assert first is second  # same persistent httpx client across calls
        assert MockHttp.call_count == 1  # constructed exactly once

    def test_method_after_aclose_raises(self):
        client = get_talk_client(_config())
        with patch("istota.talk.httpx.AsyncClient") as MockHttp:
            MockHttp.return_value.aclose = AsyncMock()

            async def go():
                await client._ensure_open()
                await client.aclose()
                with pytest.raises(RuntimeError, match="closed"):
                    await client.send_message("room", "x")

            run_coro(go())

    def test_poll_messages_overrides_timeout_per_request(self):
        client = get_talk_client(_config())
        with patch("istota.talk.httpx.AsyncClient") as MockHttp:
            inst = MockHttp.return_value
            inst.get = AsyncMock(return_value=self._resp({"ocs": {"data": []}}))
            inst.aclose = AsyncMock()

            async def go():
                # long-poll: request_timeout = timeout + 10 = 40, overriding the
                # persistent client's DEFAULT_TIMEOUT.
                await client.poll_messages("room", last_known_message_id=5, timeout=30)

            run_coro(go())

        assert inst.get.call_args.kwargs["timeout"] == 40


class TestShimDeliveryEndToEnd:
    """The scheduler delivery shims are the highest-traffic Talk call sites, but
    the scheduler unit tests mock run_coro away — so the path from the shim
    through run_coro onto the persistent client is otherwise unexercised. This
    drives post_result_to_talk end to end with only the httpx layer mocked,
    proving the awaited TalkClient method runs on the persistent loop and reuses
    the singleton client."""

    def _resp(self, json_value, status=200):
        resp = MagicMock()
        resp.status_code = status
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=json_value)
        return resp

    def test_post_result_to_talk_drives_persistent_client(self):
        from istota.scheduler import post_result_to_talk

        cfg = _config()
        task = MagicMock()
        task.id = 7
        task.conversation_token = "room42"
        task.is_group_chat = False
        task.talk_message_id = None
        task.user_id = "alice"

        with patch("istota.talk.httpx.AsyncClient") as MockHttp:
            inst = MockHttp.return_value
            inst.post = AsyncMock(
                return_value=self._resp({"ocs": {"data": {"id": 99}}})
            )
            inst.aclose = AsyncMock()

            msg_id = run_coro(post_result_to_talk(cfg, task, "hello"))

        assert msg_id == 99
        # The shim awaited the persistent client's post exactly once, on the loop.
        assert inst.post.await_count == 1
        assert MockHttp.call_count == 1  # one pooled client, not a per-call one
        # The singleton the shim used is the process-global one.
        assert get_talk_client(cfg)._client is inst


class TestTheSingletonIsBoundToOneLoop:
    """Why `get_talk_client`'s "bound to the runtime loop" is a constraint and
    not a note (ISSUE-407).

    `_ensure_open` builds the `httpx.AsyncClient` lazily, so its pool — and the
    `anyio` primitives inside it — bind to whichever loop issues the first
    request. A second loop then fails on every call for the life of the process.

    The first test runs against a real socket rather than a mocked `httpx`,
    because the failure lives in httpx's own connection machinery and a mock
    reproduces nothing. It is also the dependency fact the guard rests on: if a
    future httpx makes a pool loop-agnostic, it goes red and the guard can be
    reconsidered rather than silently outliving its reason.
    """

    def test_the_second_loop_fails_after_the_request_has_gone_out(self):
        """The shape that makes this worse than an ordinary error.

        The server records the request, so the side effect happened; the caller
        gets a `RuntimeError` and, at the call site this issue is about, reads
        it as "the operation did not happen".
        """
        with _ocs_stub() as stub:
            client = TalkClient(_config_at(stub.base_url))
            try:
                first = run_coro(client.get_conversation_info("on-the-runtime-loop"))
                assert first == {"displayName": "ok"}

                with pytest.raises(RuntimeError) as excinfo:
                    asyncio.run(client.get_conversation_info("on-another-loop"))
            finally:
                run_coro(client.aclose())

        assert "different event loop" in str(excinfo.value)
        # Both requests reached the server. The second one's answer was written
        # to the socket and never read — which is the whole claim, and it is
        # also why this has to *wait*: the client raised before reading the
        # response, so it is no evidence that the handler thread has run at all.
        # Asserting the list directly is a race that happens to win.
        assert stub.wait_for(2) == ["on-the-runtime-loop", "on-another-loop"]

    def test_the_accessor_refuses_a_caller_on_another_loop(self):
        """The guard, and the reason it raises rather than logging.

        Without it the misuse is invisible until the request has already been
        written — which is how `web_app._delete_from_talk`'s bot leg spent its
        life reporting a delete Nextcloud may well have performed as one no
        credential could make. Raising here happens *before* the side effect.
        """
        cfg = _config()
        # From the runtime loop itself: allowed, and the reentrant case the
        # accessor's docstring is about.
        assert run_coro(_get_on_this_loop(cfg)) is get_talk_client(cfg)

        with pytest.raises(RuntimeError, match="runtime loop"):
            asyncio.run(_get_on_this_loop(cfg))

    def test_a_caller_with_no_running_loop_is_left_alone(self):
        """Most callers resolve the client synchronously and hand it to
        `run_coro`; they have no loop to compare against and must not be
        refused."""
        assert get_talk_client(_config()) is not None


async def _get_on_this_loop(cfg):
    return get_talk_client(cfg)


def _config_at(base_url: str) -> Config:
    return Config(
        nextcloud=NextcloudConfig(
            url=base_url, username="bot", app_password="secret",
        )
    )


@contextlib.contextmanager
def _ocs_stub():
    """A loopback HTTP server answering any OCS room read, recording the token.

    Bound to 127.0.0.1 on an ephemeral port, so nothing outside this process can
    reach it and there is no credential for it to expect.

    `wait_for(n)` rather than a bare `paths` read: the handler runs on its own
    thread (`ThreadingHTTPServer` with `daemon_threads`, so `server_close` joins
    none of them), and the caller that matters here raised *before* reading its
    response — so nothing in the client's own control flow establishes that the
    handler has run.
    """
    paths: list[str] = []
    recorded = threading.Condition()

    def _wait_for(n: int, timeout: float = 5.0) -> list[str]:
        with recorded:
            recorded.wait_for(lambda: len(paths) >= n, timeout=timeout)
            return list(paths)

    class _Handler(http.server.BaseHTTPRequestHandler):
        # Keep-alive, so the first request leaves a connection in the pool for
        # the second to reuse. On HTTP/1.0 the server closes after each answer,
        # the second loop dials a fresh connection of its own and the
        # cross-loop failure does not happen at all — which is a fact about the
        # stub, not about the product: Nextcloud speaks HTTP/1.1.
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
            with recorded:
                paths.append(self.path.rsplit("/", 1)[-1])
                recorded.notify_all()
            body = json.dumps({"ocs": {"data": {"displayName": "ok"}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):  # keep the suite's own output clean
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield types.SimpleNamespace(
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
            paths=paths,
            wait_for=_wait_for,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
