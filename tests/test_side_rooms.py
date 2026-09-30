"""Side rooms (multiplayer D4, umbrella Stage 10).

A side room is one member's private companion of a shared room. These pin the
schema and its migration, lazy creation, pinned delivery, the parent transcript
as untrusted context, the `room whisper` and held `room post` verbs on the relay
request table, and the routing of a shared-room confirmation to the principal's
side room.
"""
import asyncio
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from istota import confirmations, db, side_rooms
from istota import whatsapp_requests as requests
from istota.config import Config, NextcloudConfig, TalkConfig, UserConfig
from istota.whatsapp_requests import RequestError

from .support.rooms import plain_talk_room
from .support.talk_double import FakeTalkClient, talk_bot_client

PARTICIPANTS_ALICE = [{"actorType": "users", "actorId": "alice"},
                      {"actorType": "users", "actorId": "bot"}]


def _config(tmp_path):
    path = tmp_path / "state.db"
    db.init_db(path)
    return Config(
        db_path=path,
        temp_dir=tmp_path / "temp",
        nextcloud=NextcloudConfig(url="https://cloud.example.com", username="bot",
                                  app_password="secret"),
        talk=TalkConfig(enabled=True, bot_username="bot"),
        users={"alice": UserConfig(display_name="Alice"),
               "bob": UserConfig(display_name="Bob")},
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Alice and Bob in a shared web room, a Talk double and a participants stub."""
    config = _config(tmp_path)
    with db.get_db(config.db_path) as conn:
        shared = db.create_web_chat_room(conn, "alice", "Family").token
        db.add_web_room_member(conn, shared, "bob")
    talk = FakeTalkClient(config.db_path)
    participants = AsyncMock(return_value=PARTICIPANTS_ALICE)
    monkeypatch.setattr("istota.talk.TalkClient.get_participants", participants)
    with patch("istota.transport.talk.get_talk_client", talk_bot_client(talk)):
        yield dict(config=config, shared=shared, talk=talk, participants=participants)


def _running_task(conn, user, token, *, prompt="hello", source_type="web"):
    ident = db.create_task(conn, user_id=user, source_type=source_type,
                           prompt=prompt, conversation_token=token)
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
    return ident


def _rows(config, sql, params=()):
    with db.get_db(config.db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _private_talk_room(conn, user):
    """The user's own Talk conversation with the bot, as the registry knows it."""
    shape = plain_talk_room(conn, user, name="Talk")
    return shape.talk_ref


# ---------------------------------------------------------------------------
# Schema and migration
# ---------------------------------------------------------------------------

_PRE_STAGE_REQUESTS = """CREATE TABLE whatsapp_skill_requests (
    id TEXT PRIMARY KEY,
    requester_user_id TEXT NOT NULL,
    origin_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    request_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('self_send', 'relay_question')),
    recipient_user_id TEXT NOT NULL,
    relay_id TEXT UNIQUE,
    text TEXT,
    content_hash TEXT NOT NULL,
    service_body TEXT,
    service_hash TEXT NOT NULL,
    template_body TEXT,
    template_hash TEXT,
    preview TEXT,
    preview_digest TEXT,
    provider TEXT NOT NULL,
    binding_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('held','queued','sending','sent','uncertain','failed','cancelled','expired')),
    approved_at TEXT,
    approved_digest TEXT,
    queue_deadline TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at TEXT,
    content_cleared_at TEXT,
    error_code TEXT,
    UNIQUE (requester_user_id, origin_task_id, request_key)
)"""


def _pre_stage_db(tmp_path):
    """A database as the tree left it before this stage."""
    old = tmp_path / "old.db"
    db.init_db(old)
    conn = sqlite3.connect(old)
    try:
        conn.execute("DROP INDEX idx_rooms_side")
        conn.execute("ALTER TABLE rooms DROP COLUMN side_of")
        conn.execute("ALTER TABLE rooms DROP COLUMN side_for_user")
        conn.execute("DELETE FROM _migration_state WHERE name='side_rooms_v1'")
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("DROP TABLE whatsapp_skill_requests")
        conn.execute(_PRE_STAGE_REQUESTS)
        conn.execute("CREATE UNIQUE INDEX idx_whatsapp_request_held_task "
                     "ON whatsapp_skill_requests(origin_task_id) WHERE state = 'held'")
        conn.execute("CREATE INDEX idx_whatsapp_request_queue "
                     "ON whatsapp_skill_requests(state, queue_deadline)")
        conn.execute("INSERT INTO tasks (id,user_id,source_type,prompt) VALUES (1,'alice','web','x')")
        conn.execute(
            "INSERT INTO whatsapp_skill_requests (id,requester_user_id,origin_task_id,request_key,"
            "kind,recipient_user_id,content_hash,service_body,service_hash,provider,"
            "binding_fingerprint,state) VALUES ('r1','alice',1,'k','self_send','alice','h',"
            "'body','sh','baileys','fp','sent')")
        conn.execute("INSERT INTO rooms (token,user_id,name,origin) VALUES ('old-room','alice','Old','web')")
        conn.commit()
    finally:
        conn.close()
    return old


def _normalized(sql):
    import re
    sql = re.sub(r"--[^\n]*", "", sql or "")
    sql = sql.replace('"', "").replace(" IF NOT EXISTS", "")
    sql = sql.replace("whatsapp_skill_requests_rebuild", "whatsapp_skill_requests")
    return re.sub(r"\s+", " ", sql).replace("( ", "(").replace(" )", ")").strip()


def _shape(conn, table):
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL "
        "ORDER BY type, name", (table,)).fetchall()
    return [(r[0], r[1], _normalized(r[2])) for r in rows]


