"""Multiplayer Stage 19: an email thread with two or more humans besides the bot
is a room (D6, D10).

Driven through `poll_emails` for the inbound half and `deliver_email_result`
for the reply, the two places the behaviour lives. The single-correspondent
mail is the control: it must stay exactly as it was, a task on a thread hash
with no room.
"""

import json
from unittest.mock import patch

import pytest

from istota import db, room_policy, speech_gate
from istota.config import Config, EmailConfig, UserConfig
from istota.memory.sleep_cycle import gather_day_data
from istota.skills.email import Email, EmailEnvelope
from istota.surfaces import is_room_member_for
from istota.transport import classify_ahead
from istota.transport.email import threads
from istota.transport.email.inbound import poll_emails
from istota.transport.email.outbound import deliver_email_result

HOST = "carol"
HOST_ADDR = "carol@test.com"
BOT = "bot@test.com"
ALICE = "alice@ext.example"
BOB = "bob@ext.example"
DAVE = "dave@ext.example"
ROOT = "<root-1@test.com>"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(db_path, tmp_path):
    config = Config()
    config.db_path = db_path
    config.temp_dir = tmp_path / "temp"
    config.temp_dir.mkdir(exist_ok=True)
    config.skills_dir = tmp_path / "skills"
    config.skills_dir.mkdir(exist_ok=True)
    config.bot_name = "Zorg"
    config.email = EmailConfig(
        enabled=True,
        imap_host="imap.test", imap_port=993,
        imap_user="user", imap_password="pass",
        smtp_host="smtp.test", smtp_port=587,
        bot_email=BOT,
    )
    config.users = {
        HOST: UserConfig(
            email_addresses=[HOST_ADDR],
            trusted_email_senders=["*@ext.example"],
        ),
    }
    return config


_UID = [100]


def _poll(config, *, sender, to=(BOT,), cc=(), message_id, references=None,
          subject="Dinner plans", body="hello"):
    """Poll one message; return the created task ids."""
    _UID[0] += 1
    uid = str(_UID[0])
    envelope = EmailEnvelope(
        id=uid, subject=subject, sender=sender,
        date="Mon, 01 Jan 2026 12:00:00 +0000", is_read=False,
    )
    email = Email(
        id=uid, subject=subject, sender=sender,
        date="Mon, 01 Jan 2026 12:00:00 +0000",
        body=body, attachments=[], message_id=message_id,
        references=references, to=tuple(to), cc=tuple(cc),
        authentication_results=None,
    )
    with (
        patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
        patch("istota.transport.email.inbound.read_email", return_value=email),
        patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        patch("istota.transport.email.inbound._deliver_confirmation_prompts"),
        patch("istota.transport.email.inbound._deliver_dmarc_alerts"),
    ):
        return poll_emails(config)


def _room_token():
    return threads.thread_room_token(ROOT)


