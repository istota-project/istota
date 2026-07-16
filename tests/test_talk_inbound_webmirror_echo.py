"""Tests for echo prevention of post-as-user mirrored Talk messages (Stage 3
of the user-scoped Nextcloud OAuth spec): the referenceId fast-path in the
poller and the external-ids ledger backstop in record_inbound."""

import pytest
from unittest.mock import AsyncMock, patch

from istota import db
from istota.config import Config, NextcloudConfig, SchedulerConfig, TalkConfig, UserConfig
from istota.transport.talk import inbound as _talk_poller_mod
from istota.transport.talk.inbound import poll_talk_conversations
from istota.transport._types import WEBMIRROR_REF_PREFIX


@pytest.fixture(autouse=True)
def _reset_poller_caches():
    _talk_poller_mod._participant_cache.clear()
    _talk_poller_mod._conversation_cache = None
    _talk_poller_mod._dm_token_cache.clear()
    yield
    _talk_poller_mod._participant_cache.clear()
    _talk_poller_mod._conversation_cache = None
    _talk_poller_mod._dm_token_cache.clear()


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(db_path, tmp_path):
    config = Config()
    config.db_path = db_path
    config.temp_dir = tmp_path / "temp"
    config.temp_dir.mkdir(exist_ok=True)
    config.talk = TalkConfig(enabled=True, bot_username="istota")
    config.nextcloud = NextcloudConfig(
        url="https://nc.test", username="istota", app_password="pass",
    )
    config.users = {"alice": UserConfig()}
    config.scheduler = SchedulerConfig()
    return config


def _msg(id=100, actor_id="alice", message="hello", reference_id=None):
    msg = {
        "id": id,
        "actorId": actor_id,
        "actorType": "users",
        "message": message,
        "messageType": "comment",
        "messageParameters": {},
        "timestamp": 1700000000,
    }
    if reference_id is not None:
        msg["referenceId"] = reference_id
    return msg


async def _poll(config, messages, token="dmroom"):
    conversations = [{"token": token, "type": 1, "name": "alice"}]
    with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
        instance = MockClient.return_value
        instance.list_conversations = AsyncMock(return_value=conversations)
        instance.poll_messages = AsyncMock(return_value=messages)
        instance.send_message = AsyncMock()
        return await poll_talk_conversations(config)


class TestReferenceIdFastPath:
    @pytest.mark.asyncio
    async def test_webmirror_message_skipped(self, config):
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "dmroom", 50)

        created = await _poll(config, [
            _msg(id=100, reference_id=f"{WEBMIRROR_REF_PREFIX}42"),
        ])

        assert created == []
        with db.get_db(config.db_path) as conn:
            # No task created…
            assert db.list_tasks(conn) == []
            # …no canonical message row…
            n = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
            assert n == 0
            # …but the poll cursor advanced past the echo…
            assert db.get_talk_poll_state(conn, "dmroom") == 100
            # …and the context cache still holds the turn (it's legitimately
            # part of the conversation).
            assert db.has_cached_talk_messages(conn, "dmroom")

    @pytest.mark.asyncio
    async def test_other_reference_ids_not_skipped(self, config):
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "dmroom", 50)

        created = await _poll(config, [
            _msg(id=101, reference_id="istota:task:7:result"),
        ])

        # A non-webmirror referenceId is a normal message → task created.
        assert len(created) == 1

    @pytest.mark.asyncio
    async def test_plain_message_still_creates_task(self, config):
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "dmroom", 50)

        created = await _poll(config, [_msg(id=102)])

        assert len(created) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, created[0])
            assert task.prompt == "hello"


class TestExternalIdLedgerBackstop:
    @pytest.mark.asyncio
    async def test_stamped_message_dropped_without_reference_id(self, config):
        """The same mirrored message with its referenceId stripped (federation
        edge) is caught by record_inbound's external-ids echo check."""
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "dmroom", 50)
            db.register_room(conn, "dmroom", "alice", origin="talk")
            db.add_room_binding(conn, "dmroom", "talk", "dmroom")
            # The canonical user row the web POST stored, stamped with the
            # Talk id the mirror post received.
            db.add_message(
                conn, "dmroom", role="user", body="hello",
                origin_surface="web", task_id=999,
                external_ids={"talk": "100"},
            )

        created = await _poll(config, [_msg(id=100)])  # no referenceId

        assert created == []
        with db.get_db(config.db_path) as conn:
            assert db.list_tasks(conn) == []
            # No duplicate user row landed.
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE room_token='dmroom'"
            ).fetchone()["n"]
            assert n == 1

    @pytest.mark.asyncio
    async def test_inbound_talk_id_stamped_on_canonical_row(self, config):
        """record_inbound stamps the Talk message id on the stored user turn
        (feeds the Talk→web read-sync cursor cap)."""
        import json
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "dmroom", 50)

        created = await _poll(config, [_msg(id=123)])

        assert len(created) == 1
        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT external_ids FROM messages WHERE role='user'"
            ).fetchone()
            assert json.loads(row["external_ids"]) == {"talk": "123"}
