"""The participant veto and the bot's announcement (multiplayer Stage 20).

D8: anyone in a room more than one human reads, guest included, switches the
bot off there with `!<bot name> off` (or, on WhatsApp, by removing its number).
D12: while it is off nothing is recorded — no transcript row, no classifier
call, no task — until a member turns it back on and everyone who switched it
off has agreed with their own `!<bot name> on`, or has left. The bot announces
itself once in a room a guest is in. D20: a held email proposal the host
approved is not held a second time when the send matches what was approved.

Each surface is driven through its own production entry point: the Talk poll,
`handle_whatsapp_batch`, `poll_emails`, the web send route, and the request
drain for the D20 release.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from istota import confirmations, db
from istota.rooms import policy as room_policy
from istota.rooms import veto as room_veto
from istota.rooms import side_rooms
from istota.relay import requests
from istota.config import Config, NextcloudConfig, TalkConfig, UserConfig
from istota.transport._types import ParticipantRef
from istota.transport.ingest import classify_ahead, record_inbound

from .support.rooms import plain_talk_room


def _config(tmp_path):
    path = tmp_path / "state.db"
    db.init_db(path)
    return Config(
        db_path=path,
        temp_dir=tmp_path / "temp",
        bot_name="Zorg",
        nextcloud=NextcloudConfig(url="https://cloud.example.com", username="bot",
                                  app_password="secret"),
        talk=TalkConfig(enabled=True, bot_username="bot"),
        users={"alice": UserConfig(display_name="Alice"),
               "bob": UserConfig(display_name="Bob")},
    )


@pytest.fixture
def config(tmp_path):
    return _config(tmp_path)


def _max(ref="guests/max"):
    return ParticipantRef(surface="talk", surface_ref=ref, display_name="Max")


def _alice():
    return ParticipantRef(surface="talk", surface_ref="alice", user_id="alice")


def _group(conn, token="grp"):
    """A Talk room Alice created, with Max, a guest, on its roster."""
    shape = plain_talk_room(conn, "alice", token=token, name="Family")
    db.upsert_room_participant(conn, room_token=shape.canonical, surface="talk",
                               surface_ref="alice", kind="principal", user_id="alice",
                               acknowledged=True)
    db.upsert_room_participant(conn, room_token=shape.canonical, surface="talk",
                               surface_ref="guests/max", kind="guest",
                               display_name="Max", acknowledged=True)
    return shape


def _guest_turn(conn, config, text="can Alice do Thursday?", token="grp"):
    return record_inbound(
        conn, config, surface="talk", surface_ref=token, user_id="", text=text,
        is_group_chat=True, addressed_to_bot=True, author=_max(),
    )


def _member_turn(conn, config, user="alice", text="hello", token="grp"):
    return record_inbound(
        conn, config, surface="talk", surface_ref=token, user_id=user, text=text,
        is_group_chat=True, addressed_to_bot=True,
    )


def _count(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


class TestTheCommand:
    @pytest.mark.parametrize("text,verb", [
        ("!zorg off", "off"),
        ("!Zorg OFF", "off"),
        ("  !zorg on  ", "on"),
        ("!zorg off.", "off"),
        ("!zorg  on!", "on"),
    ])
    def test_the_bots_own_name(self, text, verb):
        assert room_veto.parse_command(text, "Zorg") == verb

    @pytest.mark.parametrize("text", [
        "!zorgy off", "!zorg offline", "please !zorg off", "!relay off",
        "!zorg", "zorg off", "", None,
    ])
    def test_anything_else_is_not_the_veto(self, text):
        assert room_veto.parse_command(text, "Zorg") is None

    def test_a_name_with_a_space_is_typed_without_it(self):
        assert room_veto.parse_command("!zorgbot off", "Zorg Bot") == "off"
        assert room_veto.parse_command("!zorg bot off", "Zorg Bot") == "off"


# ---------------------------------------------------------------------------
# Off records nothing (D12)
# ---------------------------------------------------------------------------


class TestOffRecordsNothing:
    def test_a_guest_switches_the_bot_off_and_nothing_is_recorded(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            outcome = room_veto.apply(conn, config, room_token="grp", author=_max(),
                                      verb="off")
            assert outcome.state == "off"
            assert room_veto.is_vetoed(conn, "grp")
            before = (_count(conn, "SELECT COUNT(*) FROM messages WHERE role='user'"),
                      _count(conn, "SELECT COUNT(*) FROM speech_gate_decisions"),
                      _count(conn, "SELECT COUNT(*) FROM tasks"))

            guest = _guest_turn(conn, config)
            member = _member_turn(conn, config)

            after = (_count(conn, "SELECT COUNT(*) FROM messages WHERE role='user'"),
                     _count(conn, "SELECT COUNT(*) FROM speech_gate_decisions"),
                     _count(conn, "SELECT COUNT(*) FROM tasks"))
        assert guest.outcome == member.outcome == "dropped"
        assert guest.task_id is member.task_id is None
        assert after == before

    def test_the_notice_is_in_the_room_and_names_the_way_back(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            outcome = room_veto.apply(conn, config, room_token="grp", author=_max(),
                                      verb="off")
            rows = conn.execute(
                "SELECT role, body FROM messages WHERE room_token='grp'",
            ).fetchall()
        assert "!zorg on" in outcome.text
        assert [tuple(r) for r in rows] == [("system", outcome.text)]

    def test_a_private_room_is_not_vetoed(self, config):
        with db.get_db(config.db_path) as conn:
            plain_talk_room(conn, "alice", token="dm", name="Alice")
            outcome = room_veto.apply(conn, config, room_token="dm", author=_alice(),
                                      verb="off")
            assert outcome is None
            assert not room_veto.is_vetoed(conn, "dm")
            assert _member_turn(conn, config, token="dm").outcome == "created"

    def test_queued_work_in_the_room_is_cancelled(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            queued = _member_turn(conn, config).task_id
            room_veto.apply(conn, config, room_token="grp", author=_max(), verb="off")
            assert db.get_task(conn, queued).status == "cancelled"

    def test_an_answer_finishing_after_the_veto_goes_nowhere(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            task = db.get_task(conn, _member_turn(conn, config).task_id)
            assert not room_veto.task_room_vetoed(conn, task)
            room_veto.apply(conn, config, room_token="grp", author=_max(), verb="off")
            assert room_veto.task_room_vetoed(conn, task)

    def test_no_classifier_is_asked_in_a_vetoed_room(self, config):
        config.speech_gate.mode = "classifier"
        with db.get_db(config.db_path) as conn:
            _group(conn)
            db.add_room_member(conn, "grp", "bob")
            room_veto.apply(conn, config, room_token="grp", author=_max(), verb="off")
        with patch("istota.executor.build_speech_gate_completer",
                   side_effect=AssertionError("asked")):
            decision = classify_ahead(
                config, surface="talk", surface_ref="grp", user_id="alice",
                text="anyone?", is_group_chat=True, addressed_to_bot=False,
            )
        assert decision is None

    def test_a_held_post_into_a_vetoed_room_is_refused(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            room_veto.apply(conn, config, room_token="grp", author=_max(), verb="off")
            with pytest.raises(requests.RequestError, match="room_off"):
                side_rooms._post_destination(conn, "grp", "alice")


# ---------------------------------------------------------------------------
# Back on: a member, and the vetoer's agreement (D12)
# ---------------------------------------------------------------------------


class TestBackOn:
    def _off(self, conn, config):
        _group(conn)
        room_veto.apply(conn, config, room_token="grp", author=_max(), verb="off")

    def test_a_member_alone_cannot_override_a_guest_who_is_still_here(self, config):
        with db.get_db(config.db_path) as conn:
            self._off(conn, config)
            outcome = room_veto.apply(conn, config, room_token="grp", author=_alice(),
                                      verb="on")
            assert outcome.state == "waiting"
            assert room_veto.is_vetoed(conn, "grp")
            # The guest agreeing is the second half, and it lifts the veto.
            outcome = room_veto.apply(conn, config, room_token="grp", author=_max(),
                                      verb="on")
            assert outcome.state == "on"
            assert not room_veto.is_vetoed(conn, "grp")
            assert _member_turn(conn, config).outcome == "created"

    def test_the_vetoer_agreeing_first_waits_for_a_member(self, config):
        with db.get_db(config.db_path) as conn:
            self._off(conn, config)
            assert room_veto.apply(conn, config, room_token="grp", author=_max(),
                                   verb="on").state == "waiting"
            assert room_veto.is_vetoed(conn, "grp")
            assert room_veto.apply(conn, config, room_token="grp", author=_alice(),
                                   verb="on").state == "on"
            assert not room_veto.is_vetoed(conn, "grp")

    def test_a_vetoer_who_left_does_not_hold_the_room(self, config):
        with db.get_db(config.db_path) as conn:
            self._off(conn, config)
            db.sync_room_roster(conn, room_token="grp", surface="talk",
                                present=["alice"])
            assert room_veto.apply(conn, config, room_token="grp", author=_alice(),
                                   verb="on").state == "on"

    def test_a_member_who_switched_it_off_switches_it_back_on(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            room_veto.apply(conn, config, room_token="grp", author=_alice(), verb="off")
            assert room_veto.apply(conn, config, room_token="grp", author=_alice(),
                                   verb="on").state == "on"

    def test_every_vetoer_has_to_agree(self, config):
        with db.get_db(config.db_path) as conn:
            self._off(conn, config)
            tina = _max("guests/tina")
            room_veto.apply(conn, config, room_token="grp", author=tina, verb="off")
            room_veto.apply(conn, config, room_token="grp", author=_alice(), verb="on")
            room_veto.apply(conn, config, room_token="grp", author=_max(), verb="on")
            assert room_veto.is_vetoed(conn, "grp")
            assert room_veto.apply(conn, config, room_token="grp", author=tina,
                                   verb="on").state == "on"

    def test_a_guest_cannot_switch_it_back_on_alone(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            room_veto.apply(conn, config, room_token="grp", author=_alice(), verb="off")
            room_veto.apply(conn, config, room_token="grp", author=_alice(), verb="off")
            outcome = room_veto.apply(conn, config, room_token="grp", author=_max(),
                                      verb="on")
            assert outcome.state == "waiting"
            assert room_veto.is_vetoed(conn, "grp")

    def test_an_unauthenticated_member_on_is_agreement_only(self, config):
        """A mail's From proves nothing, so a member's `on` by mail counts as a
        vetoer agreeing and never as the member turning the bot back on."""
        with db.get_db(config.db_path) as conn:
            _group(conn)
            room_veto.apply(conn, config, room_token="grp", author=_alice(), verb="off")
            outcome = room_veto.apply(conn, config, room_token="grp", author=_alice(),
                                      verb="on", authenticated=False)
            assert outcome.state == "waiting"
            assert room_veto.is_vetoed(conn, "grp")

    def test_on_in_a_room_that_is_on_says_so(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            outcome = room_veto.apply(conn, config, room_token="grp", author=_max(),
                                      verb="on")
            assert outcome.state == "not_off"
            assert not room_veto.is_vetoed(conn, "grp")


class TestTheMigration:
    def test_an_upgraded_database_matches_a_fresh_one(self, tmp_path, config):
        import sqlite3

        old = tmp_path / "old.db"
        db.init_db(old)
        raw = sqlite3.connect(old)
        raw.execute("DROP TABLE room_vetoes")
        raw.execute("DROP TABLE room_notices")
        for column in ("vetoed_at", "veto_on_by", "announced_at"):
            raw.execute(f"ALTER TABLE room_policy DROP COLUMN {column}")
        raw.execute("DELETE FROM _migration_state WHERE name = 'room_veto_v1'")
        raw.commit()
        raw.row_factory = sqlite3.Row
        db._run_migrations(raw)
        raw.commit()
        raw.close()
        with db.get_db(config.db_path) as fresh, db.get_db(old) as upgraded:
            for table in ("room_policy", "room_vetoes", "room_notices"):
                a = {r[1]: tuple(r)[2:6] for r in fresh.execute(f"PRAGMA table_info({table})")}
                b = {r[1]: tuple(r)[2:6] for r in upgraded.execute(f"PRAGMA table_info({table})")}
                assert a and a == b, table

    def test_deleting_a_room_deletes_its_vetoes(self, config):
        with db.get_db(config.db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "Plans")
            db.upsert_room_participant(conn, room_token=room.token, surface="talk",
                                       surface_ref="guests/max", kind="guest",
                                       acknowledged=True)
            room_veto.apply(conn, config, room_token=room.token, author=_max(),
                            verb="off")
            handle = next(h for h in db.list_web_chat_rooms(conn, "alice")
                          if h.token == room.token)
            db.delete_web_chat_room(conn, handle.id, "alice")
            assert _count(conn, "SELECT COUNT(*) FROM room_vetoes") == 0


# ---------------------------------------------------------------------------
# Talk
# ---------------------------------------------------------------------------


def _talk_config(tmp_path):
    from istota.config import SchedulerConfig

    path = tmp_path / "talk.db"
    db.init_db(path)
    config = Config(
        db_path=path, temp_dir=tmp_path / "temp", bot_name="Zorg",
        nextcloud=NextcloudConfig(url="https://nc.test", username="istota",
                                  app_password="pass"),
        talk=TalkConfig(enabled=True, bot_username="istota"),
        users={"alice": UserConfig(), "bob": UserConfig()},
        scheduler=SchedulerConfig(),
    )
    config.temp_dir.mkdir(exist_ok=True)
    return config


_ROSTER = [
    {"actorType": "users", "actorId": "alice", "displayName": "Alice"},
    {"actorType": "users", "actorId": "bob", "displayName": "Bob"},
    {"actorType": "guests", "actorId": "max", "displayName": "Max"},
    {"actorType": "users", "actorId": "istota", "displayName": "Zorg"},
]


def _talk_msg(id, actor_id, text, actor_type="users"):
    return {"id": id, "actorId": actor_id, "actorType": actor_type,
            "actorDisplayName": actor_id.title(), "message": text,
            "messageType": "comment", "messageParameters": {}}


async def _poll(config, *msgs, token="group1", history=()):
    from istota.transport.talk import inbound as talk_inbound
    from istota.transport.talk.inbound import poll_talk_conversations

    talk_inbound._participant_cache.clear()
    talk_inbound._conversation_cache = None
    talk_inbound._last_full_sweep = None
    with patch("istota.transport.talk.inbound.get_talk_client") as MockClient:
        client = MockClient.return_value
        client.list_conversations = AsyncMock(return_value=[
            {"token": token, "type": 2, "displayName": "Group"},
        ])
        client.poll_messages = AsyncMock(return_value=list(msgs))
        client.get_participants = AsyncMock(return_value=_ROSTER)
        client.send_message = AsyncMock(return_value={"id": 999})
        client.fetch_chat_history = AsyncMock(return_value=list(history))
        created = await poll_talk_conversations(config)
    return created, client


def _talk_group(config, token="group1"):
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, token, "alice", origin="talk", name="Group")
        db.add_room_binding(conn, token, "talk", token)
        db.add_room_member(conn, token, "alice")
        db.add_room_member(conn, token, "bob")
        db.set_talk_poll_state(conn, token, 50)


class TestOnTalk:
    @pytest.mark.asyncio
    async def test_a_guests_veto_is_heard_and_the_room_records_nothing_after(
        self, tmp_path,
    ):
        config = _talk_config(tmp_path)
        _talk_group(config)

        created, client = await _poll(
            config, _talk_msg(101, "max", "!zorg off", actor_type="guests"),
        )
        assert created == []
        posted = [c.args[1] for c in client.send_message.await_args_list]
        assert len(posted) == 1 and "!zorg on" in posted[0]

        created, client = await _poll(
            config,
            _talk_msg(102, "alice", "@istota what is on today?"),
            _talk_msg(103, "max", "nice weather", actor_type="guests"),
            # An empty cache in a vetoed room is the veto, not a room to
            # backfill from Talk's own history.
            history=[_talk_msg(102, "alice", "@istota what is on today?")],
        )
        assert created == []
        client.send_message.assert_not_awaited()
        with db.get_db(config.db_path) as conn:
            assert room_veto.is_vetoed(conn, "group1")
            cached = [r[0] for r in conn.execute(
                "SELECT message_id FROM talk_messages WHERE conversation_token='group1'")]
            user_rows = _count(conn, "SELECT COUNT(*) FROM messages WHERE role='user'")
            decisions = _count(conn, "SELECT COUNT(*) FROM speech_gate_decisions")
        assert cached == []
        assert user_rows == decisions == 0

    @pytest.mark.asyncio
    async def test_nothing_after_the_veto_in_the_same_batch_is_classified(
        self, tmp_path,
    ):
        config = _talk_config(tmp_path)
        config.speech_gate.mode = "classifier"
        _talk_group(config)
        # Recorded, not raised: `classify_ahead` turns any exception into a
        # failed decision, which would hide the call.
        asked = []
        with patch("istota.executor.build_speech_gate_completer",
                   side_effect=lambda *a, **k: asked.append(k) or (lambda _p: "{}")):
            created, _ = await _poll(
                config,
                _talk_msg(101, "max", "!zorg off", actor_type="guests"),
                _talk_msg(102, "alice", "anyone around?"),
            )
        assert created == []
        assert asked == []
        with db.get_db(config.db_path) as conn:
            assert _count(conn, "SELECT COUNT(*) FROM speech_gate_decisions") == 0

    @pytest.mark.asyncio
    async def test_back_on_the_room_is_recorded_again(self, tmp_path):
        config = _talk_config(tmp_path)
        _talk_group(config)
        await _poll(config, _talk_msg(101, "max", "!zorg off", actor_type="guests"))
        await _poll(config,
                    _talk_msg(102, "alice", "!zorg on"),
                    _talk_msg(103, "max", "!zorg on", actor_type="guests"))
        created, _ = await _poll(config, _talk_msg(104, "alice", "plain chat"))
        with db.get_db(config.db_path) as conn:
            assert not room_veto.is_vetoed(conn, "group1")
            bodies = [r[0] for r in conn.execute(
                "SELECT body FROM messages WHERE role='user'")]
        assert bodies == ["plain chat"]


# ---------------------------------------------------------------------------
# WhatsApp groups
# ---------------------------------------------------------------------------


class TestOnWhatsApp:
    @pytest.fixture
    def wa(self, tmp_path):
        from .support.whatsapp_config import build_whatsapp_config

        path = tmp_path / "wa.db"
        db.init_db(path)
        config = Config(
            db_path=path, temp_dir=tmp_path / "tmp", bot_name="Zorg",
            whatsapp=build_whatsapp_config(
                enabled=True, provider=db.WHATSAPP_BAILEYS_PROVIDER,
                business_phone_number="+15551230000",
            ),
            users={"alice": UserConfig(display_name="Alice")},
        )
        with db.get_db(path) as conn:
            db.set_whatsapp_binding(conn, "alice", bootstrap_phone_number="+15551234567")
            db.latch_whatsapp_jid(conn, "alice", jid=ALICE_JID)
        return config

    def _apply(self, config, *events):
        from istota.transport.whatsapp.webhook import handle_whatsapp_batch

        with db.get_db(config.db_path) as conn:
            return handle_whatsapp_batch(conn, config, list(events),
                                         provider=db.WHATSAPP_BAILEYS_PROVIDER)

    def test_a_guests_veto_is_answered_in_the_group(self, wa):
        self._apply(wa, _wa_roster([ALICE_JID, GUEST_JID], added_by=ALICE_JID))
        (result,) = self._apply(wa, _wa_message("!zorg off", sender=GUEST_JID))

        assert result.disposition == "room_veto"
        assert result.group_post_room == _wa_room(wa)
        assert "!zorg on" in result.response_text
        (later,) = self._apply(wa, _wa_message("hello @zorg", sender=ALICE_JID,
                                                message_id="M2", mentions_bot=True))
        assert later.disposition == "group_vetoed"
        assert later.task_id is None
        with db.get_db(wa.db_path) as conn:
            assert _count(conn, "SELECT COUNT(*) FROM messages WHERE role='user'") == 0

    def test_the_answer_is_sent_into_the_group(self, wa, monkeypatch):
        from istota.transport.whatsapp import outbound
        from istota.transport.whatsapp._types import WhatsAppSendResult
        from istota.transport.whatsapp.providers._types import (
            WhatsAppProviderAdapter, WhatsAppProviderCaps,
        )
        from istota.transport.whatsapp.webhook import deliver_event_responses

        seen = []

        async def send(request):
            seen.append(request)
            return WhatsAppSendResult(message_id=f"BOT{len(seen)}")

        caps = WhatsAppProviderCaps(
            metered=False, has_service_window=False, supports_templates=False,
            delivery_receipts=True, address_field="jid", service_body_limit=4096,
            interactive_body_limit=4096,
        )
        monkeypatch.setattr(outbound, "active_adapter", lambda config: WhatsAppProviderAdapter(
            name="baileys", caps=caps, parse_webhook=None, send=send,
            verify_signature=None))
        self._apply(wa, _wa_roster([ALICE_JID, GUEST_JID], added_by=ALICE_JID))
        results = self._apply(wa, _wa_message("!zorg off", sender=GUEST_JID))
        asyncio.run(deliver_event_responses(wa, results))

        assert [r.to for r in seen] == [WA_GROUP]

    def test_removing_the_bot_is_a_veto_that_outlives_the_re_add(self, wa):
        self._apply(wa, _wa_roster([ALICE_JID, GUEST_JID], added_by=ALICE_JID))
        self._apply(wa, _wa_roster([], bot_present=False))
        self._apply(wa, _wa_roster([ALICE_JID, GUEST_JID]))
        with db.get_db(wa.db_path) as conn:
            assert room_veto.is_vetoed(conn, _wa_room(wa))
        (later,) = self._apply(wa, _wa_message("@zorg hi", sender=ALICE_JID,
                                                message_id="M3", mentions_bot=True))
        assert later.disposition == "group_vetoed"
        # Nobody named as the remover: a member's `on` is enough.
        (on,) = self._apply(wa, _wa_message("!zorg on", sender=ALICE_JID,
                                             message_id="M4"))
        assert on.disposition == "room_veto"
        with db.get_db(wa.db_path) as conn:
            assert not room_veto.is_vetoed(conn, _wa_room(wa))


WA_GROUP = "120363000000000001@g.us"
ALICE_JID = "15551234567@s.whatsapp.net"
GUEST_JID = "15559990000@s.whatsapp.net"


def _wa_room(wa):
    with db.get_db(wa.db_path) as conn:
        return db.resolve_room_token(conn, "whatsapp", WA_GROUP)


def _wa_roster(members, *, added_by="", bot_present=True):
    from istota.transport.whatsapp import baileys_protocol as proto

    return proto.group_roster({
        "group_jid": WA_GROUP, "subject": "Family", "added_by": added_by,
        "bot_present": bot_present,
        "participants": [{"jid": m, "lid": ""} for m in members],
    })


def _wa_message(text, *, sender, message_id="M1", mentions_bot=False):
    from datetime import datetime, timezone

    from istota.transport.whatsapp import baileys_protocol as proto

    return proto.inbound_event({
        "message_id": message_id, "jid": WA_GROUP, "group": True,
        "sender_jid": sender, "sender_lid": "", "mentions_bot": mentions_bot,
        "message_type": "text", "text": text, "username": "Someone",
        "reply_to_message_id": None,
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
    })


# ---------------------------------------------------------------------------
# Email thread rooms
# ---------------------------------------------------------------------------


HOST_ADDR = "carol@test.com"
BOT_ADDR = "bot@test.com"
ALICE_ADDR = "alice@ext.example"
BOB_ADDR = "bob@ext.example"
ROOT = "<root-1@test.com>"
_UID = [500]


def _email_config(tmp_path, *, trusted=("*@ext.example",)):
    from istota.config import EmailConfig

    path = tmp_path / "mail.db"
    db.init_db(path)
    config = Config()
    config.db_path = path
    config.temp_dir = tmp_path / "temp"
    config.temp_dir.mkdir(exist_ok=True)
    config.skills_dir = tmp_path / "skills"
    config.skills_dir.mkdir(exist_ok=True)
    config.bot_name = "Zorg"
    config.email = EmailConfig(
        enabled=True, imap_host="imap.test", imap_port=993, imap_user="user",
        imap_password="pass", smtp_host="smtp.test", smtp_port=587,
        bot_email=BOT_ADDR,
    )
    config.users = {"carol": UserConfig(email_addresses=[HOST_ADDR],
                                        trusted_email_senders=list(trusted))}
    return config


def _mail(config, *, sender, to=(BOT_ADDR,), cc=(), message_id, references=None,
          body="hello", authentication_results=None):
    from istota.skills.email import Email, EmailEnvelope
    from istota.transport.email.inbound import poll_emails

    _UID[0] += 1
    uid = str(_UID[0])
    envelope = EmailEnvelope(id=uid, subject="Dinner", sender=sender,
                             date="Mon, 01 Jan 2026 12:00:00 +0000", is_read=False)
    email = Email(id=uid, subject="Dinner", sender=sender,
                  date="Mon, 01 Jan 2026 12:00:00 +0000", body=body, attachments=[],
                  message_id=message_id, references=references, to=tuple(to),
                  cc=tuple(cc), authentication_results=authentication_results)
    with (
        patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
        patch("istota.transport.email.inbound.read_email", return_value=email),
        patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        patch("istota.transport.email.inbound._deliver_confirmation_prompts"),
        patch("istota.transport.email.inbound._deliver_dmarc_alerts"),
    ):
        return poll_emails(config)


def _thread_room(config):
    with db.get_db(config.db_path) as conn:
        return db.resolve_room_token(conn, "email", ROOT)


def _start_thread(config):
    return _mail(config, sender=HOST_ADDR, to=(BOT_ADDR, ALICE_ADDR), cc=(BOB_ADDR,),
                 message_id=ROOT)


class TestOnEmail:
    def test_a_guests_veto_by_mail_and_the_thread_records_nothing_after(self, tmp_path):
        config = _email_config(tmp_path)
        _start_thread(config)
        assert _mail(config, sender=ALICE_ADDR, cc=(HOST_ADDR, BOB_ADDR),
                     message_id="<a2@ext.example>", references=ROOT,
                     body="!zorg off\n\n> earlier text") == []
        with db.get_db(config.db_path) as conn:
            assert room_veto.is_vetoed(conn, _thread_room(config))
            rows_before = _count(conn, "SELECT COUNT(*) FROM messages WHERE role='user'")

        assert _mail(config, sender=BOB_ADDR, cc=(HOST_ADDR, ALICE_ADDR),
                     message_id="<b3@ext.example>", references=ROOT,
                     body="Zorg, what about Friday?") == []
        with db.get_db(config.db_path) as conn:
            assert _count(conn, "SELECT COUNT(*) FROM messages WHERE role='user'") == rows_before
            processed = conn.execute(
                "SELECT routing_method, subject, task_id FROM processed_emails "
                "ORDER BY id DESC LIMIT 1").fetchone()
        assert tuple(processed) == ("room_off", None, None)

    def test_a_members_on_by_mail_is_agreement_only(self, tmp_path):
        config = _email_config(tmp_path)
        _start_thread(config)
        _mail(config, sender=HOST_ADDR, to=(BOT_ADDR, ALICE_ADDR), cc=(BOB_ADDR,),
              message_id="<c2@test.com>", references=ROOT, body="!zorg off")
        _mail(config, sender=HOST_ADDR, to=(BOT_ADDR, ALICE_ADDR), cc=(BOB_ADDR,),
              message_id="<c3@test.com>", references=ROOT, body="!zorg on")
        with db.get_db(config.db_path) as conn:
            assert room_veto.is_vetoed(conn, _thread_room(config))

    def _guest_off(self, config):
        _start_thread(config)
        _mail(config, sender=ALICE_ADDR, cc=(HOST_ADDR, BOB_ADDR),
              message_id="<a2@ext.example>", references=ROOT, body="!zorg off")

    def _vetoer_agreed(self, config):
        with db.get_db(config.db_path) as conn:
            return conn.execute(
                "SELECT agreed_at FROM room_vetoes WHERE person = ?",
                (f"email:{ALICE_ADDR}",),
            ).fetchone()["agreed_at"] is not None

    def test_a_forged_agreement_is_not_heard(self, tmp_path):
        """A guest's `on` by mail is their agreement only when the receiving
        MTA authenticated the sender: a member could otherwise forge it."""
        config = _email_config(tmp_path)
        self._guest_off(config)
        _mail(config, sender=ALICE_ADDR, cc=(HOST_ADDR, BOB_ADDR),
              message_id="<a3@ext.example>", references=ROOT, body="!zorg on")
        assert not self._vetoer_agreed(config)

    def test_an_authenticated_agreement_is(self, tmp_path):
        config = _email_config(tmp_path)
        self._guest_off(config)
        _mail(config, sender=ALICE_ADDR, cc=(HOST_ADDR, BOB_ADDR),
              message_id="<a3@ext.example>", references=ROOT, body="!zorg on",
              authentication_results="mx.test; spf=pass; dmarc=pass header.from=ext.example")
        assert self._vetoer_agreed(config)
        with db.get_db(config.db_path) as conn:
            assert room_veto.is_vetoed(conn, _thread_room(config))


# ---------------------------------------------------------------------------
# The announcement (D8)
# ---------------------------------------------------------------------------


class TestTheAnnouncement:
    def test_it_names_the_bot_its_host_and_the_off_switch(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            text = room_veto.announcement_text(conn, config, "grp")
        assert "Zorg" in text and "Alice" in text and "!zorg off" in text

    def test_it_is_posted_once_into_a_room_with_a_guest(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
        with patch("istota.transport.talk.TalkTransport.deliver",
                   new=AsyncMock(return_value=77)) as deliver:
            assert asyncio.run(room_veto.drain_room_notices(config)) == 1
            assert asyncio.run(room_veto.drain_room_notices(config)) == 0
        assert deliver.await_count == 1
        assert deliver.await_args.args[0] == "grp"
        with db.get_db(config.db_path) as conn:
            rows = conn.execute(
                "SELECT role, delivery_reference FROM messages WHERE room_token='grp'",
            ).fetchall()
            assert room_policy.get_policy(conn, "grp").announced_at is not None
        assert [tuple(r) for r in rows] == [("system", "room-announce:grp")]

    def test_not_in_a_room_of_members_only_nor_while_off(self, config):
        with db.get_db(config.db_path) as conn:
            shape = plain_talk_room(conn, "alice", token="pair", name="Pair")
            db.add_room_member(conn, shape.canonical, "bob")
            _group(conn)
            room_veto.apply(conn, config, room_token="grp", author=_max(), verb="off")
        with patch("istota.transport.talk.TalkTransport.deliver",
                   new=AsyncMock(return_value=77)) as deliver:
            assert asyncio.run(room_veto.drain_room_notices(config)) == 0
        deliver.assert_not_awaited()

    def test_the_first_mail_on_a_thread_carries_it_once(self, tmp_path):
        from istota.transport.email.outbound import deliver_email_result

        config = _email_config(tmp_path)
        task_ids = _start_thread(config)
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        envelope = json.dumps({"subject": "", "body": "Thursday works.",
                               "format": "plain"})
        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<out@test.com>") as reply:
            asyncio.run(deliver_email_result(config, task, envelope))
            asyncio.run(deliver_email_result(config, task, envelope))
        first, second = (c.kwargs["body"] for c in reply.call_args_list)
        assert first.startswith("Thursday works.") and "!zorg off" in first
        assert second == "Thursday works."


# ---------------------------------------------------------------------------
# D20: a held email proposal is approved once
# ---------------------------------------------------------------------------


def _email_proposal(config, reply="Thursday at 7 suits Carol."):
    """A guest's addressed mail runs as the host; its answer held as a proposal."""
    _start_thread(config)
    (task_id,) = _mail(config, sender=ALICE_ADDR, cc=(HOST_ADDR, BOB_ADDR),
                       message_id="<a2@ext.example>", references=ROOT,
                       body="Can Carol do Thursday?")
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        task = db.get_task(conn, task_id)
        proposal = side_rooms.propose_guest_reply(conn, config, task, reply)
    return task_id, proposal


