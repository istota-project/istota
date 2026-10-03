"""Side rooms (multiplayer D4, umbrella Stage 10).

A side room is one member's private companion of a shared room. These pin what
is left of it until the side-room schema goes (ISSUE-608 Stage 5): the schema
and its migration, creation, pinned delivery and the parent transcript. The
verbs and confirmations moved onto the member's own private room and are
pinned in `tests/test_private_replies.py`.
"""
import asyncio
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from istota import db
from istota.config import Config, NextcloudConfig, TalkConfig, UserConfig

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
    monkeypatch.setattr("istota.nextcloud.talk.TalkClient.get_participants", participants)
    with patch("istota.transport.talk.get_talk_client", talk_bot_client(talk)):
        yield dict(config=config, shared=shared, talk=talk, participants=participants)


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
        from istota.webui import app as web_app
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
        # The side room's own header line; the shared room's card does name
        # the principal's side room, as the place to whisper to.
        assert "Side room: this is" not in composed.system


class TestTheSideRoomStaysPrivate:
    def test_a_second_member_makes_it_unusable_rather_than_shared(self, env):
        config, shared = env["config"], env["shared"]
        with db.get_db(config.db_path) as conn:
            side = db.ensure_side_room(conn, shared, "alice")
            db.add_room_member(conn, side.token, "bob")
            with pytest.raises(ValueError):
                db.ensure_side_room(conn, shared, "alice")
            assert db.side_room_parent(conn, side.token) is None

    def test_it_is_never_promoted_to_talk(self, env, monkeypatch):
        from istota.webui import app as web_app
        config, shared = env["config"], env["shared"]
        monkeypatch.setattr(web_app, "_config", config)
        with db.get_db(config.db_path) as conn:
            side = db.ensure_side_room(conn, shared, "alice")
            handle = next(h for h in db.list_web_chat_rooms(conn, "alice") if h.token == side.token)
        status, _ = asyncio.run(web_app._chat_promote_to_talk("alice", handle.id))
        assert status == "not_found"

    def test_a_dropped_parent_destination_lands_in_the_side_room(self, env):
        from istota.transport.registry import make_registry
        from istota.transport.routing import resolve_delivery_plan
        config, shared = env["config"], env["shared"]
        with db.get_db(config.db_path) as conn:
            side = db.ensure_side_room(conn, shared, "alice")
            ident = db.create_task(conn, user_id="alice", source_type="scheduled", prompt="x",
                                   conversation_token=side.token, output_target=f"web:{shared}")
            task = db.get_task(conn, ident)
        plan = resolve_delivery_plan(config, task, make_registry(config))
        assert [(d.surface, d.channel) for d in plan] == [("web", side.token)]
