"""WhatsApp groups as rooms (multiplayer D6, umbrella Stage 18).

The group JID becomes a room binding, its roster the room's participants, and
its turns go through the same `record_inbound` path as a Talk room's. These
drive the Baileys decoder's records through `handle_whatsapp_batch`, the
production entry point both the bridge and the signed webhook call, and check
what the room, its participants, its epochs, its tasks and the send ledger say
afterwards.

Nothing here reaches a real WhatsApp account; the real-group check is owed
before merge.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from istota import confirmations, db, room_policy, side_rooms
from istota import whatsapp_requests as requests
from istota.config import Config, UserConfig
from istota.transport.registry import make_registry
from istota.transport.routing import resolve_delivery_plan
from istota.transport.whatsapp import WhatsAppTransport, outbound
from istota.transport.whatsapp import baileys_protocol as proto
from istota.transport.whatsapp._types import WhatsAppSendResult
from istota.transport.whatsapp.groups import group_room_token
from istota.transport.whatsapp.providers._types import (
    WhatsAppProviderAdapter,
    WhatsAppProviderCaps,
)
from istota.transport.whatsapp.webhook import handle_whatsapp_batch

from .support.whatsapp_config import build_whatsapp_config

BAILEYS = db.WHATSAPP_BAILEYS_PROVIDER
GROUP = "120363000000000001@g.us"
ALICE_JID = "15551234567@s.whatsapp.net"
BOB_JID = "15557654321@s.whatsapp.net"
GUEST_JID = "15559990000@s.whatsapp.net"
GUEST_LID = "277009032835160@lid"
ROOM = group_room_token(GROUP)

CAPS = WhatsAppProviderCaps(
    metered=False, has_service_window=False,
    supports_templates=False, delivery_receipts=True,
    address_field="jid", service_body_limit=4096, interactive_body_limit=4096,
)


@pytest.fixture
def config(tmp_path) -> Config:
    path = tmp_path / "istota.db"
    db.init_db(path)
    config = Config(
        db_path=path,
        temp_dir=tmp_path / "tmp",
        whatsapp=build_whatsapp_config(
            enabled=True, provider=BAILEYS, business_phone_number="+15551230000",
        ),
        users={"alice": UserConfig(display_name="Alice"),
               "bob": UserConfig(display_name="Bob")},
    )
    with db.get_db(path) as conn:
        for user, number, jid in (("alice", "+15551234567", ALICE_JID),
                                  ("bob", "+15557654321", BOB_JID)):
            db.set_whatsapp_binding(conn, user, bootstrap_phone_number=number)
            db.latch_whatsapp_jid(conn, user, jid=jid)
    return config


@pytest.fixture
def sent(monkeypatch):
    """Every request the Baileys adapter was asked to send."""
    requests_seen = []

    async def send(request):
        requests_seen.append(request)
        return WhatsAppSendResult(message_id=f"BOT{len(requests_seen)}")

    adapter = WhatsAppProviderAdapter(
        name="baileys", caps=CAPS, parse_webhook=None, send=send,
        verify_signature=None,
    )
    monkeypatch.setattr(outbound, "active_adapter", lambda config: adapter)
    return requests_seen


def _apply(config, *events):
    with db.get_db(config.db_path) as conn:
        return handle_whatsapp_batch(conn, config, list(events), provider=BAILEYS)


def _roster(members, *, added_by="", bot_present=True, subject="Family"):
    return proto.group_roster({
        "group_jid": GROUP, "subject": subject, "added_by": added_by,
        "bot_present": bot_present,
        "participants": [
            {"jid": m, "lid": ""} if isinstance(m, str) else m for m in members
        ],
    })


def _message(text, *, sender=ALICE_JID, lid="", message_id="M1",
             mentions_bot=False, reply_to=None, username="Someone"):
    return proto.inbound_event({
        "message_id": message_id, "jid": GROUP, "group": True,
        "sender_jid": sender, "sender_lid": lid, "mentions_bot": mentions_bot,
        "message_type": "text", "text": text, "username": username,
        "reply_to_message_id": reply_to,
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
    })


def _rows(config, sql, params=()):
    with db.get_db(config.db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _task(config, task_id):
    with db.get_db(config.db_path) as conn:
        return db.get_task(conn, task_id)


# ---------------------------------------------------------------------------
# The roster makes the room
# ---------------------------------------------------------------------------


class TestTheRosterRegistersTheRoom:
    def test_the_user_who_added_the_bot_hosts_it(self, config):
        (result,) = _apply(config, _roster(
            [ALICE_JID, BOB_JID, GUEST_JID], added_by=BOB_JID,
        ))

        assert result.disposition == "group_registered"
        with db.get_db(config.db_path) as conn:
            room = db.get_room(conn, ROOM)
            assert room.user_id == "bob" and room.origin == "whatsapp"
            assert room.name == "Family"
            assert db.resolve_room_token(conn, "whatsapp", GROUP) == ROOM
            assert sorted(db.list_room_members(conn, ROOM)) == ["alice", "bob"]
            assert room_policy.ensure_policy(conn, ROOM).host_user_id == "bob"
            assert room_policy.get_policy(conn, ROOM).guest_reply == "held"
        kinds = {r["surface_ref"]: r["kind"] for r in _rows(
            config, "SELECT surface_ref, kind FROM room_participants "
            "WHERE room_token=? AND left_at IS NULL", (ROOM,))}
        assert kinds == {ALICE_JID: "principal", BOB_JID: "principal",
                         GUEST_JID: "guest"}

    def test_the_founders_are_the_epoch_baseline(self, config):
        """The group existed before the bot saw it, so nobody on its first
        roster is a joiner (Stage 14's rule, recorded where the roster is
        first known)."""
        _apply(config, _roster([ALICE_JID, GUEST_JID]))

        epochs = _rows(config, "SELECT epoch, reason FROM room_epochs WHERE room_token=?",
                       (ROOM,))
        assert epochs == [{"epoch": 0, "reason": "baseline:whatsapp"}]

    def test_with_no_adder_the_first_principal_hosts(self, config):
        _apply(config, _roster([GUEST_JID, ALICE_JID, BOB_JID], added_by=GUEST_JID))

        assert _rows(config, "SELECT user_id FROM rooms WHERE token=?", (ROOM,)) == [
            {"user_id": "alice"}]

    def test_a_group_with_no_istota_user_is_not_registered(self, config):
        (result,) = _apply(config, _roster([GUEST_JID]))

        assert result.disposition == "group_no_principal"
        assert _rows(config, "SELECT * FROM rooms") == []

    def test_a_cloud_provider_does_not_apply_a_roster(self, config):
        """Cloud API group support is out of scope (D6)."""
        with db.get_db(config.db_path) as conn:
            (result,) = handle_whatsapp_batch(
                conn, config, [_roster([ALICE_JID])],
                provider=db.WHATSAPP_LEGACY_PROVIDER,
            )
        assert result.disposition == "inactive_provider"
        assert _rows(config, "SELECT * FROM rooms") == []


class TestALaterRoster:
    def test_a_join_always_splits(self, config):
        """D3: WhatsApp has no history acknowledgment."""
        _apply(config, _roster([ALICE_JID, BOB_JID]))
        _apply(config, _message("before the guest came"))
        _apply(config, _roster([ALICE_JID, BOB_JID, GUEST_JID]))

        epochs = _rows(config, "SELECT person FROM room_epochs "
                       "WHERE room_token=? AND epoch > 0", (ROOM,))
        assert epochs == [{"person": f"whatsapp:{GUEST_JID}"}]
        with db.get_db(config.db_path) as conn:
            assert db.front_stage_cutoff(conn, ROOM)

    def test_somebody_off_the_roster_has_left(self, config):
        _apply(config, _roster([ALICE_JID, BOB_JID, GUEST_JID]))
        _apply(config, _roster([ALICE_JID, BOB_JID]))

        (row,) = _rows(config, "SELECT left_at FROM room_participants "
                       "WHERE room_token=? AND surface_ref=?", (ROOM, GUEST_JID))
        assert row["left_at"] is not None

    def test_a_number_that_appears_upgrades_the_lid_row_and_its_epoch(self, config):
        """D1: an unmapped LID is a guest, and a later mapping upgrades it."""
        _apply(config, _roster([ALICE_JID, BOB_JID]))
        _apply(config, _message("before the guest came"))
        _apply(config, _roster([ALICE_JID, BOB_JID, {"jid": "", "lid": GUEST_LID}]))
        _apply(config, _roster([ALICE_JID, BOB_JID, {"jid": GUEST_JID, "lid": GUEST_LID}]))

        present = _rows(config, "SELECT surface_ref FROM room_participants "
                        "WHERE room_token=? AND left_at IS NULL ORDER BY id", (ROOM,))
        assert [r["surface_ref"] for r in present] == [ALICE_JID, BOB_JID, GUEST_JID]
        epochs = _rows(config, "SELECT person FROM room_epochs "
                       "WHERE room_token=? AND epoch > 0", (ROOM,))
        assert epochs == [{"person": f"whatsapp:{GUEST_JID}"}]
        with db.get_db(config.db_path) as conn:
            assert db.front_stage_cutoff(conn, ROOM)

    def test_the_bot_removed_archives_the_room(self, config):
        _apply(config, _roster([ALICE_JID, BOB_JID]))
        (result,) = _apply(config, _roster([], bot_present=False))

        assert result.disposition == "group_left"
        assert _rows(config, "SELECT archived FROM rooms WHERE token=?", (ROOM,)) == [
            {"archived": 1}]


class TestTheHostLeaving:
    def test_the_bot_leaves_with_the_host(self, config):
        """D14: the room loses its host and the bot leaves the group."""
        _apply(config, _roster([ALICE_JID, BOB_JID, GUEST_JID], added_by=BOB_JID))
        (result,) = _apply(config, _roster([ALICE_JID, GUEST_JID]))

        assert result.disposition == "host_left"
        assert result.leave_group_jid == GROUP
        with db.get_db(config.db_path) as conn:
            assert room_policy.get_policy(conn, ROOM).host_user_id is None
            assert db.get_room(conn, ROOM).archived

    def test_a_host_listed_by_lid_alone_has_not_left(self, config):
        """A LID-only entry could be anyone, the host included, and a wrong
        answer here leaves the group for good."""
        _apply(config, _roster([ALICE_JID, BOB_JID], added_by=BOB_JID))
        (result,) = _apply(config, _roster([ALICE_JID, {"jid": "", "lid": GUEST_LID}]))

        assert result.disposition == "roster_synced"
        assert result.leave_group_jid is None

    def test_a_host_whose_binding_changed_is_still_in_the_group(self, config):
        _apply(config, _roster([ALICE_JID, BOB_JID], added_by=BOB_JID))
        with db.get_db(config.db_path) as conn:
            conn.execute("DELETE FROM whatsapp_user_bindings WHERE user_id = 'bob'")
        (result,) = _apply(config, _roster([ALICE_JID, BOB_JID]))

        assert result.leave_group_jid is None

    def test_a_lost_leave_is_asked_again_and_the_room_stays_archived(self, config):
        _apply(config, _roster([ALICE_JID, BOB_JID, GUEST_JID], added_by=BOB_JID))
        _apply(config, _roster([ALICE_JID, GUEST_JID]))
        (again,) = _apply(config, _roster([ALICE_JID, GUEST_JID]))

        assert again.leave_group_jid == GROUP
        assert _rows(config, "SELECT archived FROM rooms WHERE token=?", (ROOM,)) == [
            {"archived": 1}]

    def test_the_bot_re_added_revives_the_room(self, config):
        _apply(config, _roster([ALICE_JID, BOB_JID]))
        _apply(config, _roster([], bot_present=False))
        _apply(config, _roster([ALICE_JID, BOB_JID]))

        assert _rows(config, "SELECT archived FROM rooms WHERE token=?", (ROOM,)) == [
            {"archived": 0}]

    def test_a_member_who_leaves_the_group_leaves_the_room(self, config):
        """Membership came from the group: without this, a member who left
        could still read the backstage and post into the group."""
        _apply(config, _roster([ALICE_JID, BOB_JID, GUEST_JID], added_by=ALICE_JID))
        _apply(config, _roster([ALICE_JID, GUEST_JID]))

        with db.get_db(config.db_path) as conn:
            assert db.list_room_members(conn, ROOM) == ["alice"]
            ident = db.create_task(conn, user_id="bob", source_type="web",
                                   prompt="post it", conversation_token=ROOM)
            with pytest.raises(ValueError):
                db.ensure_side_room(conn, ROOM, "bob")
            del ident

    def test_another_member_leaving_is_not_the_hosts_departure(self, config):
        _apply(config, _roster([ALICE_JID, BOB_JID, GUEST_JID], added_by=BOB_JID))
        (result,) = _apply(config, _roster([BOB_JID, GUEST_JID]))

        assert result.disposition == "roster_synced"
        assert result.leave_group_jid is None


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------


@pytest.fixture
def group(config):
    """Alice (host), Bob and a guest in the registered group."""
    _apply(config, _roster([ALICE_JID, BOB_JID, GUEST_JID], added_by=ALICE_JID))
    return config


class TestATurnInTheGroup:
    def test_a_principal_addressing_the_bot_by_name_gets_a_task(self, group):
        (result,) = _apply(group, _message("Istota, when is the dinner?"))

        assert result.disposition == "task"
        task = _task(group, result.task_id)
        assert task.user_id == "alice"
        assert task.source_type == "whatsapp"
        assert task.conversation_token == ROOM
        assert task.is_group_chat
        (row,) = _rows(group, "SELECT author_user_id, task_id FROM messages "
                       "WHERE room_token=?", (ROOM,))
        assert row == {"author_user_id": "alice", "task_id": result.task_id}

    def test_an_unaddressed_turn_is_recorded_and_not_answered(self, group):
        (result,) = _apply(group, _message("see you all at seven"))

        assert result.disposition == "group_recorded"
        assert result.task_id is None
        assert len(_rows(group, "SELECT * FROM messages WHERE room_token=?", (ROOM,))) == 1
        assert _rows(group, "SELECT * FROM tasks") == []

    def test_a_mention_addresses_the_bot(self, group):
        (result,) = _apply(group, _message("@zz can you check", mentions_bot=True))
        assert result.task_id is not None

    def test_a_quoted_reply_to_a_bot_message_addresses_the_bot(self, group):
        with db.get_db(group.db_path) as conn:
            conn.execute(
                "INSERT INTO sent_whatsapp (logical_key, user_id, send_kind, status, "
                "meta_message_id, body_chars, body_sha256, quota_month, "
                "created_at, updated_at) VALUES ('k', 'alice', 'service', "
                "'accepted', 'BOTMSG', 1, 'x', '2026-09', datetime('now'), "
                "datetime('now'))",
            )
        (result,) = _apply(group, _message("and on Friday?", reply_to="BOTMSG"))
        assert result.task_id is not None

    def test_a_guest_seen_only_by_lid_runs_as_the_host(self, group):
        """D1 and D2: a withheld number is a guest, and a guest's turn runs as
        the host with the guest recorded as the author."""
        (result,) = _apply(group, _message(
            "Istota can Alice do Thursday?", sender="", lid=GUEST_LID,
            message_id="M2", username="Max",
        ))

        task = _task(group, result.task_id)
        assert task.user_id == "alice"
        assert task.guest_participant_id is not None
        (row,) = _rows(group, "SELECT author_user_id, author_label FROM messages "
                       "WHERE room_token=?", (ROOM,))
        assert row == {"author_user_id": None, "author_label": "Max"}
        (participant,) = _rows(group, "SELECT kind FROM room_participants "
                               "WHERE id=?", (task.guest_participant_id,))
        assert participant["kind"] == "guest"

    def test_a_guests_command_is_recorded_and_ignored(self, group):
        (result,) = _apply(group, _message(
            "!stop", sender=GUEST_JID, message_id="M3", username="Max",
        ))
        assert result.task_id is None
        assert result.command_text is None

    def test_a_members_command_answers_in_their_own_chat(self, group):
        (result,) = _apply(group, _message("!room host", message_id="M4"))

        assert result.disposition == "command"
        assert result.user_id == "alice"
        assert result.conversation_token == ROOM

    def test_a_redelivered_message_is_a_duplicate(self, group):
        _apply(group, _message("Istota hello"))
        (again,) = _apply(group, _message("Istota hello"))

        assert again.disposition == "duplicate"
        assert len(_rows(group, "SELECT * FROM tasks")) == 1

    def test_a_message_in_an_unregistered_group_is_dropped(self, config):
        (result,) = _apply(config, _message("Istota hello"))

        assert result.disposition == "group_unregistered"
        assert _rows(config, "SELECT * FROM messages") == []

    def test_a_group_image_is_answered_by_nothing(self, group):
        event = proto.inbound_event({
            "message_id": "M5", "jid": GROUP, "group": True,
            "sender_jid": ALICE_JID, "message_type": "unsupported", "text": None,
            "media_name": "0123456789abcdef0123456789abcdef.jpg",
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
        })
        (result,) = _apply(group, event)

        assert event.media is None
        assert result.disposition == "group_unsupported"
        assert result.response_text is None


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


class TestTheAnswerGoesToTheGroup:
    def test_a_group_turns_answer_is_sent_to_the_group(self, group, sent):
        (result,) = _apply(group, _message("Istota, when is the dinner?"))
        task = _task(group, result.task_id)
        plan = resolve_delivery_plan(group, task, make_registry(group))

        assert [(d.surface, d.channel) for d in plan] == [("whatsapp", ROOM)]
        asyncio.run(WhatsAppTransport(group).deliver(ROOM, "At seven.", task=task))
        (request,) = sent
        assert request.to == GROUP
        assert request.text == "At seven."

    def test_a_web_turn_in_the_groups_room_never_reaches_the_group(self, group, sent):
        """D4: the web view of a group is its principals' backstage."""
        with db.get_db(group.db_path) as conn:
            ident = db.create_task(conn, user_id="alice", source_type="web",
                                   prompt="hi", conversation_token=ROOM,
                                   output_target="room")
            task = db.get_task(conn, ident)
        plan = resolve_delivery_plan(group, task, make_registry(group))

        assert all(d.surface != "whatsapp" for d in plan)

    def test_a_direct_chat_task_still_goes_to_the_users_own_chat(self, group, sent):
        from istota.transport.whatsapp import whatsapp_conversation_token

        with db.get_db(group.db_path) as conn:
            ident = db.create_task(conn, user_id="alice", source_type="whatsapp",
                                   prompt="hi",
                                   conversation_token=whatsapp_conversation_token("alice"))
            task = db.get_task(conn, ident)
        asyncio.run(WhatsAppTransport(group).deliver(
            whatsapp_conversation_token("alice"), "hello", task=task))
        (request,) = sent
        assert request.to == ALICE_JID

    def test_an_archived_group_receives_nothing(self, group, sent):
        (result,) = _apply(group, _message("Istota, when is the dinner?"))
        task = _task(group, result.task_id)
        with db.get_db(group.db_path) as conn:
            db.set_room_archived(conn, ROOM, True)

        asyncio.run(WhatsAppTransport(group).deliver(ROOM, "At seven.", task=task))
        assert sent == []
        assert WhatsAppTransport(group).resolve_target(task) is None

    def test_the_prompt_says_a_scheduled_job_cannot_post_into_the_group(self, group):
        from istota.executor import room_identity_line

        (result,) = _apply(group, _message("Istota, when is the dinner?"))
        line = room_identity_line(group, _task(group, result.task_id),
                                  rooms_cli_available=True)

        assert "a WhatsApp group" in line
        assert "cannot post into the group" in line
        assert "target =" not in line


