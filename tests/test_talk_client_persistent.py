"""Tests for TalkClient's persistent-client lifecycle + get_talk_client singleton.

Stage 2 of the persistent-asyncio-loop spec. The 11 TalkClient methods still
wrap their bodies in ``async with httpx.AsyncClient(...)`` at this stage — the
persistent ``self._client`` is created and idle. Per-method use of ``_client``
(and the post-aclose / per-request-timeout method behaviours) lands in Stage 6.
"""

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
    def test_returns_same_instance_and_client(self):
        cfg = _config()
        a = get_talk_client(cfg)
        b = get_talk_client(cfg)
        assert a is b
        assert id(a._client) == id(b._client)
        assert a._client is not None  # opened on the persistent loop

    def test_client_opened_on_persistent_loop(self):
        a = get_talk_client(_config())
        # The underlying httpx client must be bound to the runtime's loop,
        # not whatever thread first called get_talk_client.
        rt = get_async_runtime()
        assert rt.is_running is True
        assert a._client is not None

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
