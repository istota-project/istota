"""Stage 3 — inbound storage via the shared `record_inbound` helper.

`record_inbound` is the single choke point both `ingest_message` (Talk/email)
and the web POST path call: resolve canonical room token via `room_bindings`,
lazily auto-register an unknown room surface, echo-check, store the user message
into the canonical `messages` store, and create the task.
"""

import pytest

from istota import db
from istota.config import Config
from istota.transport import IncomingMessage, ingest_message
from istota.transport.ingest import record_inbound


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(db_path):
    cfg = Config()
    cfg.db_path = db_path
    return cfg


class TestRecordInboundTalk:
    def test_unknown_talk_room_auto_registers(self, config, db_path):
        with db.get_db(db_path) as conn:
            token, task_id = record_inbound(
                conn, config, surface="talk", surface_ref="cpzpcfx2",
                user_id="alice", text="hi", channel_name="#istota",
            )
        assert token == "cpzpcfx2"
        with db.get_db(db_path) as conn:
            room = db.get_room(conn, "cpzpcfx2")
            assert room is not None
            assert room.origin == "talk"
            assert room.name == "#istota"
            assert db.resolve_room_token(conn, "talk", "cpzpcfx2") == "cpzpcfx2"
            # user message stored
            msgs = db.get_messages(conn, "cpzpcfx2")
            assert [(m.role, m.body, m.task_id) for m in msgs] == [("user", "hi", task_id)]
            assert db.get_task(conn, task_id).conversation_token == "cpzpcfx2"

    def test_known_room_not_duplicated(self, config, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "cpzpcfx2", "alice", origin="talk", name="#istota")
            db.add_room_binding(conn, "cpzpcfx2", "talk", "cpzpcfx2")
        with db.get_db(db_path) as conn:
            record_inbound(
                conn, config, surface="talk", surface_ref="cpzpcfx2",
                user_id="alice", text="hi", channel_name="#renamed",
            )
        with db.get_db(db_path) as conn:
            rooms = db.list_rooms(conn, "alice")
            assert len(rooms) == 1  # not duplicated
            # Talk-side rename flows back: a Talk-origin room's name tracks the
            # conversation's current displayName.
            assert rooms[0].name == "#renamed"

    def test_idempotent_user_message_on_duplicate_poll(self, config, db_path):
        msg_args = dict(
            surface="talk", surface_ref="room1", user_id="alice",
            text="hi", platform_message_id=555,
        )
        with db.get_db(db_path) as conn:
            _, first = record_inbound(conn, config, **msg_args)
        with db.get_db(db_path) as conn:
            _, second = record_inbound(conn, config, **msg_args)
        assert first == second  # create_task dedups
        with db.get_db(db_path) as conn:
            msgs = db.get_messages(conn, "room1")
        assert len(msgs) == 1  # user message not duplicated


class TestRecordInboundBinding:
    def test_resolves_canonical_token_via_binding(self, config, db_path):
        # A web-origin room later bound to a Talk conversation; a Talk inbound
        # on that conversation must resolve to the canonical web token.
        with db.get_db(db_path) as conn:
            db.register_room(conn, "web-alice-xyz", "alice", origin="web", name="Chat")
            db.add_room_binding(conn, "web-alice-xyz", "web", "web-alice-xyz")
            db.add_room_binding(conn, "web-alice-xyz", "talk", "talktoken9")
        with db.get_db(db_path) as conn:
            token, task_id = record_inbound(
                conn, config, surface="talk", surface_ref="talktoken9",
                user_id="alice", text="from talk",
            )
        assert token == "web-alice-xyz"
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)
            assert task.conversation_token == "web-alice-xyz"
            msgs = db.get_messages(conn, "web-alice-xyz")
            assert msgs[0].origin_surface == "talk"


class TestRecordInboundEmail:
    def test_email_creates_no_room(self, config, db_path):
        with db.get_db(db_path) as conn:
            token, task_id = record_inbound(
                conn, config, surface="email", surface_ref="thread-token",
                user_id="bob", text="email body", source_type="email",
            )
        assert token == "thread-token"
        with db.get_db(db_path) as conn:
            assert db.get_room(conn, "thread-token") is None
            assert db.get_messages(conn, "thread-token") == []
            assert db.get_task(conn, task_id).source_type == "email"


class TestRecordInboundEcho:
    def test_known_echo_dropped(self, config, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk")
            db.add_room_binding(conn, "room1", "talk", "room1")
            # record a prior mirrored assistant message carrying a Talk post id
            mid = db.add_message(
                conn, "room1", role="assistant", body="bot reply",
                origin_surface="web", task_id=1,
            )
            db.set_message_external_id(conn, mid, "talk", "8888")
        with db.get_db(db_path) as conn:
            token, task_id = record_inbound(
                conn, config, surface="talk", surface_ref="room1",
                user_id="alice", text="bot reply", platform_message_id=8888,
                external_id="8888",
            )
        assert token == "room1"
        assert task_id is None  # dropped, no task created
        with db.get_db(db_path) as conn:
            # no new user message
            roles = [m.role for m in db.get_messages(conn, "room1")]
            assert roles == ["assistant"]


class TestIngestMessageStillWorks:
    def test_talk_ingest_stores_user_message(self, config, db_path):
        msg = IncomingMessage(
            user_id="alice", text="hello", source_type="talk",
            surface="talk", channel_token="roomZ", platform_message_id=7,
            channel_name="My Room",
        )
        with db.get_db(db_path) as conn:
            task_id = ingest_message(conn, config, msg)
        with db.get_db(db_path) as conn:
            assert db.get_room(conn, "roomZ").name == "My Room"
            assert [m.body for m in db.get_messages(conn, "roomZ")] == ["hello"]
            assert db.get_task(conn, task_id).conversation_token == "roomZ"

    def test_email_ingest_unchanged(self, config, db_path):
        msg = IncomingMessage(
            user_id="bob", text="hi", source_type="email",
            surface="email", channel_token="thread1",
        )
        with db.get_db(db_path) as conn:
            task_id = ingest_message(conn, config, msg)
        with db.get_db(db_path) as conn:
            assert db.get_room(conn, "thread1") is None
            assert db.get_task(conn, task_id).source_type == "email"