def _approve(config, task_id):
    with db.get_db(config.db_path) as conn:
        confirmations.approve(conn, db.get_task(conn, task_id), config=config, by="web")


class TestOneApproval:
    def test_the_proposal_names_its_exact_recipients(self, tmp_path):
        config = _email_config(tmp_path, trusted=())
        _task_id, proposal = _email_proposal(config)
        assert f"To: {ALICE_ADDR}" in proposal.preview
        assert f"Cc: {HOST_ADDR}, {BOB_ADDR}" in proposal.preview

    def test_an_approved_proposal_is_sent_without_a_second_hold(self, tmp_path):
        config = _email_config(tmp_path, trusted=())
        task_id, _ = _email_proposal(config)
        _approve(config, task_id)
        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<p@test.com>") as reply:
            asyncio.run(requests.drain_requests(config))
        assert reply.call_count == 1
        assert reply.call_args.kwargs["to_addr"] == ALICE_ADDR
        assert reply.call_args.kwargs["cc"] == [HOST_ADDR, BOB_ADDR]
        assert "Thursday at 7 suits Carol." in reply.call_args.kwargs["body"]
        with db.get_db(config.db_path) as conn:
            assert _count(conn, "SELECT COUNT(*) FROM outbound_drafts") == 0

    def test_a_send_to_different_recipients_is_held_as_before(self, tmp_path):
        config = _email_config(tmp_path, trusted=())
        task_id, _ = _email_proposal(config)
        _approve(config, task_id)
        # A new correspondent writes on the thread before the post goes out:
        # the latest message's people are no longer the approved list.
        _mail(config, sender=BOB_ADDR, cc=(HOST_ADDR, ALICE_ADDR, "dave@ext.example"),
              message_id="<b3@ext.example>", references=ROOT, body="and me")
        with patch("istota.transport.email.outbound.reply_to_email") as reply:
            asyncio.run(requests.drain_requests(config))
        reply.assert_not_called()
        with db.get_db(config.db_path) as conn:
            assert _count(conn, "SELECT COUNT(*) FROM outbound_drafts") == 1


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False


