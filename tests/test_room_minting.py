"""New room identities never double as external surface addresses."""
from types import SimpleNamespace

import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.transport.ingest import record_inbound
from istota.transport.routing import talk_channel_for_task, origin_descriptor
from istota.transport.talk import inbound
from tests.support.rooms import plain_talk_room, promoted_room
from tests.support.talk_double import UnknownTalkRoom


@pytest.fixture
def config(db_path):
    return Config(db_path=db_path, users={"alice": UserConfig()})


def test_registration_mints_and_keeps_explicit_legacy_tokens(db_path):
    with db.get_db(db_path) as conn:
        room = db.register_room(conn, None, "alice", origin="talk")
        assert db.is_canonical_room_token(room.token)
        assert db.is_room_member(conn, room.token, "alice")
        assert db.register_room(conn, "legacy", "alice", origin="talk").token == "legacy"


def test_web_and_side_room_producers_mint(db_path):
    with db.get_db(db_path) as conn:
        parent = db.create_web_chat_room(conn, "alice", "Ideas")
        side = db.ensure_side_room(conn, parent.token, "alice")
        assert db.is_canonical_room_token(parent.token)
        assert db.is_canonical_room_token(side.token)
        assert side.token != parent.token
        assert db.ensure_side_room(conn, parent.token, "alice").token == side.token
        assert db.list_room_members(conn, side.token) == ["alice"]


def test_talk_ingest_mints_once_but_private_email_keeps_thread_hash(config):
    with db.get_db(config.db_path) as conn:
        first = record_inbound(conn, config, surface="talk", surface_ref="talkref1",
                               user_id="alice", text="hi", platform_message_id=1)
        second = record_inbound(conn, config, surface="talk", surface_ref="talkref1",
                                user_id="alice", text="again", platform_message_id=2)
        assert db.is_canonical_room_token(first.room_token)
        assert second.room_token == first.room_token
        assert db.get_room(conn, "talkref1") is None
        assert db.resolve_room_token(conn, "talk", "talkref1") == first.room_token
        assert db.get_task(conn, first.task_id).conversation_token == first.room_token
        assert len(db.get_messages(conn, first.room_token)) == 2
        email = record_inbound(conn, config, surface="email", surface_ref="a" * 16,
                               user_id="alice", text="mail")
        assert email.room_token == "a" * 16
        assert db.get_task(conn, email.task_id).conversation_token == "a" * 16
        assert db.get_room(conn, email.room_token) is None


def test_poller_rechecks_binding_after_read_phase(config):
    with db.get_db(config.db_path) as conn:
        conv = {"token": "talkref1", "type": 1, "name": "alice"}
        plan = inbound._plan_room_pass(conn, config, [conv], {}, {})[0]
        plan.participants = []
        plan.needs_cursor_init = False
        room = db.create_web_chat_room(conn, "alice", "Ideas")
        db.add_room_binding(conn, room.token, "talk", "talkref1")
        inbound._apply_room_pass(conn, config, None, [plan], full_sweep=True, open_poll=lambda *a: None)
        assert [r.token for r in db.list_rooms(conn, "alice")] == [room.token]


def test_poller_mints_and_binds_a_room_with_no_messages(config):
    with db.get_db(config.db_path) as conn:
        conv = {"token": "talkref1", "type": 1, "name": "alice"}
        plan = inbound._plan_room_pass(conn, config, [conv], {}, {})[0]
        plan.participants = []
        plan.needs_cursor_init = False
        inbound._apply_room_pass(conn, config, None, [plan], full_sweep=True, open_poll=lambda *a: None)
        token = db.resolve_room_token(conn, "talk", "talkref1")
        assert db.is_canonical_room_token(token)
        assert db.get_room(conn, "talkref1") is None
        assert db.list_room_members(conn, token) == ["alice"]


@pytest.mark.parametrize("builder", [plain_talk_room, promoted_room])
async def test_canonical_misdelivery_is_refused_for_every_room_shape(db_path, fake_talk, builder):
    with db.get_db(db_path) as conn:
        room = builder(conn, "alice")
    assert db.is_canonical_room_token(room.canonical)
    await fake_talk.send_message(room.talk_ref, "right")
    with pytest.raises(UnknownTalkRoom):
        await fake_talk.send_message(room.canonical, "wrong")


@pytest.mark.parametrize("source_type", ["talk", "web", "email", "scheduled"])
def test_talk_fallback_never_returns_a_minted_room(config, source_type):
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "Ideas")
    task = SimpleNamespace(conversation_token=room.token, source_type=source_type,
                           talk_delivery_token=None, user_id="alice")
    assert talk_channel_for_task(config, task) is None
    with db.get_db(config.db_path) as conn:
        db.add_room_binding(conn, room.token, "talk", "talkref1")
    assert talk_channel_for_task(config, task) == "talkref1"


def test_unregistered_talk_dm_still_delivers(config):
    task = SimpleNamespace(conversation_token="rawdm123", source_type="talk",
                           talk_delivery_token=None, user_id="alice")
    assert talk_channel_for_task(config, task) == "rawdm123"


def test_descriptor_without_database_never_labels_minted_room_as_talk(config):
    task = SimpleNamespace(conversation_token=db.mint_room_token(), source_type="email",
                           talk_delivery_token=None, user_id="alice")
    assert origin_descriptor(task) == f"room:{task.conversation_token}"


def test_expiry_notice_never_treats_minted_identity_as_talk():
    from istota.scheduler import _confirmation_notice_token

    assert _confirmation_notice_token({"conversation_token": db.mint_room_token()}) is None


@pytest.mark.parametrize("collision", ["legacy_room", "deleted_alias"])
def test_native_talk_binding_wins_over_legacy_identity_collision(config, collision):
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "Current")
        db.add_room_binding(conn, room.token, "talk", "talkref1")
        if collision == "legacy_room":
            db.register_room(conn, "talkref1", "alice", origin="web")
        else:
            conn.execute("INSERT INTO room_token_migration (old_token, new_token, migrated_at) VALUES (?, ?, '2026-01-01')",
                         ("talkref1", "rm_deleted"))
        turn = record_inbound(conn, config, surface="talk", surface_ref="talkref1",
                              user_id="alice", text="hi")
        assert turn.room_token == room.token
        plan = inbound._plan_room_pass(conn, config,
                                      [{"token": "talkref1", "type": 1, "name": "alice"}], {}, {})[0]
        assert plan.canonical == room.token