class TestTheMigration:
    def test_an_upgraded_database_matches_a_fresh_one(self, tmp_path):
        fresh = tmp_path / "fresh.db"
        db.init_db(fresh)
        old = _pre_stage_db(tmp_path)
        db.init_db(old)
        db.init_db(old)
        with db.get_db(fresh) as a, db.get_db(old) as b:
            for table in ("rooms", "whatsapp_skill_requests"):
                cols_a = {r[1]: tuple(r)[2:5] for r in a.execute(f"PRAGMA table_info({table})")}
                cols_b = {r[1]: tuple(r)[2:5] for r in b.execute(f"PRAGMA table_info({table})")}
                assert cols_a == cols_b
            assert _shape(a, "whatsapp_skill_requests") == _shape(b, "whatsapp_skill_requests")
            assert b.execute("SELECT 1 FROM sqlite_master WHERE name='idx_rooms_side'").fetchone()
            assert b.execute(
                "SELECT 1 FROM _migration_state WHERE name='side_rooms_v1'").fetchone()
            # Rows survive the rebuild, and the widened CHECK takes the new kinds.
            assert b.execute("SELECT state FROM whatsapp_skill_requests WHERE id='r1'").fetchone()[0] == "sent"
            assert db.get_room(b, "old-room").side_of is None
            b.execute("UPDATE whatsapp_skill_requests SET kind='room_post' WHERE id='r1'")
            b.execute("UPDATE whatsapp_skill_requests SET kind='side_whisper' WHERE id='r1'")
            with pytest.raises(sqlite3.IntegrityError):
                b.execute("UPDATE whatsapp_skill_requests SET kind='anything' WHERE id='r1'")
            # The task-delete trigger still reaches the rebuilt table.
            b.execute("DELETE FROM tasks WHERE id=1")
            assert b.execute(
                "SELECT origin_task_id FROM whatsapp_skill_requests WHERE id='r1'").fetchone()[0] is None


# ---------------------------------------------------------------------------
# The room itself
# ---------------------------------------------------------------------------