def _rows(db_path, sql, params=()):
    with db.get_db(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _start_thread(config):
    """The host mails the bot and two friends: a thread with three humans."""
    return _poll(config, sender=HOST_ADDR, to=(BOT, ALICE), cc=(BOB,),
                 message_id=ROOT)


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


class TestAThreadBecomesARoom:
    def test_two_humans_besides_the_bot_make_a_room(self, config, db_path):
        task_ids = _start_thread(config)

        token = _room_token()
        with db.get_db(db_path) as conn:
            room = db.get_room(conn, token)
            assert room is not None and room.origin == "email"
            assert room.user_id == HOST
            assert db.resolve_room_token(conn, "email", ROOT) == token
            task = db.get_task(conn, task_ids[0])
        assert task.conversation_token == token
        assert task.output_target == "email"
        assert task.is_group_chat
        people = {r["surface_ref"]: (r["kind"], r["user_id"]) for r in _rows(
            db_path, "SELECT surface_ref, kind, user_id FROM room_participants "
            "WHERE room_token=? AND left_at IS NULL", (token,))}
        assert people == {
            HOST_ADDR: ("principal", HOST),
            ALICE: ("guest", None),
            BOB: ("guest", None),
        }

    def test_the_first_roster_is_the_epoch_baseline(self, config, db_path):
        _start_thread(config)
        assert _rows(db_path, "SELECT epoch, reason FROM room_epochs WHERE room_token=?",
                     (_room_token(),)) == [{"epoch": 0, "reason": "baseline:email"}]

    def test_the_policy_holds_guest_replies(self, config, db_path):
        _start_thread(config)
        with db.get_db(db_path) as conn:
            policy = room_policy.ensure_policy(conn, _room_token())
        assert policy.guest_reply == "held"
        assert policy.host_user_id == HOST

    def test_a_single_correspondent_mail_stays_as_it_was(self, config, db_path):
        task_ids = _poll(config, sender=HOST_ADDR, to=(BOT,), message_id=ROOT)

        assert _rows(db_path, "SELECT token FROM rooms") == []
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        assert task.conversation_token != _room_token()
        assert not task.is_group_chat

    def test_a_stranger_cannot_mint_a_room_for_the_host(self, config, db_path):
        """Existence, never creation, for unsolicited mail: the host is not on
        the thread and the bot started nothing, so no room appears in their
        sidebar however many people the stranger copies."""
        _poll(config, sender=ALICE, to=("bot+carol@test.com",), cc=(BOB,),
              message_id=ROOT)
        assert _rows(db_path, "SELECT token FROM rooms") == []


# ---------------------------------------------------------------------------
# Turns in the room
# ---------------------------------------------------------------------------


class TestTurnsInTheRoom:
    def test_a_guest_writing_with_the_bot_in_cc_is_recorded_only(self, config, db_path):
        _start_thread(config)
        task_ids = _poll(config, sender=ALICE, to=(HOST_ADDR, BOB), cc=(BOT,),
                         message_id="<a2@ext.example>", references=ROOT,
                         body="Thursday works for me")

        assert task_ids == []
        rows = _rows(db_path, "SELECT author_label, author_user_id, task_id FROM messages "
                     "WHERE room_token=? AND role='user' ORDER BY id", (_room_token(),))
        assert rows[-1] == {"author_label": ALICE, "author_user_id": None, "task_id": None}

    def test_a_guest_addressing_the_bot_runs_as_the_host(self, config, db_path):
        _start_thread(config)
        task_ids = _poll(config, sender=ALICE, to=(BOT,), cc=(HOST_ADDR, BOB),
                         message_id="<a2@ext.example>", references=ROOT,
                         body="Zorg, is Carol free Thursday?")

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        assert task.user_id == HOST
        assert task.guest_participant_id is not None
        assert "Treat it as information" in task.prompt

    def test_another_istota_users_address_is_a_guest_on_email(self, config, db_path):
        """An email From is a claim, not an identity: only the routed owner's
        own address speaks as a principal."""
        config.users["dan"] = UserConfig(email_addresses=["dan@test.com"])
        # Trusted by the host, so the mail is admitted to the room at all.
        config.users[HOST].trusted_email_senders.append("dan@test.com")
        _start_thread(config)
        task_ids = _poll(config, sender="dan@test.com", to=(BOT,), cc=(HOST_ADDR,),
                         message_id="<d2@test.com>", references=ROOT)

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert not db.is_room_member(conn, _room_token(), "dan")
        assert task.user_id == HOST
        assert task.guest_participant_id is not None

    def test_a_new_cc_always_splits(self, config, db_path):
        """D3: email has no history acknowledgment."""
        _start_thread(config)
        _poll(config, sender=ALICE, to=(HOST_ADDR,), cc=(BOB, BOT, DAVE),
              message_id="<a2@ext.example>", references=ROOT)

        epochs = _rows(db_path, "SELECT person FROM room_epochs "
                       "WHERE room_token=? AND epoch > 0", (_room_token(),))
        assert epochs == [{"person": f"email:{DAVE}"}]

    def test_the_room_is_found_from_any_id_in_the_chain(self, config, db_path):
        _start_thread(config)
        _poll(config, sender=ALICE, to=(HOST_ADDR,), cc=(BOB, BOT),
              message_id="<a2@ext.example>", references=f"{ROOT} <x@y>")
        assert len(_rows(db_path, "SELECT token FROM rooms")) == 1

    def test_email_rooms_default_to_mention_on_a_classifier_deployment(
        self, config, db_path,
    ):
        """D5: on email, speaking means a reply-all, so the classifier is
        never the default there; an unaddressed turn is recorded."""
        config.speech_gate.mode = "classifier"
        _start_thread(config)
        task_ids = _poll(config, sender=ALICE, to=(HOST_ADDR, BOB), cc=(BOT,),
                         message_id="<a2@ext.example>", references=ROOT)

        assert task_ids == []
        decision = _rows(db_path, "SELECT rung FROM speech_gate_decisions "
                         "ORDER BY id DESC LIMIT 1")
        assert decision == [{"rung": "mode_mention"}]


# ---------------------------------------------------------------------------
# The reply
# ---------------------------------------------------------------------------


def _structured(body="Thursday at 7 works."):
    return json.dumps({"subject": "", "body": body, "format": "plain"})


class TestTheReplyIsAReplyAll:
    @pytest.mark.asyncio
    async def test_it_goes_to_the_latest_messages_participants(self, config, db_path):
        _start_thread(config)
        # Alice replies and drops Bob from the thread.
        task_ids = _poll(config, sender=ALICE, to=(BOT,), cc=(HOST_ADDR,),
                         message_id="<a2@ext.example>", references=ROOT)
        # The host asks the bot to answer; Bob is not on this message either.
        task_ids = _poll(config, sender=HOST_ADDR, to=(BOT,), cc=(ALICE,),
                         message_id="<c3@test.com>",
                         references=f"{ROOT} <a2@ext.example>")
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_ids[0])

        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<out@test.com>") as reply:
            ok = await deliver_email_result(config, task, _structured())

        assert ok is True
        kwargs = reply.call_args.kwargs
        assert kwargs["to_addr"] == HOST_ADDR
        assert kwargs["cc"] == [ALICE]
        assert kwargs["in_reply_to"] == "<c3@test.com>"
        assert kwargs["references"] == f"{ROOT} <a2@ext.example> <c3@test.com>"
        sent = _rows(db_path, "SELECT to_addr, conversation_token FROM sent_emails")
        assert sent == [{"to_addr": f"{HOST_ADDR}, {ALICE}",
                         "conversation_token": _room_token()}]

    @pytest.mark.asyncio
    async def test_an_untrusted_recipient_holds_the_whole_reply(self, config, db_path):
        """The existing gate, applied to every recipient, and the draft shows
        in the thread's room."""
        config.users[HOST].trusted_email_senders = []
        task_ids = _start_thread(config)
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_ids[0])

        with patch("istota.transport.email.outbound.reply_to_email") as reply:
            ok = await deliver_email_result(config, task, _structured())

        assert ok is True
        reply.assert_not_called()
        drafts = _rows(db_path, "SELECT to_addrs, cc_addrs, room_token FROM outbound_drafts")
        assert len(drafts) == 1
        assert json.loads(drafts[0]["to_addrs"]) == [HOST_ADDR]
        assert json.loads(drafts[0]["cc_addrs"]) == [ALICE, BOB]
        assert drafts[0]["room_token"] == _room_token()

    @pytest.mark.asyncio
    async def test_a_single_correspondent_reply_is_unchanged(self, config, db_path):
        task_ids = _poll(config, sender=HOST_ADDR, to=(BOT,), message_id=ROOT)
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_ids[0])

        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<out@test.com>") as reply:
            await deliver_email_result(config, task, _structured())

        kwargs = reply.call_args.kwargs
        assert kwargs["to_addr"] == HOST_ADDR
        assert not kwargs.get("cc")


