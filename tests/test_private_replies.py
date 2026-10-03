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
        with patch("istota.rooms.side_rooms._send_private_mail") as send:
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
        with patch("istota.rooms.side_rooms._send_private_mail") as send:
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
        from istota.rooms.side_rooms import pin_plan
        from istota.transport.routing import Destination
        into = [Destination("web", parent, "push"), Destination("talk", "family-talk", "push")]
        assert pin_plan(config, task, into, fallback=lambda: []) == []
        assert pin_plan(config, control, into, fallback=lambda: []) == into

    def test_the_fallback_is_used_only_when_the_plan_empties(self, config):
        from istota.rooms.side_rooms import pin_plan
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
        from istota.rooms.side_rooms import pin_plan
        from istota.transport.routing import Destination

        with db.get_db(config.db_path) as conn:
            web = _web_room(conn)
            ident = db.create_task(conn, user_id="alice", source_type="web", prompt="x",
                                   conversation_token=web)
            task = db.get_task(conn, ident)
        plan = [Destination("web", web, "push")]
        assert pin_plan(config, task, plan, fallback=lambda: []) == plan
