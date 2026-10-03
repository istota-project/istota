"""Private replies (ISSUE-608): what a shared room has for one member, in their own room.

The resolver that picks a member's own private room, the record-then-send
delivery primitive, the bell fallback when there is no private room, the two
`about_room_token` columns, and linking: a reply to or quote of a tagged row
links the turn to its shared room, which the prompt and the delivery plan
then read. The verbs and confirmations move onto these in Stage 3.
"""
import asyncio
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from istota import db
from istota.config import (
    Config, EmailConfig, NextcloudConfig, TalkConfig, UserConfig,
)
from istota.rooms import private_replies
from istota.rooms.private_replies import PrivateDestination
from istota.transport.whatsapp import outbound, whatsapp_conversation_token
from istota.transport.sms import sms_conversation_token
from istota.transport.whatsapp._types import WhatsAppSendResult
from istota.transport.whatsapp.providers._types import (
    WhatsAppProviderAdapter,
    WhatsAppProviderCaps,
)

from .support.rooms import plain_talk_room
from .support.talk_double import FakeTalkClient, talk_bot_client
from .support.whatsapp_config import build_whatsapp_config

GROUP_JID = "120363000000000001@g.us"
ALICE_JID = "15551234567@s.whatsapp.net"
PARTICIPANTS_ALICE = [{"actorType": "users", "actorId": "alice"},
                      {"actorType": "users", "actorId": "bot"}]
PARTICIPANTS_SHARED = PARTICIPANTS_ALICE + [{"actorType": "users", "actorId": "bob"}]
CAPS = WhatsAppProviderCaps(
    metered=False, has_service_window=False,
    supports_templates=False, delivery_receipts=True,
    address_field="jid", service_body_limit=4096, interactive_body_limit=4096,
)


def _config(tmp_path):
    path = tmp_path / "state.db"
    db.init_db(path)
    config = Config(
        db_path=path,
        temp_dir=tmp_path / "temp",
        nextcloud=NextcloudConfig(url="https://cloud.example.com", username="bot",
                                  app_password="secret"),
        talk=TalkConfig(enabled=True, bot_username="bot"),
        whatsapp=build_whatsapp_config(
            enabled=True, provider=db.WHATSAPP_BAILEYS_PROVIDER,
            business_phone_number="+15551230000",
        ),
        users={"alice": UserConfig(display_name="Alice",
                                   email_addresses=["alice@example.com"]),
               "bob": UserConfig(display_name="Bob")},
    )
    with db.get_db(path) as conn:
        db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number="+15551234567")
        db.latch_whatsapp_jid(conn, "alice", jid=ALICE_JID)
    return config


@pytest.fixture
def config(tmp_path):
    return _config(tmp_path)


@pytest.fixture
def sent(monkeypatch):
    """Every request the Baileys adapter was asked to send."""
    seen = []

    async def send(request):
        seen.append(request)
        return WhatsAppSendResult(message_id=f"BOT{len(seen)}")

    adapter = WhatsAppProviderAdapter(
        name="baileys", caps=CAPS, parse_webhook=None, send=send, verify_signature=None,
    )
    monkeypatch.setattr(outbound, "active_adapter", lambda config: adapter)
    return seen


@pytest.fixture
def talk(config, monkeypatch):
    """A strict Talk double and a participants stub naming Alice and the bot."""
    double = FakeTalkClient(config.db_path)
    participants = AsyncMock(return_value=PARTICIPANTS_ALICE)
    monkeypatch.setattr("istota.nextcloud.talk.TalkClient.get_participants", participants)
    with patch("istota.transport.talk.get_talk_client", talk_bot_client(double)):
        yield dict(client=double, participants=participants)


# --- room builders, each the shape its real producer writes ---------------


def _web_room(conn, user="alice", name="general"):
    return db.create_web_chat_room(conn, user, name).token


def _shared_web(conn, name="Family"):
    token = db.create_web_chat_room(conn, "alice", name).token
    db.add_web_room_member(conn, token, "bob")
    return token


def _shared_talk(conn, name="Family"):
    shape = plain_talk_room(conn, "alice", name=name)
    db.add_room_member(conn, shape.canonical, "bob")
    return shape.canonical


def _whatsapp_group(conn, name="Family"):
    token = db.register_room(conn, None, "alice", origin="whatsapp", name=name).token
    db.add_room_binding(conn, token, "whatsapp", GROUP_JID)
    db.add_room_member(conn, token, "bob")
    return token


def _email_thread(conn, name="Thread"):
    token = db.register_room(conn, None, "alice", origin="email", name=name).token
    db.add_room_binding(conn, token, "email", "thread-ref-1")
    db.add_room_member(conn, token, "bob")
    return token


def _phone_room(conn, surface="whatsapp", user="alice"):
    ref = (whatsapp_conversation_token if surface == "whatsapp" else sms_conversation_token)(user)
    token = db.register_room(conn, None, user, origin=surface, name="WhatsApp").token
    db.add_room_binding(conn, token, surface, ref)
    return token


def _resolve(config, about, user="alice"):
    with db.get_db(config.db_path) as conn:
        return private_replies.private_room_for(conn, config, user, about)