# ---------------------------------------------------------------------------
# Stage 15's obligation: no mirror into a shared origin room
# ---------------------------------------------------------------------------


class TestNoMirrorIntoASharedRoom:
    def test_an_emissary_reply_is_not_mirrored_into_a_shared_origin_room(
        self, config, db_path,
    ):
        config.users["dan"] = UserConfig(email_addresses=["dan@test.com"])
        with db.get_db(db_path) as conn:
            db.register_room(conn, "web-shared", HOST, origin="web")
            db.add_web_room_member(conn, "web-shared", "dan")
            db.record_sent_email(
                conn, user_id=HOST, message_id="<out-1@test.com>",
                to_addr=ALICE, subject="Question",
                conversation_token="web-shared", origin_target="room:web-shared",
            )
        task_ids = _poll(config, sender=ALICE, to=(BOT,),
                         message_id="<a9@ext.example>",
                         references="<out-1@test.com>", subject="Re: Question")

        assert _rows(db_path, "SELECT id FROM messages WHERE room_token='web-shared'") == []
        with db.get_db(db_path) as conn:
            assert db.get_task(conn, task_ids[0]).withheld_from_room


# ---------------------------------------------------------------------------
# The side room's email view
# ---------------------------------------------------------------------------


class TestTheSideRoomsEmailView:
    @pytest.mark.asyncio
    async def test_a_private_mail_to_the_users_own_address(self, config, db_path):
        from istota import side_rooms

        _start_thread(config)
        with patch("istota.side_rooms._send_private_mail") as send:
            ok = await side_rooms.push_to_email_view(
                config, user_id=HOST, parent_token=_room_token(),
                body="You are free after 7.", reference_id="r1",
            )

        assert ok is True
        kwargs = send.call_args.kwargs
        assert kwargs["to"] == HOST_ADDR
        assert kwargs["subject"].startswith("re: ")
        assert "in_reply_to" not in kwargs and "references" not in kwargs

    @pytest.mark.asyncio
    async def test_nothing_for_a_parent_not_on_email(self, config, db_path):
        from istota import side_rooms

        with db.get_db(db_path) as conn:
            db.register_room(conn, "web-x", HOST, origin="web")
        with patch("istota.side_rooms._send_private_mail") as send:
            ok = await side_rooms.push_to_email_view(
                config, user_id=HOST, parent_token="web-x", body="x",
                reference_id="r1",
            )
        assert ok is False
        send.assert_not_called()

    def test_a_shared_thread_routes_confirmations_to_the_email_view(
        self, config, db_path,
    ):
        from istota import side_rooms

        task_ids = _start_thread(config)
        with db.get_db(db_path) as conn:
            route = side_rooms.confirmation_route(conn, db.get_task(conn, task_ids[0]))
        assert route is not None and route.email_bound