class TestTheSideRoom:
    def test_created_on_first_need_once_private_and_linked(self, env):
        config, shared = env["config"], env["shared"]
        with db.get_db(config.db_path) as conn:
            assert db.get_side_room(conn, shared, "alice") is None
            side = db.ensure_side_room(conn, shared, "alice")
            again = db.ensure_side_room(conn, shared, "alice")
            assert side.token == again.token != shared
            assert side.side_of == shared and side.side_for_user == "alice"
            assert db.list_room_members(conn, side.token) == ["alice"]
            assert not db.room_is_shared(conn, side.token)
            # One per member: Bob's is his own room.
            bobs = db.ensure_side_room(conn, shared, "bob")
            assert bobs.token != side.token and db.list_room_members(conn, bobs.token) == ["bob"]
            # An ordinary private web room: the member has a handle for it.
            assert any(h.token == side.token for h in db.list_web_chat_rooms(conn, "alice"))

    def test_only_a_member_of_the_parent_gets_one_and_never_of_a_side_room(self, env):
        config, shared = env["config"], env["shared"]
        with db.get_db(config.db_path) as conn:
            with pytest.raises(ValueError):
                db.ensure_side_room(conn, shared, "carol")
            side = db.ensure_side_room(conn, shared, "alice")
            with pytest.raises(ValueError):
                db.ensure_side_room(conn, side.token, "alice")
            with pytest.raises(ValueError):
                db.ensure_side_room(conn, "no-such-room", "alice")

    def test_never_a_default_delivery_room(self, env):
        config, shared = env["config"], env["shared"]
        with db.get_db(config.db_path) as conn:
            side = db.ensure_side_room(conn, shared, "alice")
            handle = db.default_web_room(conn, "alice")
            assert handle is None or handle.token != side.token

    def test_the_web_route_refuses_a_second_member(self, env, monkeypatch):
        from istota import web_app
        config, shared = env["config"], env["shared"]
        monkeypatch.setattr(web_app, "_config", config)
        with db.get_db(config.db_path) as conn:
            side = db.ensure_side_room(conn, shared, "alice")
            handle = next(h for h in db.list_web_chat_rooms(conn, "alice") if h.token == side.token)
        status, body = web_app._chat_add_member("alice", handle.id, "bob")
        assert status == 409
        with db.get_db(config.db_path) as conn:
            assert db.list_room_members(conn, side.token) == ["alice"]


# ---------------------------------------------------------------------------
# Pinned delivery
# ---------------------------------------------------------------------------


class TestPinnedDelivery:
    @pytest.mark.parametrize("target", ["web:{parent}", "room:{parent}", "talk:{talk}"])
    def test_a_side_room_task_never_delivers_into_its_parent(self, env, target):
        from istota.transport.registry import make_registry
        from istota.transport.routing import resolve_delivery_plan
        config, shared = env["config"], env["shared"]
        with db.get_db(config.db_path) as conn:
            db.add_room_binding(conn, shared, "talk", "family-talk")
            side = db.ensure_side_room(conn, shared, "alice")
            spec = target.format(parent=shared, talk="family-talk")
            ident = db.create_task(conn, user_id="alice", source_type="web", prompt="x",
                                   conversation_token=side.token, output_target=spec)
            task = db.get_task(conn, ident)
            control_id = db.create_task(conn, user_id="alice", source_type="web", prompt="x",
                                        conversation_token=shared, output_target=spec)
            control = db.get_task(conn, control_id)
        registry = make_registry(config)
        plan = resolve_delivery_plan(config, task, registry)
        assert all(d.channel not in (shared, "family-talk") for d in plan)
        # The same target from anywhere else still reaches the room.
        control_plan = resolve_delivery_plan(config, control, registry)
        assert any(d.channel in (shared, "family-talk", "stream") for d in control_plan)
        assert control_plan != []


# ---------------------------------------------------------------------------
# The parent transcript as context
# ---------------------------------------------------------------------------


