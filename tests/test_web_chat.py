"""Tests for the web chat surface (Phase 1 backend)."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db
from istota.config import (
    Config,
    SiteConfig,
    UserConfig,
    WebChatConfig,
    WebConfig,
    load_config,
)
from istota.transport.registry import make_registry
from istota.transport.routing import plan_has_surface, resolve_delivery_plan

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


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    with db.get_db(db_path) as c:
        yield c


# ---------------------------------------------------------------------------
# DB layer: rooms + rate-limit counter
# ---------------------------------------------------------------------------


class TestWebChatRoomsDB:
    def test_ensure_default_creates_general(self, conn):
        room = db.ensure_default_web_chat_room(conn, "alice")
        assert room.name == "general"
        assert room.user_id == "alice"
        assert room.token.startswith("web-alice-")
        assert not room.archived

    def test_ensure_default_idempotent(self, conn):
        first = db.ensure_default_web_chat_room(conn, "alice")
        second = db.ensure_default_web_chat_room(conn, "alice")
        assert first.id == second.id

    def test_create_and_list_rooms_oldest_first(self, conn):
        db.create_web_chat_room(conn, "alice", "general")
        db.create_web_chat_room(conn, "alice", "ideas")
        rooms = db.list_web_chat_rooms(conn, "alice")
        assert [r.name for r in rooms] == ["general", "ideas"]

    def test_rooms_are_per_user(self, conn):
        db.create_web_chat_room(conn, "alice", "general")
        db.create_web_chat_room(conn, "bob", "general")
        assert len(db.list_web_chat_rooms(conn, "alice")) == 1
        assert len(db.list_web_chat_rooms(conn, "bob")) == 1

    def test_tokens_are_unique(self, conn):
        a = db.create_web_chat_room(conn, "alice", "one")
        b = db.create_web_chat_room(conn, "alice", "two")
        assert a.token != b.token

    def test_rename_room(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        updated = db.update_web_chat_room(conn, room.id, name="renamed")
        assert updated.name == "renamed"

    def test_archive_hides_from_default_list(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        db.update_web_chat_room(conn, room.id, archived=True)
        assert db.list_web_chat_rooms(conn, "alice") == []
        assert len(db.list_web_chat_rooms(conn, "alice", include_archived=True)) == 1

    def test_get_by_token(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        found = db.get_web_chat_room_by_token(conn, room.token)
        assert found.id == room.id

    def test_count_recent_web_tasks(self, conn):
        room = db.create_web_chat_room(conn, "alice", "general")
        for _ in range(3):
            db.create_task(
                conn, prompt="hi", user_id="alice", source_type="web",
                conversation_token=room.token, output_target="web",
            )
        # A non-web task for the same user must not be counted.
        db.create_task(conn, prompt="x", user_id="alice", source_type="talk")
        assert db.count_recent_web_tasks(conn, "alice", 300) == 3


# ---------------------------------------------------------------------------
# Delivery routing: web is a stream surface (no Talk/email push)
# ---------------------------------------------------------------------------


class TestWebDeliveryRouting:
    def _config(self, tmp_path):
        return Config(db_path=tmp_path / "istota.db")

    def test_web_output_target_resolves_to_stream(self, tmp_path):
        config = self._config(tmp_path)
        task = db.Task(
            id=1, status="completed", source_type="web", user_id="alice",
            prompt="hi", conversation_token="web-alice-abc", output_target="web",
        )
        plan = resolve_delivery_plan(config, task, make_registry(config))
        assert plan_has_surface(plan, "web")
        assert not plan_has_surface(plan, "talk")
        assert not plan_has_surface(plan, "email")
        assert all(d.kind == "stream" for d in plan)

    def test_web_default_plan_when_target_unset(self, tmp_path):
        config = self._config(tmp_path)
        task = db.Task(
            id=1, status="completed", source_type="web", user_id="alice",
            prompt="hi", conversation_token="web-alice-abc", output_target=None,
        )
        plan = resolve_delivery_plan(config, task, make_registry(config))
        assert plan_has_surface(plan, "web")
        assert not plan_has_surface(plan, "talk")


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestWebChatConfig:
    def test_defaults(self):
        chat = WebChatConfig()
        assert chat.max_prompt_chars == 32000
        assert chat.rate_limit_messages == 30
        assert "pdf" in chat.attachment_extensions

    def test_parsed_from_toml(self, tmp_path):
        toml = tmp_path / "config.toml"
        toml.write_text(
            "[web]\nenabled = true\n\n"
            "[web.chat]\nmax_prompt_chars = 1000\nrate_limit_messages = 5\n"
        )
        config = load_config(toml)
        assert config.web.chat.max_prompt_chars == 1000
        assert config.web.chat.rate_limit_messages == 5
        # Untouched knobs keep defaults.
        assert config.web.chat.max_attachment_mb == 25


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


def _make_config(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(
        db_path=db_path,
        nextcloud_mount_path=tmp_path / "mount",
        site=SiteConfig(enabled=True, hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice"),
               "bob": UserConfig(display_name="Bob")},
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


def _patch_app(config):
    import istota.web_app as mod
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    return mod.app


async def _login(client, username):
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


@pytest.fixture
async def chat_client(tmp_path):
    config = _make_config(tmp_path)
    app = _patch_app(config)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


@_needs_web_deps
class TestChatRoomsApi:
    async def test_rooms_requires_auth(self, chat_client):
        resp = await chat_client.get("/istota/api/chat/rooms")
        assert resp.status_code == 401

    async def test_list_rooms_autocreates_general(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/rooms", cookies=cookies)
        assert resp.status_code == 200
        rooms = resp.json()["rooms"]
        assert len(rooms) == 1
        assert rooms[0]["name"] == "general"

    async def test_create_room(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.post(
            "/istota/api/chat/rooms", json={"name": "ideas"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "ideas"

    async def test_rename_room(self, chat_client):
        cookies = await _login(chat_client, "alice")
        created = (await chat_client.post(
            "/istota/api/chat/rooms", json={"name": "old"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )).json()
        resp = await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}", json={"name": "new"},
            cookies=cookies, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "new"

    async def test_cannot_touch_other_users_room(self, chat_client):
        alice = await _login(chat_client, "alice")
        created = (await chat_client.post(
            "/istota/api/chat/rooms", json={"name": "secret"}, cookies=alice,
            headers={"origin": "https://example.com"},
        )).json()
        bob = await _login(chat_client, "bob")
        resp = await chat_client.patch(
            f"/istota/api/chat/rooms/{created['id']}", json={"name": "x"},
            cookies=bob, headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 404


@_needs_web_deps
class TestChatMessagesApi:
    async def _room(self, client, cookies):
        return (await client.get("/istota/api/chat/rooms", cookies=cookies)).json()["rooms"][0]

    async def test_send_creates_web_task(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "what's on my calendar?"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] is not None
        assert "stream_url" in body
        # The task is a source_type=web task on the room token.
        import istota.web_app as mod
        with db.get_db(mod._config.db_path) as c:
            task = db.get_task(c, body["task_id"])
        assert task.source_type == "web"
        assert task.output_target == "web"
        assert task.conversation_token == room["token"]

    async def test_empty_text_rejected(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "   "}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 400

    async def test_history_round_trip(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "hello there"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        resp = await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][0]["text"] == "hello there"
        assert data["active_task"] is not None  # task is pending

    async def test_rate_limit_returns_429(self, chat_client):
        import istota.web_app as mod
        mod._config.web.chat.rate_limit_messages = 2
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        for _ in range(2):
            ok = await chat_client.post(
                f"/istota/api/chat/rooms/{room['id']}/messages",
                json={"text": "hi"}, cookies=cookies,
                headers={"origin": "https://example.com"},
            )
            assert ok.status_code == 200
        blocked = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "hi"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers
        mod._config.web.chat.rate_limit_messages = 30

    async def test_command_runs_inline_no_task(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._room(chat_client, cookies)
        resp = await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "!help"}, cookies=cookies,
            headers={"origin": "https://example.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] is None
        assert "inline_result" in body