def _rows(config, sql, params=()):
    with db.get_db(config.db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _max_message_id(config):
    return _rows(config, "SELECT COALESCE(MAX(id), 0) AS m FROM messages")[0]["m"]


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------


class TestTheColumns:
    def test_a_fresh_database_has_both_columns(self, config):
        for table in ("messages", "tasks"):
            columns = {r["name"] for r in _rows(config, f"PRAGMA table_info({table})")}
            assert "about_room_token" in columns

    def test_an_existing_database_gains_them_at_boot(self, tmp_path):
        path = tmp_path / "old.db"
        db.init_db(path)
        with sqlite3.connect(path) as conn:
            conn.execute("ALTER TABLE messages DROP COLUMN about_room_token")
            conn.execute("ALTER TABLE tasks DROP COLUMN about_room_token")
        db.init_db(path)
        db.init_db(path)
        with sqlite3.connect(path) as conn:
            for table in ("messages", "tasks"):
                columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                assert "about_room_token" in columns

    def test_add_message_stores_the_tag_and_reads_it_back(self, config):
        with db.get_db(config.db_path) as conn:
            room = _web_room(conn)
            ident = db.add_message(conn, room, role="system", body="x",
                                   origin_surface="web", about_room_token="rm_parent")
            untagged = db.add_message(conn, room, role="system", body="y", origin_surface="web")
            messages = {m.id: m for m in db.list_system_messages(conn, room)}
        assert messages[ident].about_room_token == "rm_parent"
        assert messages[untagged].about_room_token is None


# ---------------------------------------------------------------------------
# The migration: side rooms removed (Stage 5)
# ---------------------------------------------------------------------------


def _skill_request(conn, ident, *, kind, destination, origin=None, task_id=None):
    import json

    conn.execute(
        "INSERT INTO whatsapp_skill_requests (id,requester_user_id,origin_task_id,"
        "request_key,kind,recipient_user_id,content_hash,service_body,service_hash,"
        "provider,binding_fingerprint,state,origin,destination) "
        "VALUES (?,'alice',?,?,?,'alice','h','body','sh','room','fp','queued',?,?)",
        (ident, task_id, ident, kind, json.dumps(origin) if origin else None,
         json.dumps(destination)),
    )


def _pre_removal_db(tmp_path):
    """A database as Stage 4 left it: side-room columns, rows in one, no marker."""
    path = tmp_path / "pre.db"
    db.init_db(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE tasks DROP COLUMN private_park")
        conn.execute("ALTER TABLE rooms ADD COLUMN side_of TEXT")
        conn.execute("ALTER TABLE rooms ADD COLUMN side_for_user TEXT")
        conn.execute("CREATE UNIQUE INDEX idx_rooms_side ON rooms (side_of, side_for_user) "
                     "WHERE side_of IS NOT NULL")
        conn.execute("DELETE FROM _migration_state WHERE name = 'private_replies_v1'")
        conn.execute("INSERT INTO rooms (token,user_id,name,origin) VALUES ('grp','alice','Family','web')")
        conn.execute("INSERT INTO rooms (token,user_id,name,origin,side_of,side_for_user) "
                     "VALUES ('side1','alice','re: Family','web','grp','alice')")
        for room, user in (("grp", "alice"), ("grp", "bob"), ("side1", "alice")):
            conn.execute("INSERT INTO room_members (room_token,user_id) VALUES (?,?)", (room, user))
        conn.execute("INSERT INTO room_bindings (room_token,surface,surface_ref) "
                     "VALUES ('side1','web','side1')")
        conn.execute("INSERT INTO web_chat_rooms (user_id,token,name) VALUES ('alice','side1','re: Family')")
        conn.execute("INSERT INTO messages (room_token,role,body,origin_surface) "
                     "VALUES ('side1','system','a whisper','web')")
        conn.execute("INSERT INTO messages (room_token,role,body,origin_surface) "
                     "VALUES ('grp','user','hello','web')")
        conn.execute("INSERT INTO tasks (id,user_id,source_type,prompt,conversation_token) "
                     "VALUES (7,'alice','web','post it','side1')")
        conn.execute("INSERT INTO tasks (id,user_id,source_type,prompt,conversation_token) "
                     "VALUES (8,'alice','web','hi','grp')")
        # A question parked in the shared room and asked in the side room.
        conn.execute("INSERT INTO tasks (id,user_id,source_type,prompt,conversation_token,status) "
                     "VALUES (9,'alice','web','book it','grp','pending_confirmation')")
        conn.execute("INSERT INTO messages (room_token,role,body,origin_surface,delivery_reference) "
                     "VALUES ('side1','system','Book it?','web','side-confirmation:9:abc')")
        _skill_request(conn, "old-whisper", kind="side_whisper",
                       destination={"kind": "side_room", "room_token": "side1", "parent": "grp"})
        _skill_request(conn, "new-whisper", kind="side_whisper",
                       destination={"kind": "private_reply", "about": "grp", "user": "alice"})
        _skill_request(conn, "side-post", kind="room_post", task_id=7,
                       destination={"kind": "room", "room_token": "grp"},
                       origin={"surface": "web", "channel": "side1", "room_token": "side1"})
        _skill_request(conn, "private-post", kind="room_post",
                       destination={"kind": "room", "room_token": "grp"},
                       origin={"surface": "web", "channel": "mine", "room_token": "mine"})
        conn.commit()
    finally:
        conn.close()
    return path


def _columns(conn, table):
    return {r[1]: (r[2], r[3], r[4]) for r in conn.execute(f"PRAGMA table_info({table})")}


class TestTheSideRoomRemoval:
    def test_side_rooms_and_their_requests_are_gone(self, tmp_path):
        path = _pre_removal_db(tmp_path)
        db.init_db(path)
        with db.get_db(path) as conn:
            assert db.get_room(conn, "side1") is None
            assert db.get_room(conn, "grp") is not None
            for table, column in (("room_members", "room_token"), ("room_bindings", "room_token"),
                                  ("web_chat_rooms", "token"), ("messages", "room_token"),
                                  ("tasks", "conversation_token")):
                assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = 'side1'"
                                    ).fetchone()[0] == 0, table
            # The shared room's own rows are untouched.
            assert conn.execute("SELECT COUNT(*) FROM messages WHERE room_token='grp'").fetchone()[0] == 1
            assert db.get_task(conn, 8) is not None
            kept = {r[0] for r in conn.execute("SELECT id FROM whatsapp_skill_requests")}
            assert kept == {"new-whisper", "private-post"}
            assert "side_of" not in _columns(conn, "rooms")
            assert "side_for_user" not in _columns(conn, "rooms")
            assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_rooms_side'").fetchone() is None
            assert "private_park" in _columns(conn, "tasks")
            parks = dict(conn.execute("SELECT id, private_park FROM tasks WHERE id IN (8, 9)"))
            assert parks == {8: 0, 9: 1}
            assert conn.execute("SELECT 1 FROM _migration_state "
                                "WHERE name='private_replies_v1'").fetchone()

    def test_a_second_boot_changes_nothing(self, tmp_path):
        path = _pre_removal_db(tmp_path)
        db.init_db(path)
        with db.get_db(path) as conn:
            before = [tuple(r) for r in conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name")]
            rows = conn.execute("SELECT COUNT(*) FROM whatsapp_skill_requests").fetchone()[0]
        db.init_db(path)
        with db.get_db(path) as conn:
            after = [tuple(r) for r in conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name")]
            assert conn.execute("SELECT COUNT(*) FROM whatsapp_skill_requests").fetchone()[0] == rows
        assert after == before

    def test_an_upgraded_database_matches_a_fresh_one(self, tmp_path, config):
        path = _pre_removal_db(tmp_path)
        db.init_db(path)
        with db.get_db(config.db_path) as fresh, db.get_db(path) as upgraded:
            for table in ("rooms", "tasks", "messages"):
                assert _columns(fresh, table) == _columns(upgraded, table), table
            for table in ("rooms",):
                indexes = "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? ORDER BY name"
                assert ([r[0] for r in fresh.execute(indexes, (table,))]
                        == [r[0] for r in upgraded.execute(indexes, (table,))])

    def test_a_request_table_from_before_the_room_kinds_upgrades_in_one_boot(self, tmp_path):
        path = _pre_removal_db(tmp_path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("DELETE FROM whatsapp_skill_requests")
            conn.execute("ALTER TABLE whatsapp_skill_requests DROP COLUMN origin")
            conn.execute("ALTER TABLE whatsapp_skill_requests DROP COLUMN destination")
            conn.commit()
        finally:
            conn.close()
        db.init_db(path)
        with db.get_db(path) as conn:
            assert conn.execute("SELECT 1 FROM _migration_state "
                                "WHERE name='private_replies_v1'").fetchone()
            assert "side_of" not in _columns(conn, "rooms")

    def test_a_fresh_database_never_had_side_rooms(self, config):
        with db.get_db(config.db_path) as conn:
            assert "side_of" not in _columns(conn, "rooms")
            assert conn.execute("SELECT 1 FROM _migration_state "
                                "WHERE name='private_replies_v1'").fetchone()


# ---------------------------------------------------------------------------
# The shared predicate
# ---------------------------------------------------------------------------


class TestThePredicate:
    def test_a_one_member_room_is_private(self, config):
        with db.get_db(config.db_path) as conn:
            room = _web_room(conn)
            assert db.is_private_room_of(conn, room, "alice")
            assert not db.is_private_room_of(conn, room, "bob")

    def test_a_second_member_an_archive_or_a_missing_room_is_not(self, config):
        with db.get_db(config.db_path) as conn:
            shared = _shared_web(conn)
            archived = _web_room(conn, name="old")
            db.set_room_archived(conn, archived, True)
            assert not db.is_private_room_of(conn, shared, "alice")
            assert not db.is_private_room_of(conn, archived, "alice")
            assert not db.is_private_room_of(conn, "rm_missing", "alice")
            assert not db.is_private_room_of(conn, "", "alice")

    def test_a_phone_room_only_when_asked_for(self, config):
        with db.get_db(config.db_path) as conn:
            phone = _phone_room(conn)
            assert not db.is_private_room_of(conn, phone, "alice")
            assert db.is_private_room_of(conn, phone, "alice", allow_phone=True)

    def test_the_relay_refuses_what_the_predicate_refuses(self, config):
        """The relay's default-room check is the predicate: a pinned phone room
        is refused there exactly as before the extraction."""
        from istota.relay.destinations import _room
        from istota.relay.requests import RequestError

        with db.get_db(config.db_path) as conn:
            phone = _phone_room(conn)
            conn.execute("INSERT INTO user_profiles(user_id, default_room) VALUES ('alice', ?)",
                         (phone,))
            with pytest.raises(RequestError, match="recipient_has_no_private_room"):
                _room(conn, config, "alice")
            web = _web_room(conn)
            conn.execute("UPDATE user_profiles SET default_room=? WHERE user_id='alice'", (web,))
            assert _room(conn, config, "alice")["room_token"] == web


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


class TestTheResolver:
    def test_a_whatsapp_group_goes_to_the_private_whatsapp_room(self, config):
        with db.get_db(config.db_path) as conn:
            group = _whatsapp_group(conn)
            _web_room(conn)
            phone = _phone_room(conn)
        dest = _resolve(config, group)
        assert dest == PrivateDestination(room_token=phone, surface="whatsapp",
                                          talk_ref=None, whatsapp=True)

    def test_an_archived_whatsapp_room_is_skipped(self, config):
        with db.get_db(config.db_path) as conn:
            group = _whatsapp_group(conn)
            web = _web_room(conn)
            phone = _phone_room(conn)
            db.set_room_archived(conn, phone, True)
        dest = _resolve(config, group)
        assert (dest.room_token, dest.surface) == (web, "web")

    def test_a_talk_parent_takes_the_configured_default_talk_room_first(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_talk(conn)
            older = plain_talk_room(conn, "alice", name="first")
            pinned = plain_talk_room(conn, "alice", name="pinned")
            _web_room(conn)
            conn.execute("INSERT INTO user_profiles(user_id, default_room) VALUES ('alice', ?)",
                         (pinned.canonical,))
        dest = _resolve(config, parent)
        assert dest == PrivateDestination(room_token=pinned.canonical, surface="talk",
                                          talk_ref=pinned.talk_ref, whatsapp=False)
        assert dest.room_token != older.canonical

    def test_a_talk_parent_takes_the_oldest_private_talk_room_and_never_a_shared_one(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_talk(conn)
            other_shared = plain_talk_room(conn, "alice", name="Club")
            db.add_room_member(conn, other_shared.canonical, "bob")
            first = plain_talk_room(conn, "alice", name="first")
            conn.execute("UPDATE rooms SET created_at='2000-01-01' WHERE token=?",
                         (other_shared.canonical,))
            # Same-second rooms order by their random token; pin "first" older.
            conn.execute("UPDATE rooms SET created_at='2001-01-01' WHERE token=?",
                         (first.canonical,))
            plain_talk_room(conn, "alice", name="second")
        dest = _resolve(config, parent)
        assert dest.room_token == first.canonical
        assert dest.talk_ref == first.talk_ref

    def test_a_web_parent_takes_the_default_web_room(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            web = _web_room(conn)
            plain_talk_room(conn, "alice", name="talk")
        dest = _resolve(config, parent)
        assert dest == PrivateDestination(room_token=web, surface="web",
                                          talk_ref=None, whatsapp=False)

    def test_an_email_parent_goes_straight_to_the_fallback_order(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _email_thread(conn)
            talk = plain_talk_room(conn, "alice", name="talk")
            _phone_room(conn)
        dest = _resolve(config, parent)
        assert (dest.room_token, dest.surface) == (talk.canonical, "talk")
        with db.get_db(config.db_path) as conn:
            web = _web_room(conn)
        assert _resolve(config, parent).room_token == web

    def test_the_fallback_ends_at_the_private_whatsapp_room(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_talk(conn)
            phone = _phone_room(conn)
        dest = _resolve(config, parent)
        assert (dest.room_token, dest.surface, dest.whatsapp) == (phone, "whatsapp", True)

    def test_none_when_nothing_qualifies_and_sms_is_never_chosen(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            _phone_room(conn, surface="sms")
            _shared_talk(conn, name="Club")
        assert _resolve(config, parent) is None

    def test_a_whatsapp_group_with_no_private_rooms_resolves_none(self, config):
        with db.get_db(config.db_path) as conn:
            group = _whatsapp_group(conn)
        assert _resolve(config, group, user="bob") is None


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


def _deliver(config, about, *, user="alice", kind="confirmation", reference="7:abc", body="Shall I?"):
    with db.get_db(config.db_path) as conn:
        return private_replies.deliver_private(
            conn, config, user_id=user, about_token=about, kind=kind,
            reference=reference, body=body, task_id=7)


class TestRecord:
    def test_one_tagged_row_under_a_namespaced_reference(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            web = _web_room(conn)
        delivery = _deliver(config, parent)
        (row,) = _rows(config, "SELECT * FROM messages WHERE room_token=?", (web,))
        assert row["id"] == delivery.message_id
        assert row["about_room_token"] == parent
        assert row["delivery_reference"] == "private-confirmation:7:abc"
        assert (row["role"], row["origin_surface"], row["task_id"]) == ("system", "web", None)
        assert row["body"] == "Shall I?"
        assert delivery.dest.room_token == web

    def test_a_retry_returns_the_same_row(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            web = _web_room(conn)
        first = _deliver(config, parent)
        second = _deliver(config, parent)
        assert first.message_id == second.message_id
        assert len(_rows(config, "SELECT id FROM messages WHERE room_token=?", (web,))) == 1

    def test_a_retry_stays_in_the_room_it_first_landed_in(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_talk(conn)
            talk = plain_talk_room(conn, "alice", name="talk")
        first = _deliver(config, parent)
        with db.get_db(config.db_path) as conn:
            pinned = plain_talk_room(conn, "alice", name="pinned")
            conn.execute("INSERT INTO user_profiles(user_id, default_room) VALUES ('alice', ?)",
                         (pinned.canonical,))
        second = _deliver(config, parent)
        assert second.message_id == first.message_id
        assert second.dest.room_token == talk.canonical
        assert second.dest.talk_ref == talk.talk_ref

    def test_two_members_confirmations_land_in_their_own_rooms(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            alices = _web_room(conn)
            bobs = _web_room(conn, user="bob")
        a = _deliver(config, parent, user="alice", reference="7:aaa")
        b = _deliver(config, parent, user="bob", reference="8:bbb")
        assert (a.dest.room_token, b.dest.room_token) == (alices, bobs)
        assert a.message_id != b.message_id
        rows = _rows(config, "SELECT room_token, delivery_reference FROM messages "
                             "WHERE delivery_reference LIKE 'private-%' ORDER BY id")
        assert rows == [{"room_token": alices, "delivery_reference": "private-confirmation:7:aaa"},
                        {"room_token": bobs, "delivery_reference": "private-confirmation:8:bbb"}]

    def test_the_tag_is_the_canonical_token(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_talk(conn)
            ref = db.get_room_binding(conn, parent, "talk").surface_ref
            _web_room(conn)
        delivery = _deliver(config, ref)
        (row,) = _rows(config, "SELECT about_room_token FROM messages WHERE id=?",
                       (delivery.message_id,))
        assert row["about_room_token"] == parent

    def test_a_reference_used_in_another_users_room_is_not_taken(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            alices = _web_room(conn)
            _web_room(conn, user="bob")
        first = _deliver(config, parent, user="alice", reference="same")
        second = _deliver(config, parent, user="bob", reference="same", kind="confirmation")
        assert first.dest.room_token == alices
        assert (second.dest, second.message_id) == (None, None)
        (row,) = _rows(config, "SELECT room_token, body FROM messages "
                               "WHERE delivery_reference='private-confirmation:same'")
        assert row["room_token"] == alices

    def test_an_unknown_kind_is_refused(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
        with pytest.raises(ValueError):
            _deliver(config, parent, kind="memo")

    def test_no_room_is_ever_created(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
        before = _rows(config, "SELECT COUNT(*) AS n FROM rooms")[0]["n"]
        for kind in private_replies.KINDS:
            _deliver(config, parent, kind=kind, reference=f"r-{kind}")
        assert _rows(config, "SELECT COUNT(*) AS n FROM rooms")[0]["n"] == before


# ---------------------------------------------------------------------------
# The bell fallback
# ---------------------------------------------------------------------------


class TestTheBellFallback:
    def test_a_whisper_with_no_private_room_becomes_a_bell_row(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn, name="Family")
        mark = _max_message_id(config)
        delivery = _deliver(config, parent, kind="whisper", reference="room-whisper:r1",
                            body="Only for you.")
        assert (delivery.dest, delivery.message_id) == (None, None)
        assert _rows(config, "SELECT id FROM messages WHERE id > ? AND delivery_reference "
                             "LIKE 'private-%'", (mark,)) == []
        (row,) = _rows(config, "SELECT * FROM notifications WHERE user_id='alice' "
                               "AND source='task_alert'")
        assert row["title"] == "Private note about Family"
        assert row["body"] == "Only for you."
        assert delivery.notice is not None

    @pytest.mark.parametrize("kind", ["confirmation", "proposal", "answer_notice"])
    def test_other_kinds_add_nothing(self, config, kind):
        """A confirmation's own bell row is written by the park, not here."""
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
        delivery = _deliver(config, parent, kind=kind)
        assert (delivery.dest, delivery.message_id, delivery.notice) == (None, None, None)
        assert _rows(config, "SELECT id FROM notifications WHERE source='task_alert'") == []

    def test_a_resolver_read_error_falls_to_the_bell(self, config, monkeypatch):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            _web_room(conn)

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(private_replies, "private_room_for", broken)
        delivery = _deliver(config, parent, kind="whisper", reference="r2", body="hi")
        assert delivery.dest is None and delivery.notice is not None

    def test_the_bell_row_is_delivered_by_the_send_phase(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
        delivery = _deliver(config, parent, kind="whisper", reference="r3", body="hi")
        with patch("istota.notifications.store.deliver_pending") as deliver:
            asyncio.run(private_replies.send_private(config, delivery, body="hi"))
        deliver.assert_called_once_with(config, [delivery.notice])

    def test_the_shared_room_line_names_nobody_and_carries_nothing(self):
        assert private_replies.SHARED_ROOM_NOTICE == (
            "I've sent a private note to the person who asked. If you don't have "
            "a private chat with me yet, message me directly.")


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


class TestSend:
    def test_whatsapp_is_keyed_on_the_row_and_headed_with_the_room(self, config, sent):
        with db.get_db(config.db_path) as conn:
            group = _whatsapp_group(conn)
            _phone_room(conn)
        delivery = _deliver(config, group, body="Book it?")
        delivered = asyncio.run(private_replies.send_private(config, delivery, body="Book it?"))
        assert delivered is True
        (request,) = sent
        assert request.to == ALICE_JID
        assert request.text == "re: Family\n\nBook it?"
        (ledger,) = _rows(config, "SELECT logical_key FROM sent_whatsapp")
        assert ledger["logical_key"] == f"private-reply:{delivery.message_id}"

    def test_talk_is_posted_audited_and_stamped(self, config, talk):
        with db.get_db(config.db_path) as conn:
            parent = _shared_talk(conn)
            private = plain_talk_room(conn, "alice", name="talk")
        delivery = _deliver(config, parent, body="Shall I?")
        delivered = asyncio.run(private_replies.send_private(config, delivery, body="Shall I?"))
        assert delivered is True
        (post,) = talk["client"].calls_to(private.talk_ref, method="send_message")
        assert post.args["message"] == "re: Family\n\nShall I?"
        assert post.args["reference_id"] == "private-confirmation:7:abc"
        assert talk["client"].refusals == []
        (row,) = _rows(config, "SELECT external_ids FROM messages WHERE id=?",
                       (delivery.message_id,))
        assert '"talk"' in row["external_ids"]
        with db.get_db(config.db_path) as conn:
            found = db.find_message_by_external_id(conn, private.canonical, "talk",
                                                   str(post.sent_id))
        assert found == delivery.message_id

    def test_a_whisper_that_reaches_nobody_goes_to_the_bell_too(self, config, sent):
        with db.get_db(config.db_path) as conn:
            group = _whatsapp_group(conn)
            phone = _phone_room(conn)
            # The room outlives the binding: its token is a function of the user.
            conn.execute("DELETE FROM whatsapp_user_bindings WHERE user_id='alice'")
        delivery = _deliver(config, group, kind="whisper", reference="room-whisper:r9",
                            body="Only for you.")
        assert delivery.dest.room_token == phone and delivery.notice is None
        with patch("istota.notifications.store.deliver_pending") as deliver:
            delivered = asyncio.run(private_replies.send_private(
                config, delivery, body="Only for you."))
        assert delivered is False and sent == []
        (row,) = _rows(config, "SELECT title, body FROM notifications "
                               "WHERE user_id='alice' AND source='task_alert'")
        assert row == {"title": "Private note about Family", "body": "Only for you."}
        deliver.assert_called_once()

    def test_a_delivered_whisper_adds_no_bell_row(self, config, sent):
        with db.get_db(config.db_path) as conn:
            group = _whatsapp_group(conn)
            _phone_room(conn)
        delivery = _deliver(config, group, kind="whisper", reference="room-whisper:r10")
        assert asyncio.run(private_replies.send_private(config, delivery, body="x")) is True
        assert _rows(config, "SELECT id FROM notifications WHERE source='task_alert'") == []

    def test_a_callers_label_is_flattened_onto_one_line(self, config, sent):
        with db.get_db(config.db_path) as conn:
            group = _whatsapp_group(conn)
            _phone_room(conn)
        delivery = _deliver(config, group)
        asyncio.run(private_replies.send_private(
            config, delivery, body="x", header_room_label="Fam\nily"))
        (request,) = sent
        assert request.text.split("\n\n", 1)[0] == "re: Fam ily"

    def test_a_talk_room_that_is_not_private_gets_nothing(self, config, talk):
        talk["participants"].return_value = PARTICIPANTS_SHARED
        with db.get_db(config.db_path) as conn:
            parent = _shared_talk(conn)
            private = plain_talk_room(conn, "alice", name="talk")
        delivery = _deliver(config, parent)
        delivered = asyncio.run(private_replies.send_private(config, delivery, body="Shall I?"))
        assert delivered is False
        assert talk["client"].calls_to(private.talk_ref, method="send_message") == []
        (row,) = _rows(config, "SELECT external_ids FROM messages WHERE id=?",
                       (delivery.message_id,))
        assert row["external_ids"] is None

    def test_a_web_room_is_read_from_the_transcript_and_nothing_is_sent(self, config, sent, talk):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            _web_room(conn)
        delivery = _deliver(config, parent)
        assert asyncio.run(private_replies.send_private(config, delivery, body="x")) is False
        assert sent == [] and talk["client"].calls == []

    def test_a_failed_post_reports_undelivered_and_never_raises(self, config, talk, monkeypatch):
        with db.get_db(config.db_path) as conn:
            parent = _shared_talk(conn)
            plain_talk_room(conn, "alice", name="talk")
        delivery = _deliver(config, parent)
        monkeypatch.setattr("istota.transport.talk.TalkTransport.deliver",
                            AsyncMock(side_effect=RuntimeError("boom")))
        assert asyncio.run(private_replies.send_private(config, delivery, body="x")) is False

    def test_an_email_thread_parent_also_gets_the_heads_up_mail(self, config):
        config.email = EmailConfig(enabled=True, bot_email="bot@example.com")
        with db.get_db(config.db_path) as conn:
            parent = _email_thread(conn, name="Thread")
            _web_room(conn)
        delivery = _deliver(config, parent, body="Shall I?")
        with patch("istota.rooms.private_replies._send_private_mail") as send:
            delivered = asyncio.run(private_replies.send_private(config, delivery, body="Shall I?"))
        assert delivered is True
        (call,) = send.call_args_list
        assert call.kwargs["to"] == "alice@example.com"
        assert call.kwargs["subject"] == "re: Thread"
        assert call.kwargs["body"].startswith("Shall I?\n\n")
        assert "private chat" in call.kwargs["body"]
        assert "notifications" in call.kwargs["body"]

    def test_no_mail_for_a_parent_that_is_not_an_email_thread(self, config, talk):
        config.email = EmailConfig(enabled=True, bot_email="bot@example.com")
        with db.get_db(config.db_path) as conn:
            parent = _shared_talk(conn)
            plain_talk_room(conn, "alice", name="talk")
        delivery = _deliver(config, parent)
        with patch("istota.rooms.private_replies._send_private_mail") as send:
            asyncio.run(private_replies.send_private(config, delivery, body="x"))
        send.assert_not_called()


# ---------------------------------------------------------------------------
# Linking
# ---------------------------------------------------------------------------


def _tagged(conn, room, about, *, body="Shall I?", reference="7:abc", kind="confirmation"):
    return db.add_message(conn, room, role="system", body=body, origin_surface="web",
                          about_room_token=about,
                          delivery_reference=f"private-{kind}:{reference}")


def _web_turn(conn, config, room, parent_id, *, user="alice", text="post that it works"):
    from istota.transport.ingest import record_inbound

    return record_inbound(conn, config, surface="web", surface_ref=room, user_id=user,
                          text=text, source_type="web", output_target="room",
                          reply_to_canonical_id=parent_id)


def _about(config, task_id):
    with db.get_db(config.db_path) as conn:
        return db.get_task(conn, task_id).about_room_token


class TestLinking:
    def test_a_web_reply_to_a_tagged_row_links_the_turn(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            web = _web_room(conn)
            row = _tagged(conn, web, parent)
            result = _web_turn(conn, config, web, row)
        assert _about(config, result.task_id) == parent

    def test_a_reply_to_an_untagged_row_does_not(self, config):
        with db.get_db(config.db_path) as conn:
            _shared_web(conn)
            web = _web_room(conn)
            row = db.add_message(conn, web, role="assistant", body="hi", origin_surface="web")
            result = _web_turn(conn, config, web, row)
        assert _about(config, result.task_id) is None

    def test_a_parent_in_another_room_does_not(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            web = _web_room(conn)
            other = _web_room(conn, name="other")
            row = _tagged(conn, other, parent)
            result = _web_turn(conn, config, web, row)
        assert _about(config, result.task_id) is None

    def test_an_unquoted_turn_is_not_linked_after_a_linked_one(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            web = _web_room(conn)
            row = _tagged(conn, web, parent)
            first = _web_turn(conn, config, web, row)
            second = _web_turn(conn, config, web, None, text="and the weather?")
        assert _about(config, first.task_id) == parent
        assert _about(config, second.task_id) is None

    def test_a_talk_reply_to_a_tagged_row_links_the_turn(self, config):
        from istota.transport.ingest import record_inbound

        with db.get_db(config.db_path) as conn:
            parent = _shared_talk(conn)
            mine = plain_talk_room(conn, "alice", name="talk")
            row = db.add_message(conn, mine.canonical, role="system", body="Shall I?",
                                 origin_surface="talk", about_room_token=parent,
                                 delivery_reference="private-confirmation:7:abc")
            db.set_message_external_id(conn, row, "talk", "4242")
            result = record_inbound(conn, config, surface="talk", surface_ref=mine.talk_ref,
                                    user_id="alice", text="yes, post it",
                                    platform_message_id=4300, external_id="4300",
                                    reply_to_message_id=4242)
        assert _about(config, result.task_id) == parent


def _baileys_event(text, *, quote=None, ident="IN1"):
    import time

    from istota.transport.whatsapp.baileys_protocol import inbound_event

    return inbound_event(dict(message_id=ident, jid=ALICE_JID, message_type="text", text=text,
                              reply_to_message_id=quote, timestamp=int(time.time())))


def _receive(config, event):
    from istota.transport.whatsapp.webhook import handle_whatsapp_batch

    with db.get_db(config.db_path) as conn:
        return handle_whatsapp_batch(conn, config, [event], provider="baileys")[0]


class TestWhatsAppQuoteLinking:
    def _sent_note(self, config, sent, about):
        delivery = _deliver(config, about, kind="whisper", reference="room-whisper:1",
                            body="Friday is free.")
        assert asyncio.run(private_replies.send_private(config, delivery, body="Friday is free."))
        return delivery, f"BOT{len(sent)}"

    def _setup(self, config):
        with db.get_db(config.db_path) as conn:
            group = _whatsapp_group(conn)
            mine = _phone_room(conn)
        return group, mine

    def test_a_quote_of_a_private_reply_links_the_turn_through_the_webhook(self, config, sent):
        group, mine = self._setup(config)
        delivery, provider_id = self._sent_note(config, sent, group)
        result = _receive(config, _baileys_event("post that Friday works", quote=provider_id))
        assert result.disposition == "task"
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, result.task_id)
            (turn,) = conn.execute(
                "SELECT reply_to_message_id FROM messages WHERE task_id = ?", (task.id,),
            ).fetchall()
        assert task.about_room_token == group
        assert task.conversation_token == mine
        assert task.reply_to_message_id == delivery.message_id
        assert task.reply_to_content == "Friday is free."
        assert turn["reply_to_message_id"] == delivery.message_id

    def test_an_unquoted_message_and_an_unknown_quote_are_not_linked(self, config, sent):
        group, _mine = self._setup(config)
        self._sent_note(config, sent, group)
        plain = _receive(config, _baileys_event("hello", ident="IN2"))
        stray = _receive(config, _baileys_event("hello", quote="SOMEONE-ELSE", ident="IN3"))
        assert _about(config, plain.task_id) is None
        assert _about(config, stray.task_id) is None

    def test_a_reused_rowid_that_is_no_longer_a_private_reply_is_ignored(self, config, sent):
        group, mine = self._setup(config)
        delivery, provider_id = self._sent_note(config, sent, group)
        with db.get_db(config.db_path) as conn:
            conn.execute("DELETE FROM messages WHERE id = ?", (delivery.message_id,))
            reused = db.add_message(conn, mine, role="assistant", body="unrelated",
                                    origin_surface="whatsapp")
        assert reused == delivery.message_id
        result = _receive(config, _baileys_event("post it", quote=provider_id))
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, result.task_id)
        assert task.about_room_token is None
        assert task.reply_to_message_id is None

    def test_another_users_ledger_row_is_not_resolved(self, config, sent):
        group, mine = self._setup(config)
        _delivery, provider_id = self._sent_note(config, sent, group)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE sent_whatsapp SET user_id = 'bob'")
            found = private_replies.quoted_private_reply(
                conn, user_id="alice", quoted_id=provider_id, room_token=mine)
            control = conn.execute("SELECT 1 FROM sent_whatsapp WHERE meta_message_id = ?",
                                   (provider_id,)).fetchone()
        assert control is not None
        assert found is None

    def test_a_relay_quote_keeps_precedence(self, config, sent, monkeypatch):
        from istota.relay import relays
        from istota.transport.whatsapp.webhook import WhatsAppEventResult

        group, _mine = self._setup(config)
        _delivery, provider_id = self._sent_note(config, sent, group)
        claimed = WhatsAppEventResult("relay_answer", user_id="alice")
        monkeypatch.setattr(relays, "match_whatsapp_reply", lambda *a, **k: claimed)
        result = _receive(config, _baileys_event("yes", quote=provider_id))
        assert result is claimed
        assert _rows(config, "SELECT id FROM tasks WHERE about_room_token IS NOT NULL") == []


# ---------------------------------------------------------------------------
# The linked prompt
# ---------------------------------------------------------------------------


def _linked_task(conn, room, about, *, user="alice", prompt="post that it works"):
    ident = db.create_task(conn, user_id=user, source_type="web", prompt=prompt,
                           conversation_token=room, about_room_token=about)
    return db.get_task(conn, ident)


def _dry_run(config, task, monkeypatch):
    from istota import executor

    monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
    success, result, _a, _t = executor.execute_task(task, config, [], dry_run=True)
    assert success, result
    system, user = result.split("===== USER =====", 1)
    return system, user


@pytest.fixture
def local_config(tmp_path):
    """No Nextcloud: a dry run assembles with no network on its path."""
    config = _config(tmp_path)
    config.nextcloud = NextcloudConfig()
    config.talk = TalkConfig(enabled=False)
    return config


class TestTheLinkedPrompt:
    def test_the_system_line_names_the_room_by_token_and_its_transcript_is_user_half(
        self, local_config, monkeypatch,
    ):
        config = local_config
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn, name="SECRET-ROOM-NAME")
            db.add_message(conn, parent, role="user", body="flights are booked for friday",
                           origin_surface="web", author_user_id="bob")
            web = _web_room(conn)
            task = _linked_task(conn, web, parent)
        system, user = _dry_run(config, task, monkeypatch)
        assert f"Linked room: this turn replies to a message about room {parent}." in system
        assert "Only alice reads this conversation" in system
        assert "SECRET-ROOM-NAME" not in system
        assert "flights are booked for friday" not in system
        assert "flights are booked for friday" in user
        assert "UNTRUSTED PARENT ROOM TRANSCRIPT" in user
        assert "## Linked room (read-only)" in user

    def test_a_member_who_left_gets_an_ordinary_private_turn(self, local_config, monkeypatch):
        config = local_config
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            db.add_message(conn, parent, role="user", body="after you left",
                           origin_surface="web", author_user_id="alice")
            bob_web = _web_room(conn, user="bob", name="bob's")
            db.drop_web_room_member(conn, parent, "bob")
            task = _linked_task(conn, bob_web, parent, user="bob")
        system, user = _dry_run(config, task, monkeypatch)
        assert "Linked room:" not in system
        assert "after you left" not in system + user

    def test_a_room_that_is_gone_links_to_nothing(self, config):
        with db.get_db(config.db_path) as conn:
            web = _web_room(conn)
            task = _linked_task(conn, web, "rm_gone")
            assert private_replies.linked_room(conn, task) is None

    def test_a_talk_departure_ends_the_link_though_the_member_row_stays(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_talk(conn)
            db.upsert_room_participant(conn, room_token=parent, surface="talk",
                                       surface_ref="alice", kind="principal", user_id="alice")
            web = _web_room(conn)
            task = _linked_task(conn, web, parent)
            assert private_replies.linked_room(conn, task) == parent
            conn.execute("UPDATE room_participants SET left_at = datetime('now') "
                         "WHERE room_token = ? AND user_id = 'alice'", (parent,))
            assert db.is_room_member(conn, parent, "alice")
            assert private_replies.linked_room(conn, task) is None

    def test_an_archived_room_links_to_nothing(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            web = _web_room(conn)
            task = _linked_task(conn, web, parent)
            assert private_replies.linked_room(conn, task) == parent
            db.set_room_archived(conn, parent, True)
            assert private_replies.linked_room(conn, task) is None

    def test_a_private_room_that_became_shared_no_longer_links(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            web = _web_room(conn)
            db.add_web_room_member(conn, web, "bob")
            task = _linked_task(conn, web, parent)
            assert private_replies.linked_room(conn, task) is None

    def test_an_unlinked_task_carries_no_linked_block(self, local_config, monkeypatch):
        config = local_config
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            db.add_message(conn, parent, role="user", body="room chatter",
                           origin_surface="web", author_user_id="bob")
            web = _web_room(conn)
            ident = db.create_task(conn, user_id="alice", source_type="web", prompt="hi",
                                   conversation_token=web)
            task = db.get_task(conn, ident)
        system, user = _dry_run(config, task, monkeypatch)
        assert "Linked room:" not in system
        assert "room chatter" not in user


# ---------------------------------------------------------------------------
# Delivery pinning
# ---------------------------------------------------------------------------


class TestThePin:
    @pytest.mark.parametrize("target", ["web:{parent}", "room:{parent}", "talk:{talk}"])
    def test_a_linked_task_never_delivers_into_its_room(self, config, target):
        from istota.transport.registry import make_registry
        from istota.transport.routing import resolve_delivery_plan

        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            db.add_room_binding(conn, parent, "talk", "family-talk")
            web = _web_room(conn)
            spec = target.format(parent=parent, talk="family-talk")
            ident = db.create_task(conn, user_id="alice", source_type="web", prompt="x",
                                   conversation_token=web, output_target=spec,
                                   about_room_token=parent)
            task = db.get_task(conn, ident)
            control_id = db.create_task(conn, user_id="alice", source_type="web", prompt="x",
                                        conversation_token=web, output_target=spec)
            control = db.get_task(conn, control_id)
        registry = make_registry(config)
        plan = resolve_delivery_plan(config, task, registry)
        assert all(d.channel not in (parent, "family-talk") for d in plan)
        assert [(d.surface, d.channel) for d in plan] == [("web", "stream")]
        # The shared-room refusal would drop these too; the pin's own effect
        # is what `pin_plan` returns, asserted below against the control.
        from istota.rooms.private_replies import pin_plan
        from istota.transport.routing import Destination
        into = [Destination("web", parent, "push"), Destination("talk", "family-talk", "push")]
        assert pin_plan(config, task, into, fallback=lambda: []) == []
        assert pin_plan(config, control, into, fallback=lambda: []) == into

    def test_the_fallback_is_used_only_when_the_plan_empties(self, config):
        from istota.rooms.private_replies import pin_plan
        from istota.transport.routing import Destination

        with db.get_db(config.db_path) as conn:
            parent = _shared_talk(conn)
            parent_ref = db.get_room_binding(conn, parent, "talk").surface_ref
            mine = plain_talk_room(conn, "alice", name="talk")
            ident = db.create_task(conn, user_id="alice", source_type="talk", prompt="x",
                                   conversation_token=mine.canonical,
                                   about_room_token=parent)
            task = db.get_task(conn, ident)
        own = Destination("talk", mine.talk_ref, "push")
        emptied = pin_plan(config, task, [Destination("talk", parent_ref, "push")],
                           fallback=lambda: [own])
        assert emptied == [own]
        kept = pin_plan(config, task, [Destination("talk", parent_ref, "push"),
                                       Destination("email", None, "push")],
                        fallback=lambda: [own])
        assert kept == [Destination("email", None, "push")]

    def test_an_unlinked_task_is_untouched(self, config):
        from istota.rooms.private_replies import pin_plan
        from istota.transport.routing import Destination

        with db.get_db(config.db_path) as conn:
            web = _web_room(conn)
            ident = db.create_task(conn, user_id="alice", source_type="web", prompt="x",
                                   conversation_token=web)
            task = db.get_task(conn, ident)
        plan = [Destination("web", web, "push")]
        assert pin_plan(config, task, plan, fallback=lambda: []) == plan


# ---------------------------------------------------------------------------
# Stage 3: the verbs and confirmations, retargeted
# ---------------------------------------------------------------------------


def _running(conn, user, token, *, prompt="hello", source_type="web", **kw):
    ident = db.create_task(conn, user_id=user, source_type=source_type,
                           prompt=prompt, conversation_token=token, **kw)
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
    return ident


def _room_count(config):
    with db.get_db(config.db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]


def _drain(config):
    from istota.relay import requests

    asyncio.run(requests.drain_requests(config))


def _whisper(config, ident, *, user="alice", key="w1", text="Your calendar is free Thursday."):
    from istota.rooms import private_replies

    with db.get_db(config.db_path) as conn:
        return private_replies.enqueue_whisper(conn, config, actor_user_id=user, task_id=ident,
                                          request_key=key, text=text)


class TestWhisper:
    def test_it_lands_in_the_principals_own_room_tagged_with_the_shared_room(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            web = _web_room(conn)
            ident = _running(conn, "alice", parent)
        before = _room_count(config)
        result = _whisper(config, ident)
        again = _whisper(config, ident)
        assert result["status"] == "queued" and again["request_id"] == result["request_id"]
        assert "delivered_to" not in result
        _drain(config)
        _drain(config)
        (row,) = _rows(config, "SELECT * FROM messages WHERE body LIKE '%free Thursday%'")
        assert row["room_token"] == web and row["about_room_token"] == parent
        assert row["delivery_reference"] == f"private-whisper:room-whisper:{result['request_id']}"
        assert _rows(config, "SELECT state FROM whatsapp_skill_requests")[0]["state"] == "sent"
        assert _room_count(config) == before

    def test_a_talk_parent_reaches_the_users_private_talk_room(self, config, talk):
        with db.get_db(config.db_path) as conn:
            parent = _shared_talk(conn)
            parent_ref = db.get_room_binding(conn, parent, "talk").surface_ref
            mine = plain_talk_room(conn, "alice", name="talk")
            ident = _running(conn, "alice", parent, source_type="talk")
        _whisper(config, ident, text="Only for you.")
        _drain(config)
        (send,) = talk["client"].calls_to(mine.talk_ref, method="send_message")
        assert send.args["message"].startswith("re: Family")
        assert "Only for you." in send.args["message"]
        assert talk["client"].calls_to(parent_ref, method="send_message") == []
        assert talk["client"].refusals == []
        (row,) = _rows(config, "SELECT external_ids FROM messages WHERE body = 'Only for you.'")
        assert '"talk"' in row["external_ids"]

    def test_with_no_private_room_it_is_a_bell_note_and_the_room_is_to_be_told(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            ident = _running(conn, "alice", parent)
        mark = _max_message_id(config)
        result = _whisper(config, ident, text="Only for you.")
        assert result["delivered_to"] == "notifications"
        assert result["room_notice"] == private_replies.SHARED_ROOM_NOTICE
        _drain(config)
        assert _rows(config, "SELECT id FROM messages WHERE id > ? AND body = 'Only for you.'",
                     (mark,)) == []
        (bell,) = _rows(config, "SELECT title, body FROM notifications WHERE source = 'task_alert'")
        assert bell["title"] == "Private note about Family"
        assert bell["body"] == "Only for you."

    def test_the_room_is_chosen_at_delivery_not_when_queued(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            ident = _running(conn, "alice", parent)
        assert _whisper(config, ident)["delivered_to"] == "notifications"
        with db.get_db(config.db_path) as conn:
            web = _web_room(conn)
        _drain(config)
        (row,) = _rows(config, "SELECT room_token FROM messages WHERE body LIKE '%free Thursday%'")
        assert row["room_token"] == web

    def test_refused_outside_a_shared_room_and_after_a_talk_departure(self, config):
        from istota.relay.requests import RequestError

        with db.get_db(config.db_path) as conn:
            private = _web_room(conn)
            mine = _running(conn, "alice", private)
            parent = _shared_talk(conn)
            db.upsert_room_participant(conn, room_token=parent, surface="talk",
                                       surface_ref="alice", kind="principal", user_id="alice")
            conn.execute("UPDATE room_participants SET left_at = datetime('now') "
                         "WHERE room_token = ? AND user_id = 'alice'", (parent,))
            gone = _running(conn, "alice", parent, source_type="talk")
        for ident in (mine, gone):
            with pytest.raises(RequestError, match="not_a_shared_room"):
                _whisper(config, ident)


class TestThePrivateAnswerOnEachSurface:
    def test_a_whatsapp_group_member_is_asked_again_in_their_whatsapp_room(self, config):
        """The phone room's own recording path takes a turn the daemon records
        for the member, inside the skill's transaction, with nothing minted."""
        from istota.rooms import private_replies

        with db.get_db(config.db_path) as conn:
            group = _whatsapp_group(conn)
            mine = _phone_room(conn)
            origin = _running(conn, "alice", group, prompt="am I free friday?",
                              source_type="whatsapp", is_group_chat=True)
        before = _room_count(config)
        with db.get_db(config.db_path) as conn:
            result = private_replies.queue_private_answer(
                conn, config, actor_user_id="alice", task_id=origin)
            task = db.get_task(conn, result["task_id"])
            (row,) = conn.execute(
                "SELECT role, body, author_user_id, delivery_reference FROM messages "
                "WHERE task_id = ?", (task.id,)).fetchall()
        assert task.conversation_token == mine
        assert task.source_type == "whatsapp" and task.output_target == "whatsapp"
        assert task.about_room_token == group
        assert task.prompt == "am I free friday?"
        assert dict(row) == {"role": "user", "body": "am I free friday?",
                             "author_user_id": "alice",
                             "delivery_reference": f"private-answer:{origin}"}
        assert _room_count(config) == before

    def test_a_talk_member_is_asked_again_in_their_private_talk_room(self, config):
        from istota.rooms import private_replies

        with db.get_db(config.db_path) as conn:
            parent = _shared_talk(conn)
            mine = plain_talk_room(conn, "alice", name="talk")
            origin = _running(conn, "alice", parent, source_type="talk")
            result = private_replies.queue_private_answer(
                conn, config, actor_user_id="alice", task_id=origin)
            task = db.get_task(conn, result["task_id"])
        assert task.conversation_token == mine.canonical
        assert task.source_type == "talk" and task.about_room_token == parent


def _hold_post(config, ident, *, text="Alice can do Thursday after 7", room=None,
               user="alice", key="p1"):
    from istota.rooms import private_replies

    with db.get_db(config.db_path) as conn:
        return private_replies.hold_room_post(conn, config, actor_user_id=user, task_id=ident,
                                         request_key=key, text=text, room=room)


def _approve(config, ident):
    from istota import confirmations
    from istota.relay import requests

    with db.get_db(config.db_path) as conn:
        requests.park_question(conn, config, task=db.get_task(conn, ident))
        confirmations.approve(conn, db.get_task(conn, ident), config=config, by="web")


class TestRoomPost:
    def _linked(self, config, *, prompt="post it"):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            web = _web_room(conn)
            ident = _running(conn, "alice", web, prompt=prompt, about_room_token=parent)
        return parent, web, ident

    def test_held_from_the_linked_private_room_and_posted_once_approved(self, config):
        parent, _web, ident = self._linked(config)
        held = _hold_post(config, ident)
        assert held["status"] == "held" and held["needs_confirmation"]
        assert "Alice can do Thursday after 7" in held["preview"]
        _drain(config)
        assert _rows(config, "SELECT * FROM messages WHERE room_token=?", (parent,)) == []
        _approve(config, ident)
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, ident).status in ("completed", "pending")
        _drain(config)
        _drain(config)
        (row,) = _rows(config, "SELECT * FROM messages WHERE room_token=?", (parent,))
        assert row["body"] == "Alice can do Thursday after 7"
        assert _rows(config, "SELECT state FROM whatsapp_skill_requests")[0]["state"] == "sent"

    def test_room_names_the_target_by_token_or_by_name(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn, name="Book Club")
            web = _web_room(conn)
            by_token = _running(conn, "alice", web)
            by_name = _running(conn, "alice", web)
        assert _hold_post(config, by_token, room=parent)["status"] == "held"
        assert _hold_post(config, by_name, room="book club")["status"] == "held"
        rows = _rows(config, "SELECT destination FROM whatsapp_skill_requests ORDER BY created_at")
        assert all(f'"room_token": "{parent}"' in r["destination"] for r in rows)

    def test_refusals(self, config):
        from istota.relay.requests import RequestError

        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            web = _web_room(conn)
            other_private = _web_room(conn, name="other")
            in_shared = _running(conn, "alice", parent, about_room_token=parent)
            unlinked = _running(conn, "alice", web)
            private_target = _running(conn, "alice", web)
        cases = [
            (in_shared, None, "not_a_private_room"),
            (unlinked, None, "no_target_room"),
            (private_target, other_private, "parent_unavailable"),
            (unlinked, "no such room", "parent_unavailable"),
        ]
        for ident, room, code in cases:
            with pytest.raises(RequestError, match=code):
                _hold_post(config, ident, room=room, key=f"k{ident}{code[:3]}")
        # A member who left: the link no longer reaches the room.
        with db.get_db(config.db_path) as conn:
            bobs_web = _web_room(conn, user="bob", name="bob's")
            bob_gone = _running(conn, "bob", bobs_web, about_room_token=parent)
            db.drop_web_room_member(conn, parent, "bob")
        with pytest.raises(RequestError, match="parent_unavailable"):
            _hold_post(config, bob_gone, user="bob")

    def test_a_preview_a_phone_room_would_cut_short_is_refused(self, config):
        from istota.relay.requests import RequestError

        config.sms.enabled = True
        config.sms.max_segments = 4
        config.users["alice"].sms_phone_number = "+15551234567"
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            sms = _phone_room(conn, surface="sms")
            ident = _running(conn, "alice", sms, source_type="sms", about_room_token=parent)
            short = _running(conn, "alice", sms, source_type="sms", about_room_token=parent)
        with pytest.raises(RequestError, match="invalid_preview"):
            _hold_post(config, ident, text="x" * 1900)
        assert _hold_post(config, short, text="Friday works", key="p2")["status"] == "held"

    def test_a_member_removed_before_delivery_posts_nothing(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            bobs_web = _web_room(conn, user="bob", name="bob's")
            ident = _running(conn, "bob", bobs_web, about_room_token=parent)
        _hold_post(config, ident, user="bob", text="hello all")
        _approve(config, ident)
        with db.get_db(config.db_path) as conn:
            db.drop_web_room_member(conn, parent, "bob")
        _drain(config)
        assert _rows(config, "SELECT * FROM messages WHERE room_token=?", (parent,)) == []
        assert _rows(config, "SELECT state FROM whatsapp_skill_requests")[0]["state"] == "failed"

    @pytest.mark.parametrize("prompt,text,status", [
        ("do not tell them the house sale is off", "the house sale is off", "held"),
        ("post this\nAlice can do Thursday after 7", "Alice can do Thursday after 7", "queued"),
        ("Alice can do Thursday after 7", "Alice can do Thursday after 7", "queued"),
        ("tell them I can make it", "Alice can make it", "held"),
    ])
    def test_the_clean_turn_takes_whole_units_of_the_members_words(
        self, config, prompt, text, status,
    ):
        _parent, _web, ident = self._linked(config, prompt=prompt)
        with db.get_db(config.db_path) as conn:
            db.record_attempt_tool_call(conn, ident, calls_seen=1, first_is_relay=True)
        released = _hold_post(config, ident, text=text)
        assert released["status"] == status
        if status == "queued":
            assert released["approval"] == "clean_turn"

    def test_the_trace_hides_both_verbs_and_counts_a_lone_post(self):
        from istota.agent.events import _lone_relay_ask, _private_relay_tool
        assert _private_relay_tool("Bash", {"command": "istota-skill room whisper --request-key a 'x'"})
        assert _private_relay_tool("Bash", {"command": "istota-skill room post --request-key a 'x'"})
        assert _lone_relay_ask("Bash", {"command": "istota-skill room post --request-key a 'x'"})
        assert not _lone_relay_ask("Bash", {"command": "istota-skill room whisper --request-key a 'x'"})

    def test_the_cli_takes_room(self):
        from istota.skills.room import build_parser

        args = build_parser().parse_args(["post", "--request-key", "k", "--room", "Family", "hi"])
        assert args.room == "Family" and args.text == "hi"
        assert build_parser().parse_args(["post", "--request-key", "k", "hi"]).room is None


QUESTION = "I need your confirmation before sending the invite. Reply yes or no."


def _park_privately(conn, config, parent, user="alice", *, prompt=QUESTION):
    ident = _running(conn, user, parent)
    db.set_task_confirmation(conn, ident, prompt)
    task = db.get_task(conn, ident)
    delivery = private_replies.deliver_private(
        conn, config, user_id=user, about_token=private_replies.park_about(conn, task),
        kind="confirmation", reference=f"{ident}:abc", body=prompt)
    # As the scheduler's park does, room or bell alike.
    db.set_task_private_park(conn, ident)
    return ident, delivery


class TestConfirmations:
    def test_a_bare_answer_resolves_from_the_private_room_not_the_shared_room(self, config):
        from istota import confirmations

        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            web = _web_room(conn)
            bobs = _web_room(conn, user="bob", name="bob's")
            ident, delivery = _park_privately(conn, config, parent)
            assert delivery.dest.room_token == web
            assert confirmations.resolve(conn, "alice", conversation_token=parent).task is None
            found = confirmations.resolve(conn, "alice", conversation_token=web)
            assert found.task is not None and found.task.id == ident
            # Bob's private room is not Alice's question.
            assert confirmations.resolve(conn, "bob", conversation_token=bobs).task is None

    def test_two_open_in_one_private_room_ask_which(self, config):
        from istota import confirmations

        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            other = _shared_web(conn, name="Work")
            web = _web_room(conn)
            first, _ = _park_privately(conn, config, parent)
            second, _ = _park_privately(conn, config, other)
            found = confirmations.resolve(conn, "alice", conversation_token=web)
        assert found.task is None
        assert sorted(t.id for t in found.ambiguous) == [first, second]

    def test_the_principals_next_room_message_does_not_cancel_it(self, config):
        from istota import confirmations

        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            _web_room(conn)
            ident, _ = _park_privately(conn, config, parent)
            # Control: a question asked in the room itself is still cancelled.
            plain = _running(conn, "alice", parent)
            db.set_task_confirmation(conn, plain, "Proceed?")
            assert confirmations.cancel_for_conversation(conn, parent, "alice") == 1
            assert db.get_task(conn, ident).status == "pending_confirmation"
            assert db.get_task(conn, plain).status == "cancelled"

    def test_it_does_not_hold_the_room_for_the_other_members(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            _web_room(conn)
            _park_privately(conn, config, parent)
            bobs = db.create_task(conn, user_id="bob", source_type="web", prompt="hi",
                                  conversation_token=parent)
        with db.get_db(config.db_path) as conn:
            claimed = db.claim_task(conn, "w1")
        assert claimed is not None and claimed.id == bobs

    def test_through_the_scheduler_the_question_reaches_the_private_talk_room(
        self, config, monkeypatch, fake_talk,
    ):
        from istota.config import SchedulerConfig
        from istota.scheduler import process_one_task

        config.email = EmailConfig(enabled=False)
        config.scheduler = SchedulerConfig()
        config.workspace_path = config.db_path.parent / "mount"
        config.workspace_path.mkdir()
        monkeypatch.setattr("istota.nextcloud.talk.TalkClient.get_participants",
                            AsyncMock(return_value=PARTICIPANTS_ALICE))
        fake_talk.db_path = config.db_path
        with db.get_db(config.db_path) as conn:
            group = plain_talk_room(conn, "alice", token="groupref", name="Family")
            db.add_room_member(conn, group.canonical, "bob")
            private = plain_talk_room(conn, "alice", name="talk")
            ident = db.create_task(conn, prompt="invite them", user_id="alice",
                                   source_type="talk", conversation_token=group.canonical,
                                   is_group_chat=True)
        before = _room_count(config)
        with patch("istota.scheduler.execute_task", return_value=(True, QUESTION, None, None)):
            process_one_task(config)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, ident)
        assert task.status == "pending_confirmation"
        room_calls = fake_talk.calls_to(group.talk_ref)
        assert all(QUESTION not in str(c.args) for c in room_calls)
        (row,) = _rows(config, "SELECT * FROM messages WHERE room_token=?", (private.canonical,))
        assert QUESTION in row["body"] and row["about_room_token"] == group.canonical
        (send,) = fake_talk.calls_to(private.talk_ref, method="send_message")
        assert send.args["message"].startswith("re: Family")
        assert fake_talk.refusals == []
        # A reply to that post answers the question (Path A).
        assert task.talk_response_id is not None
        assert _room_count(config) == before

    def test_with_no_private_room_the_bell_is_delivered_and_the_room_told(
        self, config, monkeypatch, fake_talk,
    ):
        from istota.config import SchedulerConfig
        from istota.scheduler import process_one_task

        config.email = EmailConfig(enabled=False)
        config.scheduler = SchedulerConfig()
        config.workspace_path = config.db_path.parent / "mount"
        config.workspace_path.mkdir()
        monkeypatch.setattr("istota.nextcloud.talk.TalkClient.get_participants",
                            AsyncMock(return_value=PARTICIPANTS_SHARED))
        fake_talk.db_path = config.db_path
        delivered = []
        monkeypatch.setattr("istota.scheduler.deliver_pending",
                            lambda config, results: delivered.extend(results))
        with db.get_db(config.db_path) as conn:
            group = plain_talk_room(conn, "alice", token="groupref", name="Family")
            db.add_room_member(conn, group.canonical, "bob")
            ident = db.create_task(conn, prompt="invite them", user_id="alice",
                                   source_type="talk", conversation_token=group.canonical,
                                   is_group_chat=True)
        with patch("istota.scheduler.execute_task", return_value=(True, QUESTION, None, None)):
            process_one_task(config)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, ident)
        assert task.status == "pending_confirmation"
        sends = [c.args["message"] for c in fake_talk.calls_to(group.talk_ref,
                                                               method="send_message")]
        assert any(s.endswith(private_replies.SHARED_ROOM_NOTICE) for s in sends)
        assert not any(QUESTION in s for s in sends)
        assert task.talk_response_id is None
        assert any(getattr(r, "notification_id", None) for r in delivered)

    def test_a_park_with_no_private_room_neither_holds_the_room_nor_is_cancelled(
        self, config, monkeypatch, fake_talk,
    ):
        """The bell case (Stage 5): no row is written anywhere, so the park is
        known private by `tasks.private_park` alone."""
        from istota import confirmations
        from istota.config import SchedulerConfig
        from istota.scheduler import process_one_task

        config.email = EmailConfig(enabled=False)
        config.scheduler = SchedulerConfig()
        config.workspace_path = config.db_path.parent / "mount"
        config.workspace_path.mkdir()
        monkeypatch.setattr("istota.nextcloud.talk.TalkClient.get_participants",
                            AsyncMock(return_value=PARTICIPANTS_SHARED))
        monkeypatch.setattr("istota.scheduler.deliver_pending", lambda config, results: None)
        fake_talk.db_path = config.db_path
        with db.get_db(config.db_path) as conn:
            group = plain_talk_room(conn, "alice", token="groupref", name="Family")
            db.add_room_member(conn, group.canonical, "bob")
            ident = db.create_task(conn, prompt="invite them", user_id="alice",
                                   source_type="talk", conversation_token=group.canonical,
                                   is_group_chat=True)
        with patch("istota.scheduler.execute_task", return_value=(True, QUESTION, None, None)):
            process_one_task(config)
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, ident).status == "pending_confirmation"
            assert conn.execute("SELECT COUNT(*) FROM messages WHERE delivery_reference "
                                "LIKE 'private-confirmation:%'").fetchone()[0] == 0
            assert conn.execute("SELECT private_park FROM tasks WHERE id=?",
                                (ident,)).fetchone()[0] == 1
            bobs = db.create_task(conn, user_id="bob", source_type="talk", prompt="hi",
                                  conversation_token=group.canonical, is_group_chat=True)
        with db.get_db(config.db_path) as conn:
            claimed = db.claim_task(conn, "w1")
        assert claimed is not None and claimed.id == bobs
        with db.get_db(config.db_path) as conn:
            assert confirmations.cancel_for_conversation(conn, group.canonical, "alice") == 0
            assert db.get_task(conn, ident).status == "pending_confirmation"

    def test_a_park_asked_in_the_room_still_holds_it(self, config):
        """The control for the flag: the same park without it holds the gate."""
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            ident = _running(conn, "alice", parent)
            db.set_task_confirmation(conn, ident, QUESTION)
            bobs = db.create_task(conn, user_id="bob", source_type="web", prompt="hi",
                                  conversation_token=parent)
        with db.get_db(config.db_path) as conn:
            assert db.claim_task(conn, "w1") is None
            db.set_task_private_park(conn, ident)
        with db.get_db(config.db_path) as conn:
            claimed = db.claim_task(conn, "w1")
        assert claimed is not None and claimed.id == bobs

    def test_parking_again_clears_the_flag(self, config):
        with db.get_db(config.db_path) as conn:
            parent = _shared_web(conn)
            ident = _running(conn, "alice", parent)
            db.set_task_confirmation(conn, ident, QUESTION)
            db.set_task_private_park(conn, ident)
            db.set_task_confirmation(conn, ident, QUESTION)
            assert conn.execute("SELECT private_park FROM tasks WHERE id=?",
                                (ident,)).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# The WhatsApp happy path, end to end through the webhook handler
# ---------------------------------------------------------------------------


def _group_roster():
    from istota.transport.whatsapp import baileys_protocol as proto

    return proto.group_roster({
        "group_jid": GROUP_JID, "subject": "Family", "added_by": ALICE_JID,
        "bot_present": True,
        "participants": [{"jid": ALICE_JID, "lid": ""},
                         {"jid": "15557654321@s.whatsapp.net", "lid": ""}],
    })


def _group_message(text, *, ident="G1"):
    import time

    from istota.transport.whatsapp import baileys_protocol as proto

    return proto.inbound_event({
        "message_id": ident, "jid": GROUP_JID, "group": True, "sender_jid": ALICE_JID,
        "sender_lid": "", "mentions_bot": True, "mentions": [], "message_type": "text",
        "text": text, "username": "Alice", "timestamp": int(time.time()),
    })


class TestTheWhatsAppHappyPath:
    def test_answer_privately_then_quote_the_answer_and_post_into_the_group(
        self, config, sent, monkeypatch,
    ):
        from istota.config import SchedulerConfig
        from istota.rooms import private_replies
        from istota.scheduler import process_one_task

        config.email = EmailConfig(enabled=False)
        config.scheduler = SchedulerConfig()
        config.workspace_path = config.db_path.parent / "mount"
        config.workspace_path.mkdir()
        config.talk = TalkConfig(enabled=False)
        with db.get_db(config.db_path) as conn:
            db.set_whatsapp_binding(conn, "bob", bootstrap_phone_number="+15557654321")
            db.latch_whatsapp_jid(conn, "bob", jid="15557654321@s.whatsapp.net")

        # Alice's own chat with the bot exists: her first message minted it.
        hello = _receive(config, _baileys_event("hi", ident="P1"))
        with db.get_db(config.db_path) as conn:
            mine = db.get_task(conn, hello.task_id).conversation_token
            conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (hello.task_id,))
        _receive(config, _group_roster())
        rooms_before = _room_count(config)

        # 1. In the group she asks for a private answer.
        asked = _receive(config, _group_message("Istota, am I free friday? tell me privately"))
        with db.get_db(config.db_path) as conn:
            group = db.get_task(conn, asked.task_id).conversation_token
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (asked.task_id,))
            again = private_replies.queue_private_answer(
                conn, config, actor_user_id="alice", task_id=asked.task_id)["task_id"]
            conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (asked.task_id,))
            reasked = db.get_task(conn, again)
        assert reasked.conversation_token == mine and reasked.about_room_token == group

        # It runs as her own turn there and is answered in her own chat.
        with patch("istota.scheduler.execute_task",
                   return_value=(True, "Friday is free.", None, None)):
            assert process_one_task(config) == (again, True)
        answers = [r for r in sent if r.to == ALICE_JID and "Friday is free." in r.text]
        assert len(answers) == 1
        assert not [r for r in sent if r.to == GROUP_JID]
        answer_id = f"BOT{sent.index(answers[0]) + 1}"

        # 2. She quotes that answer: the turn is linked to the group.
        quoted = _receive(config, _baileys_event("post in the group that Friday works",
                                                 quote=answer_id, ident="P2"))
        with db.get_db(config.db_path) as conn:
            linked = db.get_task(conn, quoted.task_id)
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (linked.id,))
        assert linked.conversation_token == mine and linked.about_room_token == group

        held = _hold_post(config, linked.id, text="Friday works")
        assert held["status"] == "held"
        _approve(config, linked.id)
        _drain(config)
        into_group = [r for r in sent if r.to == GROUP_JID]
        assert [r.text for r in into_group] == ["Friday works"]
        assert _room_count(config) == rooms_before


# ---------------------------------------------------------------------------
# My notes on every shared-room surface (Stage 5)
# ---------------------------------------------------------------------------


def _guest_participant(conn, room, ref="15550001111@s.whatsapp.net"):
    return conn.execute(
        "INSERT INTO room_participants (room_token,surface,surface_ref,kind) "
        "VALUES (?,'whatsapp',?,'guest')", (room, ref)).lastrowid


class TestMyNotesOnEverySurface:
    def test_a_whatsapp_group_members_turn_reads_their_notes(self, config):
        with db.get_db(config.db_path) as conn:
            group = _whatsapp_group(conn)
            ident = db.create_task(conn, prompt="hi", user_id="bob", source_type="whatsapp",
                                   conversation_token=group, is_group_chat=True)
            assert private_replies.my_notes_room(conn, db.get_task(conn, ident)) == group

    def test_an_email_thread_members_turn_reads_their_notes(self, config):
        with db.get_db(config.db_path) as conn:
            thread = _email_thread(conn)
            ident = db.create_task(conn, prompt="hi", user_id="alice", source_type="email",
                                   conversation_token=thread)
            db.add_message(conn, thread, role="user", body="hi", origin_surface="email",
                           task_id=ident, author_user_id="alice")
            assert private_replies.my_notes_room(conn, db.get_task(conn, ident)) == thread

    def test_an_outsiders_mail_in_a_thread_room_reads_none(self, config):
        """It runs as the host with no guest id; the stored turn says whose it is."""
        with db.get_db(config.db_path) as conn:
            thread = _email_thread(conn)
            ident = db.create_task(conn, prompt="hi", user_id="alice", source_type="email",
                                   conversation_token=thread)
            db.add_message(conn, thread, role="user", body="hi", origin_surface="email",
                           task_id=ident, author_label="mallory@example.com")
            assert private_replies.my_notes_room(conn, db.get_task(conn, ident)) is None

    def test_an_email_turn_with_no_stored_row_reads_none(self, config):
        with db.get_db(config.db_path) as conn:
            thread = _email_thread(conn)
            ident = db.create_task(conn, prompt="hi", user_id="alice", source_type="email",
                                   conversation_token=thread)
            assert private_replies.my_notes_room(conn, db.get_task(conn, ident)) is None

    def test_an_sms_turn_still_reads_none(self, config):
        with db.get_db(config.db_path) as conn:
            group = _whatsapp_group(conn)
            ident = db.create_task(conn, prompt="hi", user_id="alice", source_type="sms",
                                   conversation_token=group)
            assert private_replies.my_notes_room(conn, db.get_task(conn, ident)) is None

    def test_a_guest_turn_in_a_whatsapp_group_under_direct_reads_none(self, config):
        from istota.rooms import policy as room_policy

        with db.get_db(config.db_path) as conn:
            group = _whatsapp_group(conn)
            room_policy.ensure_policy(conn, group)
            ident = db.create_task(conn, prompt="hi", user_id="alice", source_type="whatsapp",
                                   conversation_token=group, is_group_chat=True,
                                   guest_participant_id=_guest_participant(conn, group))
            room_policy.set_guest_reply(conn, group, room_policy.DIRECT)
            assert private_replies.my_notes_room(conn, db.get_task(conn, ident)) is None
            # The control: the same turn under `held` reads the host's notes.
            room_policy.set_guest_reply(conn, group, "held")
            assert private_replies.my_notes_room(conn, db.get_task(conn, ident)) == group