# ---------------------------------------------------------------------------
# D10: the per-room answer
# ---------------------------------------------------------------------------


class TestTheContainerAnswer:
    def test_only_email_and_whatsapp_containers_are_rooms(self):
        assert is_room_member_for("email", room_container=True)
        assert is_room_member_for("whatsapp", room_container=True)
        assert not is_room_member_for("email", room_container=False)
        assert not is_room_member_for("sms", room_container=True)
        assert is_room_member_for("talk", room_container=False)


# ---------------------------------------------------------------------------
# The classifier reaches containers
# ---------------------------------------------------------------------------


class TestTheClassifierReachesContainers:
    def test_classify_ahead_asks_about_a_whatsapp_group(self, config, db_path):
        config.speech_gate.mode = "classifier"
        with db.get_db(db_path) as conn:
            db.register_room(conn, "wa-room", HOST, origin="whatsapp", name="Family")
            db.add_room_binding(conn, "wa-room", "whatsapp", "123@g.us")
            for ref in ("1@s.whatsapp.net", "2@s.whatsapp.net"):
                db.upsert_room_participant(
                    conn, room_token="wa-room", surface="whatsapp",
                    surface_ref=ref, kind="guest", acknowledged=True,
                )
        scripted = lambda prompt: '{"speak": true, "reason": "asked"}'  # noqa: E731
        with patch("istota.executor.build_speech_gate_completer", return_value=scripted):
            decision = classify_ahead(
                config, surface="whatsapp", surface_ref="123@g.us", user_id=HOST,
                text="what time?", is_group_chat=False, addressed_to_bot=False,
                room_container=True, author_label="Max",
            )
        assert decision is not None and decision.speak
        assert decision.rung == speech_gate.RUNG_CLASSIFIER

    def test_classify_ahead_skips_an_email_room_in_mention_mode(self, config, db_path):
        config.speech_gate.mode = "classifier"
        _start_thread(config)
        with patch("istota.executor.build_speech_gate_completer") as build:
            decision = classify_ahead(
                config, surface="email", surface_ref=ROOT, user_id=HOST,
                text="x", is_group_chat=False, addressed_to_bot=False,
                room_container=True,
            )
        assert decision is None
        build.assert_not_called()


# ---------------------------------------------------------------------------
# Memory: a co-participant's fact never reaches the principal's memory
# ---------------------------------------------------------------------------


class TestCoParticipantFactsAreNotExtracted:
    def test_a_shared_room_task_is_not_in_the_days_data(self, config, db_path):
        with db.get_db(db_path) as conn:
            shared = db.create_task(
                conn, prompt="Monika is pregnant, she told me", user_id=HOST,
                source_type="talk", conversation_token="grp", is_group_chat=True,
            )
            private = db.create_task(
                conn, prompt="I moved to Lisbon", user_id=HOST,
                source_type="talk", conversation_token="dm",
            )
            for tid in (shared, private):
                db.update_task_status(conn, tid, "completed", result="Noted.")
            data = gather_day_data(config, conn, HOST, 24, None)

        assert "Lisbon" in data
        assert "Monika" not in data


# ---------------------------------------------------------------------------
# Held posts and whispers reach the thread and the private view
# ---------------------------------------------------------------------------