class TestTheParentTranscript:
    def _prompt(self, config, token):
        from istota.executor import build_prompt
        with db.get_db(config.db_path) as conn:
            ident = db.create_task(conn, user_id="alice", source_type="web",
                                   prompt="what did Bob say?", conversation_token=token)
            task = db.get_task(conn, ident)
            return build_prompt(task, [], config, conn=conn)

    def test_a_side_room_task_reads_the_parent_as_fenced_user_half_context(self, env):
        config, shared = env["config"], env["shared"]
        with db.get_db(config.db_path) as conn:
            db.add_message(conn, shared, role="user", body="flights are booked for friday",
                           origin_surface="web", author_user_id="bob")
            side = db.ensure_side_room(conn, shared, "alice")
        composed = self._prompt(config, side.token)
        assert "flights are booked for friday" in composed.user
        assert "flights are booked for friday" not in composed.system
        assert "UNTRUSTED PARENT ROOM TRANSCRIPT" in composed.user
        assert "side room" in composed.system

    def test_a_member_who_left_the_parent_reads_nothing_of_it(self, env):
        config, shared = env["config"], env["shared"]
        with db.get_db(config.db_path) as conn:
            db.add_message(conn, shared, role="user", body="after you left",
                           origin_surface="web", author_user_id="alice")
            side = db.ensure_side_room(conn, shared, "bob")
            db.drop_web_room_member(conn, shared, "bob")
            ident = db.create_task(conn, user_id="bob", source_type="web",
                                   prompt="catch me up", conversation_token=side.token)
            from istota.executor import build_prompt
            composed = build_prompt(db.get_task(conn, ident), [], config, conn=conn)
        assert "after you left" not in composed.user + composed.system

    def test_an_ordinary_room_task_carries_no_parent_block(self, env):
        config, shared = env["config"], env["shared"]
        with db.get_db(config.db_path) as conn:
            db.add_message(conn, shared, role="user", body="room chatter",
                           origin_surface="web", author_user_id="bob")
        composed = self._prompt(config, shared)
        assert "PARENT ROOM TRANSCRIPT" not in composed.user
        assert "side room" not in composed.system


# ---------------------------------------------------------------------------
# room whisper
# ---------------------------------------------------------------------------


class TestWhisper:
    def test_a_shared_room_task_whispers_into_the_principals_side_room(self, env):
        config, shared = env["config"], env["shared"]
        with db.get_db(config.db_path) as conn:
            ident = _running_task(conn, "alice", shared)
            result = side_rooms.enqueue_whisper(conn, config, actor_user_id="alice", task_id=ident,
                                                request_key="w1", text="Your calendar is free Thursday.")
            again = side_rooms.enqueue_whisper(conn, config, actor_user_id="alice", task_id=ident,
                                               request_key="w1", text="Your calendar is free Thursday.")
        assert result["status"] == "queued" and again["request_id"] == result["request_id"]
        asyncio.run(requests.drain_requests(config))
        asyncio.run(requests.drain_requests(config))
        with db.get_db(config.db_path) as conn:
            side = db.get_side_room(conn, shared, "alice")
        (row,) = _rows(config, "SELECT * FROM messages WHERE body LIKE '%free Thursday%'")
        assert row["room_token"] == side.token
        assert _rows(config, "SELECT state FROM whatsapp_skill_requests")[0]["state"] == "sent"
        # A web-only parent has no Talk view to push to.
        assert env["talk"].calls == []

    def test_a_talk_bound_parent_also_reaches_the_users_talk_conversation(self, env):
        config, shared = env["config"], env["shared"]
        with db.get_db(config.db_path) as conn:
            db.add_room_binding(conn, shared, "talk", "family-talk")
            private = _private_talk_room(conn, "alice")
            ident = _running_task(conn, "alice", shared)
            side_rooms.enqueue_whisper(conn, config, actor_user_id="alice", task_id=ident,
                                       request_key="w1", text="Only for you.")
        asyncio.run(requests.drain_requests(config))
        sends = env["talk"].calls_to(private, method="send_message")
        assert len(sends) == 1
        assert sends[0].args["message"].startswith("re: Family")
        assert "Only for you." in sends[0].args["message"]
        assert env["talk"].calls_to("family-talk", method="send_message") == []
        assert env["talk"].refusals == []

    def test_refused_outside_a_shared_room(self, env):
        config, shared = env["config"], env["shared"]
        with db.get_db(config.db_path) as conn:
            private = db.create_web_chat_room(conn, "alice", "Mine").token
            ident = _running_task(conn, "alice", private)
            with pytest.raises(RequestError, match="not_a_shared_room"):
                side_rooms.enqueue_whisper(conn, config, actor_user_id="alice", task_id=ident,
                                           request_key="w1", text="hi")
            side = db.ensure_side_room(conn, shared, "alice")
            ident = _running_task(conn, "alice", side.token)
            with pytest.raises(RequestError, match="not_a_shared_room"):
                side_rooms.enqueue_whisper(conn, config, actor_user_id="alice", task_id=ident,
                                           request_key="w2", text="hi")


# ---------------------------------------------------------------------------
# room post
# ---------------------------------------------------------------------------


