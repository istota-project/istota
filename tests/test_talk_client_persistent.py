"""Tests for TalkClient's persistent-client lifecycle + get_talk_client singleton.

Stage 2 of the persistent-asyncio-loop spec. The 11 TalkClient methods still
wrap their bodies in ``async with httpx.AsyncClient(...)`` at this stage — the
persistent ``self._client`` is created and idle. Per-method use of ``_client``
(and the post-aclose / per-request-timeout method behaviours) lands in Stage 6.
"""

from unittest.mock import AsyncMock

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