class TestHeldPostsAndWhispers:
    def test_an_approved_room_post_is_a_reply_all_on_the_thread(self, config, db_path):
        """The held `room post` (and every `guest_reply = held` proposal)
        lands on the thread once approved, through the outbound gate."""
        import asyncio

        from istota import confirmations, side_rooms
        from istota import whatsapp_requests as requests

        _start_thread(config)
        with db.get_db(db_path) as conn:
            side = db.ensure_side_room(conn, _room_token(), HOST)
            ident = db.create_task(conn, user_id=HOST, source_type="web",
                                   prompt="post it", conversation_token=side.token)
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
            side_rooms.hold_room_post(conn, config, actor_user_id=HOST, task_id=ident,
                                      request_key="p1", text="Thursday after 7 works")
            requests.park_question(conn, config, task=db.get_task(conn, ident))
            confirmations.approve(conn, db.get_task(conn, ident), config=config, by="web")
        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<post@test.com>") as reply:
            asyncio.run(requests.drain_requests(config))
            asyncio.run(requests.drain_requests(config))

        kwargs = reply.call_args.kwargs
        assert kwargs["body"] == "Thursday after 7 works"
        assert kwargs["to_addr"] == HOST_ADDR
        assert kwargs["cc"] == [ALICE, BOB]
        assert kwargs["in_reply_to"] == ROOT

    def test_a_whisper_reaches_the_members_own_address(self, config, db_path):
        import asyncio

        from istota import side_rooms
        from istota import whatsapp_requests as requests

        task_ids = _start_thread(config)
        with db.get_db(db_path) as conn:
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_ids[0],))
            side_rooms.enqueue_whisper(conn, config, actor_user_id=HOST,
                                       task_id=task_ids[0], request_key="w1",
                                       text="Only for you.")
        with patch("istota.side_rooms._send_private_mail") as send:
            asyncio.run(requests.drain_requests(config))

        kwargs = send.call_args.kwargs
        assert kwargs["to"] == HOST_ADDR
        assert kwargs["subject"] == "re: Dinner plans"
        assert kwargs["body"] == "Only for you."


class TestTheThreadRoomGate:
    def test_a_sender_not_on_the_thread_is_held_for_confirmation(self, config, db_path):
        """The chain's ids are bearer tokens: somebody who learned the root id
        but was never on the thread meets the untrusted-sender gate."""
        _start_thread(config)
        task_ids = _poll(config, sender="mallory@elsewhere.example", to=(BOT,),
                         message_id="<m1@elsewhere.example>", references=ROOT)

        with db.get_db(db_path) as conn:
            assert db.get_task(conn, task_ids[0]).status == "pending_confirmation"

    def test_a_person_on_the_thread_is_not(self, config, db_path):
        config.users[HOST].trusted_email_senders = []
        _start_thread(config)
        task_ids = _poll(config, sender=ALICE, to=(BOT,), cc=(HOST_ADDR,),
                         message_id="<a2@ext.example>", references=ROOT)

        with db.get_db(db_path) as conn:
            assert db.get_task(conn, task_ids[0]).status == "pending"



# ---------------------------------------------------------------------------
# Review fixes
# ---------------------------------------------------------------------------


class TestAHeldMailStaysOutOfTheRoom:
    def test_a_held_sender_does_not_join_the_thread(self, config, db_path):
        """Held mail does not make its sender one of the thread's people, so
        their next mail is held again rather than passing the gate."""
        _start_thread(config)
        outsider = "mallory@elsewhere.example"
        first = _poll(config, sender=outsider, to=(BOT,),
                      message_id="<m1@elsewhere.example>", references=ROOT)
        second = _poll(config, sender=outsider, to=(BOT,),
                       message_id="<m2@elsewhere.example>", references=ROOT)

        with db.get_db(db_path) as conn:
            assert [db.get_task(conn, t).status for t in first + second] == [
                "pending_confirmation", "pending_confirmation"]
            assert not threads.is_present(conn, _room_token(), outsider)

    def test_a_held_body_is_not_in_the_rooms_transcript(self, config, db_path):
        _start_thread(config)
        _poll(config, sender="mallory@elsewhere.example", to=(BOT,),
              message_id="<m1@elsewhere.example>", references=ROOT, body="EVIL-BODY")

        rows = _rows(db_path, "SELECT body FROM messages WHERE room_token=?",
                     (_room_token(),))
        assert not any("EVIL-BODY" in r["body"] for r in rows)