def _side_task(config, shared, *, prompt="post it"):
    with db.get_db(config.db_path) as conn:
        side = db.ensure_side_room(conn, shared, "alice")
        return side.token, _running_task(conn, "alice", side.token, prompt=prompt)


def _hold_post(config, ident, text="Alice can do Thursday after 7"):
    with db.get_db(config.db_path) as conn:
        return side_rooms.hold_room_post(conn, config, actor_user_id="alice", task_id=ident,
                                         request_key="p1", text=text)


class TestRoomPost:
    def test_held_with_the_exact_text_and_posts_nothing_until_approved(self, env):
        config, shared = env["config"], env["shared"]
        _, ident = _side_task(config, shared)
        held = _hold_post(config, ident)
        assert held["status"] == "held" and held["needs_confirmation"]
        assert "Alice can do Thursday after 7" in held["preview"]
        asyncio.run(requests.drain_requests(config))
        assert _rows(config, "SELECT * FROM messages WHERE room_token=?", (shared,)) == []
        with db.get_db(config.db_path) as conn:
            parked = requests.park_question(conn, config, task=db.get_task(conn, ident))
            assert parked["preview"] == held["preview"]
            assert db.get_task(conn, ident).status == "pending_confirmation"
            confirmations.approve(conn, db.get_task(conn, ident), config=config, by="web")
        asyncio.run(requests.drain_requests(config))
        asyncio.run(requests.drain_requests(config))
        (row,) = _rows(config, "SELECT * FROM messages WHERE room_token=?", (shared,))
        assert row["body"] == "Alice can do Thursday after 7"
        assert _rows(config, "SELECT state FROM whatsapp_skill_requests")[0]["state"] == "sent"

    def test_a_talk_bound_parent_gets_the_post_on_talk_too(self, env):
        config, shared = env["config"], env["shared"]
        with db.get_db(config.db_path) as conn:
            db.add_room_binding(conn, shared, "talk", "family-talk")
        _, ident = _side_task(config, shared)
        _hold_post(config, ident)
        with db.get_db(config.db_path) as conn:
            requests.park_question(conn, config, task=db.get_task(conn, ident))
            confirmations.approve(conn, db.get_task(conn, ident), config=config, by="web")
        asyncio.run(requests.drain_requests(config))
        (send,) = env["talk"].calls_to("family-talk", method="send_message")
        assert send.args["message"] == "Alice can do Thursday after 7"

    def test_a_member_removed_before_delivery_posts_nothing(self, env):
        config, shared = env["config"], env["shared"]
        with db.get_db(config.db_path) as conn:
            side = db.ensure_side_room(conn, shared, "bob")
            ident = _running_task(conn, "bob", side.token)
            side_rooms.hold_room_post(conn, config, actor_user_id="bob", task_id=ident,
                                      request_key="p1", text="hello all")
            requests.park_question(conn, config, task=db.get_task(conn, ident))
            confirmations.approve(conn, db.get_task(conn, ident), config=config, by="web")
            db.drop_web_room_member(conn, shared, "bob")
        asyncio.run(requests.drain_requests(config))
        assert _rows(config, "SELECT * FROM messages WHERE room_token=?", (shared,)) == []
        assert _rows(config, "SELECT state FROM whatsapp_skill_requests")[0]["state"] == "failed"

    def test_refused_outside_a_side_room(self, env):
        config, shared = env["config"], env["shared"]
        with db.get_db(config.db_path) as conn:
            ident = _running_task(conn, "alice", shared)
            with pytest.raises(RequestError, match="not_a_side_room"):
                side_rooms.hold_room_post(conn, config, actor_user_id="alice", task_id=ident,
                                          request_key="p1", text="hi")

    def test_the_members_own_words_on_a_clean_turn_skip_approval(self, env):
        config, shared = env["config"], env["shared"]
        _, ident = _side_task(config, shared, prompt='post "Alice can do Thursday after 7" please')
        with db.get_db(config.db_path) as conn:
            db.record_attempt_tool_call(conn, ident, calls_seen=1, first_is_relay=True)
        released = _hold_post(config, ident)
        assert released["status"] == "queued" and released["approval"] == "clean_turn"
        asyncio.run(requests.drain_requests(config))
        (row,) = _rows(config, "SELECT * FROM messages WHERE room_token=?", (shared,))
        assert row["body"] == "Alice can do Thursday after 7"

    def test_words_the_member_did_not_write_are_held(self, env):
        config, shared = env["config"], env["shared"]
        _, ident = _side_task(config, shared, prompt="tell them I can make it")
        with db.get_db(config.db_path) as conn:
            db.record_attempt_tool_call(conn, ident, calls_seen=1, first_is_relay=True)
        assert _hold_post(config, ident)["status"] == "held"

    def test_the_trace_hides_both_verbs_and_counts_a_lone_post(self):
        from istota.agent.events import _lone_relay_ask, _private_relay_tool
        assert _private_relay_tool("Bash", {"command": "istota-skill room whisper --request-key a 'x'"})
        assert _private_relay_tool("Bash", {"command": "istota-skill room post --request-key a 'x'"})
        assert _lone_relay_ask("Bash", {"command": "istota-skill room post --request-key a 'x'"})
        assert not _lone_relay_ask("Bash", {"command": "istota-skill room whisper --request-key a 'x'"})


