"""Stage 6 — "Also open in Talk" promote + Talk OCS room methods.

Promoting a web-origin room creates a real Nextcloud Talk conversation (via the
bot's OCS account), adds the requesting user, and writes a `talk` binding so the
room is now reachable from Talk mobile clients and the mirror fan-out pushes to
it. Rename propagates to the bound Talk conversation.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from istota import db
from istota.config import Config, NextcloudConfig


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


@pytest.fixture
def web_config(db_path):
    from istota import web_app
    cfg = Config()
    cfg.db_path = db_path
    cfg.nextcloud = NextcloudConfig(url="https://nc.example", username="bot", app_password="pw")
    web_app._config = cfg
    return cfg


# ---------------------------------------------------------------------------
# TalkClient OCS room methods
# ---------------------------------------------------------------------------


class TestTalkClientRoomMethods:
    @pytest.mark.asyncio
    async def test_create_conversation_posts_v4_room(self):
        from istota.talk import TalkClient
        cfg = Config()
        cfg.nextcloud = NextcloudConfig(url="https://nc.example", username="bot", app_password="pw")
        client = TalkClient(cfg)
        http = MagicMock()
        resp = MagicMock()
        resp.json.return_value = {"ocs": {"data": {"token": "newtok", "name": "Ideas"}}}
        resp.raise_for_status = MagicMock()
        http.post = AsyncMock(return_value=resp)
        with patch.object(client, "_ensure_open", AsyncMock(return_value=http)):
            data = await client.create_conversation("Ideas")
        assert data["token"] == "newtok"
        url = http.post.call_args[0][0]
        assert url.endswith("/ocs/v2.php/apps/spreed/api/v4/room")
        assert http.post.call_args[1]["json"] == {"roomType": 2, "roomName": "Ideas"}

    @pytest.mark.asyncio
    async def test_add_participant_posts_participants(self):
        from istota.talk import TalkClient
        cfg = Config()
        cfg.nextcloud = NextcloudConfig(url="https://nc.example", username="bot", app_password="pw")
        client = TalkClient(cfg)
        http = MagicMock()
        resp = MagicMock()
        resp.json.return_value = {"ocs": {"data": {}}}
        resp.raise_for_status = MagicMock()
        http.post = AsyncMock(return_value=resp)
        with patch.object(client, "_ensure_open", AsyncMock(return_value=http)):
            await client.add_participant("tok", "alice")
        assert http.post.call_args[0][0].endswith("/room/tok/participants")
        assert http.post.call_args[1]["json"] == {"newParticipant": "alice", "source": "users"}

    @pytest.mark.asyncio
    async def test_rename_conversation_puts_room(self):
        from istota.talk import TalkClient
        cfg = Config()
        cfg.nextcloud = NextcloudConfig(url="https://nc.example", username="bot", app_password="pw")
        client = TalkClient(cfg)
        http = MagicMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        http.put = AsyncMock(return_value=resp)
        with patch.object(client, "_ensure_open", AsyncMock(return_value=http)):
            await client.rename_conversation("tok", "New Name")
        assert http.put.call_args[0][0].endswith("/room/tok")
        assert http.put.call_args[1]["json"] == {"roomName": "New Name"}


# ---------------------------------------------------------------------------
# Promote
# ---------------------------------------------------------------------------


def _fake_talk_client():
    """A TalkClient whose OCS calls are mocked; create returns a fixed token."""
    fake = MagicMock()
    fake.create_conversation = AsyncMock(return_value={"token": "promoted-tok"})
    fake.add_participant = AsyncMock(return_value={})
    fake.send_message = AsyncMock(return_value={})
    fake.rename_conversation = AsyncMock()
    fake.aclose = AsyncMock()
    return fake


class TestPromote:
    @pytest.mark.asyncio
    async def test_promote_web_room_creates_talk_binding(self, web_config, db_path):
        from istota import web_app
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Ideas")
        fake = _fake_talk_client()
        with patch("istota.talk.TalkClient", return_value=fake):
            result = await web_app._chat_promote_to_talk("alice", room.id)
        assert result is not None
        assert result["talk_token"] == "promoted-tok"
        fake.create_conversation.assert_awaited_once()
        fake.add_participant.assert_awaited_once_with("promoted-tok", "alice")
        with db.get_db(db_path) as conn:
            binding = db.get_room_binding(conn, room.token, "talk")
        assert binding is not None and binding.surface_ref == "promoted-tok"

    @pytest.mark.asyncio
    async def test_binding_persists_when_add_participant_fails(self, web_config, db_path):
        # The orphan-room guard (MED1): the binding must be written immediately
        # after the OCS create, before the best-effort add_participant/seed
        # steps. If add_participant fails, the binding still exists, so a
        # re-promote is a no-op rather than spawning a second Talk room.
        from istota import web_app
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Ideas")
        fake = _fake_talk_client()
        fake.add_participant = AsyncMock(side_effect=RuntimeError("NC down"))
        with patch("istota.talk.TalkClient", return_value=fake):
            result = await web_app._chat_promote_to_talk("alice", room.id)
        assert result is not None
        with db.get_db(db_path) as conn:
            binding = db.get_room_binding(conn, room.token, "talk")
        assert binding is not None and binding.surface_ref == "promoted-tok"
        # A second promote attempt sees the binding and creates no new Talk room.
        fake2 = _fake_talk_client()
        with patch("istota.talk.TalkClient", return_value=fake2):
            again = await web_app._chat_promote_to_talk("alice", room.id)
        assert again is None
        fake2.create_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_promote_rejects_already_bound(self, web_config, db_path):
        from istota import web_app
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Ideas")
            db.add_room_binding(conn, room.token, "talk", "existing")
        fake = _fake_talk_client()
        with patch("istota.talk.TalkClient", return_value=fake):
            result = await web_app._chat_promote_to_talk("alice", room.id)
        assert result is None
        fake.create_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_promote_rejects_talk_origin_room(self, web_config, db_path):
        from istota import web_app
        with db.get_db(db_path) as conn:
            db.register_room(conn, "cpz", "alice", origin="talk", name="#x")
            handle = db.ensure_web_chat_handle(conn, "alice", "cpz", "#x")
        fake = _fake_talk_client()
        with patch("istota.talk.TalkClient", return_value=fake):
            result = await web_app._chat_promote_to_talk("alice", handle.id)
        assert result is None

    @pytest.mark.asyncio
    async def test_promote_unknown_room(self, web_config):
        from istota import web_app
        assert await web_app._chat_promote_to_talk("alice", 99999) is None


class TestTalkBindingLookup:
    def test_room_talk_binding(self, web_config, db_path):
        from istota import web_app
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Ideas")
            db.add_room_binding(conn, room.token, "talk", "ttok")
        assert web_app._room_talk_binding("alice", room.id) == "ttok"
        assert web_app._room_talk_binding("bob", room.id) is None