class TestTheChainFindsTheRoom:
    def test_a_reply_naming_only_a_later_message_lands_in_the_same_room(
        self, config, db_path,
    ):
        _start_thread(config)
        _poll(config, sender=ALICE, to=(HOST_ADDR,), cc=(BOB, BOT),
              message_id="<a2@ext.example>", references=ROOT)
        # A client that keeps In-Reply-To alone, naming Alice's message.
        email_in_reply_to = "<a2@ext.example>"
        _UID[0] += 1
        uid = str(_UID[0])
        envelope = EmailEnvelope(id=uid, subject="Re: Dinner plans", sender=BOB,
                                 date="Mon, 01 Jan 2026 12:00:00 +0000", is_read=False)
        email = Email(id=uid, subject="Re: Dinner plans", sender=BOB,
                      date="Mon, 01 Jan 2026 12:00:00 +0000", body="ok",
                      attachments=[], message_id="<b3@ext.example>",
                      in_reply_to=email_in_reply_to, to=(ALICE, HOST_ADDR),
                      cc=(BOT,), authentication_results=None)
        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.transport.email.inbound._deliver_confirmation_prompts"),
            patch("istota.transport.email.inbound._deliver_dmarc_alerts"),
        ):
            poll_emails(config)

        assert len(_rows(db_path, "SELECT token FROM rooms WHERE origin='email'")) == 1
        rows = _rows(db_path, "SELECT author_label FROM messages WHERE room_token=? "
                     "AND role='user' ORDER BY id", (_room_token(),))
        assert rows[-1]["author_label"] == BOB


    def test_a_reply_to_the_bots_own_mail_alone_lands_in_the_room(self, config, db_path):
        _start_thread(config)
        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn, user_id=HOST, message_id="<bot-out@test.com>", to_addr=ALICE,
                subject="Re: Dinner plans", conversation_token=_room_token(),
            )
        _poll(config, sender=ALICE, to=(HOST_ADDR, BOB), cc=(BOT,),
              message_id="<a5@ext.example>", references="<bot-out@test.com>")

        assert len(_rows(db_path, "SELECT token FROM rooms WHERE origin='email'")) == 1
        rows = _rows(db_path, "SELECT author_label FROM messages WHERE room_token=? "
                     "AND role='user' ORDER BY id", (_room_token(),))
        assert rows[-1]["author_label"] == ALICE


class TestTheReplyIsThreadedToItsTrigger:
    @pytest.mark.asyncio
    async def test_a_later_message_moves_the_recipients_not_the_threading(
        self, config, db_path,
    ):
        task_ids = _poll(config, sender=HOST_ADDR, to=(BOT, ALICE), cc=(BOB,),
                         message_id=ROOT)
        # Alice answers on the thread with the bot only in Cc: recorded, no task.
        _poll(config, sender=ALICE, to=(HOST_ADDR,), cc=(BOT,),
              message_id="<a2@ext.example>", references=ROOT)
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_ids[0])

        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<out@test.com>") as reply:
            await deliver_email_result(config, task, _structured())

        kwargs = reply.call_args.kwargs
        assert kwargs["in_reply_to"] == ROOT
        assert kwargs["to_addr"] == ALICE
        assert kwargs["cc"] == [HOST_ADDR]


class TestAQuestionInAThreadRoomParks:
    @patch("istota.scheduler.post_result_to_email", return_value=True)
    @patch("istota.scheduler.run_coro", return_value=True)
    def test_the_hosts_question_goes_to_the_side_room_not_the_thread(
        self, mock_run_coro, mock_post_email, config, db_path,
    ):
        from istota.scheduler import process_one_task

        task_ids = _start_thread(config)
        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "Shall I tell Alice Thursday works? Please confirm.",
                          None, None),
        ):
            process_one_task(config)

        with db.get_db(db_path) as conn:
            assert db.get_task(conn, task_ids[0]).status == "pending_confirmation"
            side = db.get_side_room(conn, _room_token(), HOST)
            assert side is not None
            assert db.get_messages(conn, side.token)
        mock_post_email.assert_not_called()


class TestAGuestProposalCarriesTheComposedMail:
    def test_the_envelope_body_not_the_narration(self, config, db_path):
        from istota.transport.email.outbound import composed_email_body

        task_ids = _start_thread(config)
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        envelope = json.dumps({"subject": "", "body": "Thursday at 7.", "format": "plain"})

        assert composed_email_body(config, task, envelope) == "Thursday at 7."
        assert composed_email_body(config, task, "plain prose") == "plain prose"
