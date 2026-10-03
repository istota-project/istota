"""Private replies (ISSUE-608): what a shared room has for one member, in their own room.

Stage 1 of the private-replies spec: the resolver that picks a member's own
private room, the record-then-send delivery primitive, the bell fallback when
there is no private room, and the two `about_room_token` columns. Nothing in
the product calls these yet; the call sites move onto them in Stage 3.
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