# ---------------------------------------------------------------------------
# Confirmations from a shared room
# ---------------------------------------------------------------------------


class TestSharedRoomConfirmations:
    QUESTION = "I need your confirmation before sending the invite. Reply yes or no."

    def test_the_question_goes_to_the_side_room_never_the_room(self, tmp_path, monkeypatch, fake_talk):
        from istota.config import EmailConfig, SchedulerConfig
        from istota.scheduler import process_one_task
        config = _config(tmp_path)
        config.email = EmailConfig(enabled=False)
        config.scheduler = SchedulerConfig()
        config.workspace_path = tmp_path / "mount"
        config.workspace_path.mkdir()
        monkeypatch.setattr("istota.talk.TalkClient.get_participants",
                            AsyncMock(return_value=PARTICIPANTS_ALICE))
        fake_talk.db_path = config.db_path
        with db.get_db(config.db_path) as conn:
            group = plain_talk_room(conn, "alice", token="groupref", name="Family")
            db.add_room_member(conn, group.canonical, "bob")
            private = _private_talk_room(conn, "alice")
            ident = db.create_task(conn, prompt="invite them", user_id="alice",
                                   source_type="talk", conversation_token=group.canonical,
                                   is_group_chat=True)
        with patch("istota.scheduler.execute_task", return_value=(True, self.QUESTION, None, None)):
            process_one_task(config)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, ident)
            side = db.get_side_room(conn, group.canonical, "alice")
        assert task.status == "pending_confirmation"
        # The room sees the progress ack and nothing of the question.
        room_calls = fake_talk.calls_to(group.talk_ref)
        assert all(self.QUESTION not in str(c.args) for c in room_calls)
        assert [c.args.get("reference_id") for c in room_calls
                if c.method == "send_message"] == [f"istota:task:{ident}:ack"]
        assert side is not None
        (row,) = _rows(config, "SELECT * FROM messages WHERE room_token=?", (side.token,))
        assert self.QUESTION in row["body"]
        (send,) = fake_talk.calls_to(private, method="send_message")
        assert send.args["message"].startswith("re: Family")
        assert fake_talk.refusals == []
        assert _rows(config, "SELECT * FROM messages WHERE room_token=? AND body LIKE ?",
                     (group.canonical, f"%{self.QUESTION}%")) == []

    def test_a_bare_answer_resolves_from_the_side_room_not_the_shared_room(self, env):
        config, shared = env["config"], env["shared"]
        with db.get_db(config.db_path) as conn:
            ident = _running_task(conn, "alice", shared)
            db.set_task_confirmation(conn, ident, self.QUESTION)
            side = db.ensure_side_room(conn, shared, "alice")
            assert confirmations.resolve(conn, "alice", conversation_token=shared).task is None
            found = confirmations.resolve(conn, "alice", conversation_token=side.token)
            assert found.task is not None and found.task.id == ident
            # Bob's side room is not Alice's.
            bobs = db.ensure_side_room(conn, shared, "bob")
            assert confirmations.resolve(conn, "bob", conversation_token=bobs.token).task is None