class TestTheSideRoomsWhatsAppView:
    def test_a_groups_confirmation_is_asked_in_the_members_own_chat(self, group, sent):
        (result,) = _apply(group, _message("Istota, book the table?"))
        with db.get_db(group.db_path) as conn:
            route = side_rooms.confirmation_route(conn, db.get_task(conn, result.task_id))
        assert route.whatsapp_bound and route.side_token is not None

        delivered = asyncio.run(side_rooms.push_to_whatsapp_view(
            group, user_id="alice", parent_token=ROOM,
            body=side_rooms.whatsapp_confirmation_body("Book it?", result.task_id),
            reference_id=f"istota:task:{result.task_id}:confirmation",
        ))

        assert delivered
        (request,) = sent
        assert request.to == ALICE_JID
        assert request.text.startswith("re: Family")
        assert f"!confirm {result.task_id} yes" in request.text

    def test_a_long_question_keeps_the_instruction_that_answers_it(self):
        body = side_rooms.whatsapp_confirmation_body("x" * 10000, 7)

        assert body.endswith("`!confirm 7 no`.")
        assert len(body) + len("re: ") + 80 <= 4096

    def test_a_whisper_reaches_the_members_own_chat_headed_with_the_room(self, group, sent):
        with db.get_db(group.db_path) as conn:
            ident = db.create_task(conn, user_id="alice", source_type="whatsapp",
                                   prompt="hi", conversation_token=ROOM)
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
            side_rooms.enqueue_whisper(conn, group, actor_user_id="alice",
                                       task_id=ident, request_key="w1",
                                       text="Only for you.")
        asyncio.run(requests.drain_requests(group))

        (request,) = sent
        assert request.to == ALICE_JID
        assert request.text.startswith("re: Family")
        assert "Only for you." in request.text

    def test_an_approved_room_post_is_sent_into_the_group(self, group, sent):
        """The held `room post` (and with it every `guest_reply = held`
        proposal) lands in the group once approved."""
        with db.get_db(group.db_path) as conn:
            side = db.ensure_side_room(conn, ROOM, "alice")
            ident = db.create_task(conn, user_id="alice", source_type="web",
                                   prompt="post it", conversation_token=side.token)
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
            side_rooms.hold_room_post(conn, group, actor_user_id="alice", task_id=ident,
                                      request_key="p1", text="Thursday after 7 works")
            requests.park_question(conn, group, task=db.get_task(conn, ident))
            confirmations.approve(conn, db.get_task(conn, ident), config=group, by="web")
        asyncio.run(requests.drain_requests(group))
        asyncio.run(requests.drain_requests(group))

        assert [(r.to, r.text) for r in sent] == [(GROUP, "Thursday after 7 works")]
