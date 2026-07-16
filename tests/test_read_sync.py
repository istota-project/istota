"""Tests for cross-surface read-state sync (Stages 4+5 of the user-scoped
Nextcloud OAuth spec): web→Talk push on cursor advance, Talk→web throttled
pull capped at the newest Talk-synced message."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db, web_tokens
from istota.config import Config, SiteConfig, UserConfig, WebConfig

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

_needs_web_deps = pytest.mark.skipif(
    not _has_web_deps, reason="web dependencies not installed",
)

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

KEY = "r" * 64
ORIGIN = {"origin": "https://example.com"}


def _make_config(tmp_path, token_storage="encrypted", sync_interval=60):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    config = Config(
        db_path=db_path,
        nextcloud_mount_path=tmp_path / "mount",
        site=SiteConfig(enabled=True, hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice")},
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
            token_storage=token_storage,
        ),
        bot_name="Istota",
    )
    config.web.chat.talk_read_sync_interval = sync_interval
    return config


def _patch_app(config):
    import istota.web_app as mod
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    return mod.app


async def _login(client, username="alice"):
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


def _mock_talk_client(monkeypatch, conversations=None):
    """Patch istota.talk.TalkClient with a recording factory."""
    constructed = []
    instance = MagicMock()
    instance.mark_conversation_read = AsyncMock(return_value=True)
    instance.list_conversations = AsyncMock(return_value=conversations or [])
    instance.aclose = AsyncMock()

    def factory(config, bearer_token=None, timeout=None):
        constructed.append({"bearer_token": bearer_token, "timeout": timeout})
        return instance

    import istota.talk
    monkeypatch.setattr(istota.talk, "TalkClient", factory)
    return constructed, instance


@pytest.fixture(autouse=True)
def _reset_module_state():
    import istota.web_app as mod
    web_tokens._refresh_locks.clear()
    mod._talk_read_pull_state.clear()
    mod._bg_tasks.clear()
    yield
    web_tokens._refresh_locks.clear()
    mod._talk_read_pull_state.clear()
    mod._bg_tasks.clear()


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(web_tokens._KEY_ENV_VAR, KEY)


async def _drain_bg():
    import istota.web_app as mod
    while mod._bg_tasks:
        await asyncio.gather(*list(mod._bg_tasks))


async def _default_room(client, config, cookies, bind_talk=True):
    rooms = (await client.get("/istota/api/chat/rooms", cookies=cookies)).json()["rooms"]
    room = rooms[0]
    if bind_talk:
        with db.get_db(config.db_path) as conn:
            db.add_room_binding(conn, room["token"], "talk", "talkref9")
    return room["id"], room["token"]


def _add_message(config, room_token, role="assistant", external_ids=None, body="x"):
    with db.get_db(config.db_path) as conn:
        return db.add_message(
            conn, room_token, role=role, body=body,
            origin_surface="web", task_id=None, external_ids=external_ids,
        )


def _cursor(config, room_token, username="alice"):
    with db.get_db(config.db_path) as conn:
        return db.get_room_read_state(conn, room_token, "web", username)


@_needs_web_deps
class TestWebToTalkPush:
    async def test_cursor_advance_pushes_once(self, tmp_path, keyed, monkeypatch):
        config = _make_config(tmp_path, sync_interval=0)  # pull off; push only
        web_tokens.store_tokens(config.db_path, "alice", "live-at", "rt", 3600)
        constructed, instance = _mock_talk_client(monkeypatch)
        app = _patch_app(config)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com",
        ) as client:
            cookies = await _login(client)
            room_id, room_token = await _default_room(client, config, cookies)
            _add_message(config, room_token)  # something unread

            resp = await client.post(
                f"/istota/api/chat/rooms/{room_id}/read",
                cookies=cookies, headers=ORIGIN,
            )
            assert resp.status_code == 200
            await _drain_bg()

        instance.mark_conversation_read.assert_awaited_once_with("talkref9")
        assert constructed[0]["bearer_token"] == "live-at"

    async def test_unchanged_cursor_no_push(self, tmp_path, keyed, monkeypatch):
        config = _make_config(tmp_path, sync_interval=0)
        web_tokens.store_tokens(config.db_path, "alice", "live-at", "rt", 3600)
        constructed, instance = _mock_talk_client(monkeypatch)
        app = _patch_app(config)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com",
        ) as client:
            cookies = await _login(client)
            room_id, room_token = await _default_room(client, config, cookies)
            _add_message(config, room_token)
            await client.post(
                f"/istota/api/chat/rooms/{room_id}/read",
                cookies=cookies, headers=ORIGIN,
            )
            await _drain_bg()
            instance.mark_conversation_read.reset_mock()

            # visibilitychange spam: no new messages, cursor unchanged.
            resp = await client.post(
                f"/istota/api/chat/rooms/{room_id}/read",
                cookies=cookies, headers=ORIGIN,
            )
            assert resp.status_code == 200
            await _drain_bg()

        instance.mark_conversation_read.assert_not_awaited()

    async def test_unbound_room_no_push(self, tmp_path, keyed, monkeypatch):
        config = _make_config(tmp_path, sync_interval=0)
        web_tokens.store_tokens(config.db_path, "alice", "live-at", "rt", 3600)
        constructed, instance = _mock_talk_client(monkeypatch)
        app = _patch_app(config)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com",
        ) as client:
            cookies = await _login(client)
            room_id, room_token = await _default_room(
                client, config, cookies, bind_talk=False,
            )
            _add_message(config, room_token)
            await client.post(
                f"/istota/api/chat/rooms/{room_id}/read",
                cookies=cookies, headers=ORIGIN,
            )
            await _drain_bg()

        instance.mark_conversation_read.assert_not_awaited()

    async def test_feature_off_no_push(self, tmp_path, keyed, monkeypatch):
        config = _make_config(tmp_path, token_storage="ephemeral", sync_interval=0)
        constructed, instance = _mock_talk_client(monkeypatch)
        app = _patch_app(config)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com",
        ) as client:
            cookies = await _login(client)
            room_id, room_token = await _default_room(client, config, cookies)
            _add_message(config, room_token)
            await client.post(
                f"/istota/api/chat/rooms/{room_id}/read",
                cookies=cookies, headers=ORIGIN,
            )
            await _drain_bg()

        instance.mark_conversation_read.assert_not_awaited()

    async def test_read_all_pushes_moved_rooms(self, tmp_path, keyed, monkeypatch):
        config = _make_config(tmp_path, sync_interval=0)
        web_tokens.store_tokens(config.db_path, "alice", "live-at", "rt", 3600)
        constructed, instance = _mock_talk_client(monkeypatch)
        app = _patch_app(config)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com",
        ) as client:
            cookies = await _login(client)
            room_id, room_token = await _default_room(client, config, cookies)
            _add_message(config, room_token)

            resp = await client.post(
                "/istota/api/chat/rooms/read-all", cookies=cookies, headers=ORIGIN,
            )
            assert resp.status_code == 200
            assert resp.json()["updated"] == 1
            await _drain_bg()

        instance.mark_conversation_read.assert_awaited_once_with("talkref9")


@_needs_web_deps
class TestTalkToWebPull:
    async def _setup(self, tmp_path, keyed, monkeypatch, *, unread=0,
                     sync_interval=60):
        """A Talk-bound room with a Talk-synced message and a newer web-only
        system message; cursor at 0. Returns (config, app, ids…)."""
        config = _make_config(tmp_path, sync_interval=sync_interval)
        web_tokens.store_tokens(config.db_path, "alice", "live-at", "rt", 3600)
        constructed, instance = _mock_talk_client(
            monkeypatch,
            conversations=[{"token": "talkref9", "unreadMessages": unread}],
        )
        app = _patch_app(config)
        return config, app, constructed, instance

    async def test_pull_advances_to_talk_synced_cap(self, tmp_path, keyed, monkeypatch):
        config, app, constructed, instance = await self._setup(
            tmp_path, keyed, monkeypatch,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com",
        ) as client:
            cookies = await _login(client)
            room_id, room_token = await _default_room(client, config, cookies)
            # Cursor is seeded at current max (0 messages) on first listing.
            synced = _add_message(
                config, room_token, role="assistant",
                external_ids={"talk": "500"},
            )
            web_only = _add_message(config, room_token, role="system")

            import istota.web_app as mod
            mod._talk_read_pull_state.clear()  # first listing used the slot
            resp = await client.get("/istota/api/chat/rooms", cookies=cookies)
            rooms = resp.json()["rooms"]

        # Cursor advanced exactly to the Talk-synced message, not past the
        # web-only system row — which therefore stays unread.
        assert _cursor(config, room_token) == synced
        assert synced < web_only
        assert rooms[0]["unread_count"] == 1

    async def test_pull_ignores_rooms_with_talk_unread(self, tmp_path, keyed, monkeypatch):
        config, app, constructed, instance = await self._setup(
            tmp_path, keyed, monkeypatch, unread=3,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com",
        ) as client:
            cookies = await _login(client)
            room_id, room_token = await _default_room(client, config, cookies)
            _add_message(
                config, room_token, role="assistant",
                external_ids={"talk": "500"},
            )
            import istota.web_app as mod
            mod._talk_read_pull_state.clear()
            await client.get("/istota/api/chat/rooms", cookies=cookies)

        assert _cursor(config, room_token) == 0

    async def test_throttle_one_fetch_per_interval(self, tmp_path, keyed, monkeypatch):
        config, app, constructed, instance = await self._setup(
            tmp_path, keyed, monkeypatch,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com",
        ) as client:
            cookies = await _login(client)
            await client.get("/istota/api/chat/rooms", cookies=cookies)
            n_after_first = instance.list_conversations.await_count
            await client.get("/istota/api/chat/rooms", cookies=cookies)
            await client.get("/istota/api/chat/rooms", cookies=cookies)

        assert n_after_first == 1
        assert instance.list_conversations.await_count == 1  # throttled

    async def test_interval_zero_disables_pull(self, tmp_path, keyed, monkeypatch):
        config, app, constructed, instance = await self._setup(
            tmp_path, keyed, monkeypatch, sync_interval=0,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com",
        ) as client:
            cookies = await _login(client)
            await client.get("/istota/api/chat/rooms", cookies=cookies)

        instance.list_conversations.assert_not_awaited()

    async def test_no_stamped_rows_no_advance(self, tmp_path, keyed, monkeypatch):
        # A room with no Talk-synced canonical rows: cap is 0 → cursor stays.
        config, app, constructed, instance = await self._setup(
            tmp_path, keyed, monkeypatch,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com",
        ) as client:
            cookies = await _login(client)
            room_id, room_token = await _default_room(client, config, cookies)
            _add_message(config, room_token, role="assistant")  # unstamped
            import istota.web_app as mod
            mod._talk_read_pull_state.clear()
            await client.get("/istota/api/chat/rooms", cookies=cookies)

        assert _cursor(config, room_token) == 0


class TestRoomMaxTalkSyncedMessageId:
    def test_helper(self, tmp_path):
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.register_room(conn, "r", "alice", origin="web")
            assert db.room_max_talk_synced_message_id(conn, "r") == 0
            m1 = db.add_message(
                conn, "r", role="user", body="a", origin_surface="web",
                external_ids={"talk": "100"},
            )
            m2 = db.add_message(
                conn, "r", role="assistant", body="b", origin_surface="web",
                external_ids={"talk": "101"},
            )
            m3 = db.add_message(
                conn, "r", role="system", body="c", origin_surface="web",
            )
            _ = db.add_message(
                conn, "r", role="user", body="d", origin_surface="web",
                external_ids={"matrix": "zzz"},
            )
            assert db.room_max_talk_synced_message_id(conn, "r") == m2
            assert db.room_max_message_id(conn, "r") > m2

    def test_mark_all_rooms_read_tokens(self, tmp_path):
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.register_room(conn, "r1", "alice", origin="web")
            db.register_room(conn, "r2", "alice", origin="web")
            db.add_message(conn, "r1", role="assistant", body="x", origin_surface="web")
            moved = db.mark_all_rooms_read_tokens(conn, "alice")
            assert moved == ["r1"]
            assert db.mark_all_rooms_read_tokens(conn, "alice") == []