@pytest.mark.skipif(not _has_web_deps, reason="web dependencies not installed")
class TestOnWeb:
    @pytest.fixture
    async def web(self, tmp_path):
        from httpx import ASGITransport, AsyncClient

        from istota.config import SiteConfig, WebConfig
        import istota.webui.app as mod

        path = tmp_path / "web.db"
        db.init_db(path)
        config = Config(
            db_path=path, workspace_path=tmp_path / "mount",
            site=SiteConfig(hostname="example.com"), bot_name="Zorg",
            users={"alice": UserConfig(display_name="Alice")},
            web=WebConfig(enabled=True, port=8766,
                          oauth2_provider="https://cloud.example.com",
                          oauth2_client_id="istota-web", oauth2_client_secret="s",
                          session_secret_key="test-session-key"),
        )
        mod._config = config
        mod.app.state.istota_config = config
        mod._oauth = MagicMock()
        mod._oauth.nextcloud = MagicMock()
        mod._oauth.nextcloud.authorize_access_token = AsyncMock(
            return_value={"user_id": "alice"})
        async with AsyncClient(transport=ASGITransport(app=mod.app),
                               base_url="https://example.com") as client:
            cookies = (await client.get("/istota/callback",
                                        follow_redirects=False)).cookies
            room = (await client.post(
                "/istota/api/chat/rooms", json={"name": "Family"}, cookies=cookies,
                headers={"origin": "https://example.com"})).json()
            with db.get_db(path) as conn:
                db.upsert_room_participant(
                    conn, room_token=room["token"], surface="talk",
                    surface_ref="guests/max", kind="guest", display_name="Max",
                    acknowledged=True)
            yield client, cookies, room, config

    async def _send(self, client, cookies, room, text):
        return await client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages", json={"text": text},
            cookies=cookies, headers={"origin": "https://example.com"})

    async def test_a_member_switches_it_off_and_a_send_is_refused(self, web):
        client, cookies, room, config = web
        off = await self._send(client, cookies, room, "!zorg off")
        assert off.status_code == 200
        assert "!zorg on" in off.json()["inline_result"]
        refused = await self._send(client, cookies, room, "hello")
        assert refused.status_code == 409
        with db.get_db(config.db_path) as conn:
            assert _count(conn, "SELECT COUNT(*) FROM messages WHERE role='user'") == 0
            assert _count(conn, "SELECT COUNT(*) FROM tasks") == 0

    async def test_the_reply_reaches_talk_from_the_scheduler_not_the_request(self, web):
        """The web process holds no WhatsApp bridge, so it owes the room's
        other sides the reply and the scheduler's drain posts it, once."""
        client, cookies, room, config = web
        with db.get_db(config.db_path) as conn:
            db.add_room_binding(conn, room["token"], "talk", "tk-family")
        deliver = AsyncMock(return_value=88)
        with patch("istota.transport.talk.TalkTransport.deliver", new=deliver):
            await self._send(client, cookies, room, "!zorg off")
            deliver.assert_not_awaited()
            assert await room_veto.drain_room_notices(config) == 1
            assert await room_veto.drain_room_notices(config) == 0
        assert deliver.await_count == 1
        assert deliver.await_args.args[0] == "tk-family"
        assert "!zorg on" in deliver.await_args.args[1]
