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


def test_the_web_producer_mints(db_path):
    with db.get_db(db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "Ideas")
        assert db.is_canonical_room_token(room.token)
        assert db.list_room_members(conn, room.token) == ["alice"]


@pytest.mark.parametrize("has_private_room", [False, True])
@pytest.mark.parametrize("kind", ["whisper", "confirmation", "proposal", "answer_notice"])
def test_no_private_delivery_creates_a_room(db_path, kind, has_private_room):
    """ISSUE-608: a private reply goes to a room the member already has, or
    the bell. Nothing is minted for it, whichever way it goes."""
    from istota.rooms.private_replies import deliver_private

    config = Config(db_path=db_path, users={"alice": UserConfig(), "bob": UserConfig()})
    with db.get_db(db_path) as conn:
        shared = db.create_web_chat_room(conn, "alice", "Family").token
        db.add_web_room_member(conn, shared, "bob")
        if has_private_room:
            db.create_web_chat_room(conn, "alice", "Mine")
        before = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
        delivery = deliver_private(conn, config, user_id="alice", about_token=shared,
                                   kind=kind, reference="t1", body="just for you")
        after = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
    assert after == before
    assert (delivery.dest is not None) is has_private_room


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


@pytest.mark.parametrize("command_name", ["memory", "export"])
async def test_talk_commands_read_canonical_room_data(config, tmp_path, command_name):
    from istota.commands import CommandContext, cmd_export, cmd_memory

    config.workspace_path = tmp_path
    with db.get_db(config.db_path) as conn:
        turn = record_inbound(conn, config, surface="talk", surface_ref="talkref1",
                              user_id="alice", text="Room question")
        db.update_task_status(conn, turn.task_id, "completed", result="Room answer")
        memory = tmp_path / "Channels" / turn.room_token / "CHANNEL.md"
        memory.parent.mkdir(parents=True)
        memory.write_text("Room notes")
        ctx = CommandContext(config=config, conn=conn, user_id="alice",
                             conversation_token="talkref1", surface="talk",
                             args="channel" if command_name == "memory" else "")
        if command_name == "memory":
            assert "Room notes" in await cmd_memory(ctx)
        else:
            assert "Exported 1 messages" in await cmd_export(ctx)
            export = tmp_path / "Users/alice/istota/exports/conversations" / f"{turn.room_token}.md"
            assert "Room question" in export.read_text()
        assert ctx.conversation_token == "talkref1"


@pytest.mark.parametrize("notice", ["expired", "ancient"])
def test_cleanup_notices_resolve_minted_talk_room(config, tmp_path, notice):
    from unittest.mock import patch
    from istota.scheduler import run_cleanup_checks

    config.nextcloud.url = "https://nc.example.com"
    config.temp_dir = tmp_path
    config.email.enabled = False
    config.talk.enabled = False
    with db.get_db(config.db_path) as conn:
        room = db.register_room(conn, None, "alice", origin="talk")
        db.add_room_binding(conn, room.token, "talk", "talkref1")
        task_id = db.create_task(conn, prompt="A request", user_id="alice",
                                 source_type="talk", conversation_token=room.token)
        if notice == "expired":
            db.set_task_confirmation(conn, task_id, "Proceed?")
        conn.execute("UPDATE tasks SET created_at = datetime('now', '-30 days'), "
                     "updated_at = datetime('now', '-30 days') WHERE id = ?", (task_id,))
    with patch("istota.scheduler.send_notification", return_value=True) as send:
        run_cleanup_checks(config)
    assert send.call_count == 1
    assert send.call_args.kwargs["conversation_token"] == "talkref1"
    if notice == "expired":
        with db.get_db(config.db_path) as conn:
            row = conn.execute("SELECT room_token FROM notifications WHERE source = 'task_alert'").fetchone()
            assert row["room_token"] == room.token


@pytest.mark.parametrize("builder", [plain_talk_room, promoted_room])
def test_talk_context_reads_native_cache_with_minted_identity(config, builder):
    from istota.executor import _build_talk_api_context

    with db.get_db(config.db_path) as conn:
        room = builder(conn, "alice")
        db.upsert_talk_messages(conn, room.talk_ref, [{
            "id": 10, "actorId": "alice", "actorDisplayName": "Alice",
            "actorType": "users", "message": "Cached room history",
            "messageType": "comment", "timestamp": 10,
        }])
        task = db.Task(id=1, user_id="alice", prompt="Next question",
                       source_type="talk", conversation_token=room.canonical, status="running")
        context, _ = _build_talk_api_context(task, config, conn)
        assert context is not None and "Cached room history" in context


@pytest.mark.parametrize("source", ["memory", "talk"])
async def test_search_scopes_native_and_canonical_hits_to_minted_room(config, source):
    from unittest.mock import AsyncMock, patch
    from istota.commands import CommandContext, cmd_search

    with db.get_db(config.db_path) as conn:
        room = db.register_room(conn, None, "alice", origin="talk")
        db.add_room_binding(conn, room.token, "talk", "talkref1")
        ctx = CommandContext(config=config, conn=conn, user_id="alice",
                             conversation_token="talkref1", surface="talk", args="history")
        hit = {"summary": "Room history found", "conversation_token":
               room.token if source == "memory" else "talkref1", "talk_message_id": 10}
        with patch("istota.commands._search_memory", return_value=[hit] if source == "memory" else []) as mem, \
             patch("istota.commands._search_talk_api", new=AsyncMock(return_value=[hit] if source == "talk" else [])):
            result = await cmd_search(ctx)
        assert "Room history found" in result
        assert mem.call_args.kwargs["conversation_token"] == room.token
        assert ctx.result_data["results"][0]["room_token"] == room.token
        assert "/call/talkref1#message_10" in ctx.result_data["results"][0]["talk_link"]
