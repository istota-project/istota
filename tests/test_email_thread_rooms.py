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

from istota import db
from istota.rooms import policy as room_policy
from istota.rooms import speech_gate
from istota.config import Config, EmailConfig, UserConfig
from istota.memory.sleep_cycle import gather_day_data
from istota.skills.email import Email, EmailEnvelope
from istota.rooms.surfaces import is_room_member_for
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


def _room_token(config):
    with db.get_db(config.db_path) as conn:
        return db.resolve_room_token(conn, "email", ROOT)


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

        token = _room_token(config)
        assert db.is_canonical_room_token(token)
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
                     (_room_token(config),)) == [{"epoch": 0, "reason": "baseline:email"}]

    def test_the_policy_holds_guest_replies(self, config, db_path):
        _start_thread(config)
        with db.get_db(db_path) as conn:
            policy = room_policy.ensure_policy(conn, _room_token(config))
        assert policy.guest_reply == "held"
        assert policy.host_user_id == HOST

    def test_a_single_correspondent_mail_stays_as_it_was(self, config, db_path):
        task_ids = _poll(config, sender=HOST_ADDR, to=(BOT,), message_id=ROOT)

        assert _rows(db_path, "SELECT token FROM rooms") == []
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        assert task.conversation_token != _room_token(config)
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
                     "WHERE room_token=? AND role='user' ORDER BY id", (_room_token(config),))
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
            assert not db.is_room_member(conn, _room_token(config), "dan")
        assert task.user_id == HOST
        assert task.guest_participant_id is not None

    def test_a_new_cc_always_splits(self, config, db_path):
        """D3: email has no history acknowledgment."""
        _start_thread(config)
        _poll(config, sender=ALICE, to=(HOST_ADDR,), cc=(BOB, BOT, DAVE),
              message_id="<a2@ext.example>", references=ROOT)

        epochs = _rows(db_path, "SELECT person FROM room_epochs "
                       "WHERE room_token=? AND epoch > 0", (_room_token(config),))
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
                         "conversation_token": _room_token(config)}]

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
        assert drafts[0]["room_token"] == _room_token(config)

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
# The heads-up mail (ISSUE-608): a private note about a thread room
# ---------------------------------------------------------------------------


class TestTheHeadsUpMail:
    @pytest.mark.asyncio
    async def test_a_private_mail_to_the_users_own_address(self, config, db_path):
        from istota.rooms import private_replies

        _start_thread(config)
        with db.get_db(db_path) as conn:
            delivery = private_replies.deliver_private(
                conn, config, user_id=HOST, about_token=_room_token(config),
                kind="confirmation", reference="7:abc", body="You are free after 7.")
        with patch("istota.rooms.private_replies._send_private_mail") as send:
            ok = await private_replies.send_private(
                config, delivery, body="You are free after 7.")

        assert ok is True
        kwargs = send.call_args.kwargs
        assert kwargs["to"] == HOST_ADDR
        assert kwargs["subject"].startswith("re: ")
        assert "in_reply_to" not in kwargs and "references" not in kwargs
        assert "private chat with the bot" in kwargs["body"]

    def test_a_shared_thread_asks_its_confirmations_privately(self, config, db_path):
        from istota.rooms import private_replies

        task_ids = _start_thread(config)
        with db.get_db(db_path) as conn:
            about = private_replies.park_about(conn, db.get_task(conn, task_ids[0]))
        assert about == _room_token(config)


# ---------------------------------------------------------------------------
# D10: the per-room answer
# ---------------------------------------------------------------------------


class TestTheContainerAnswer:
    def test_email_container_override_and_phone_membership(self):
        assert is_room_member_for("email", room_container=True)
        assert is_room_member_for("whatsapp", room_container=True)
        assert not is_room_member_for("email", room_container=False)
        assert is_room_member_for("sms", room_container=False)
        assert is_room_member_for("whatsapp", room_container=False)
        assert not is_room_member_for("ntfy", room_container=True)
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

        from istota import confirmations
        from istota.rooms import private_replies
        from istota.relay import requests

        _start_thread(config)
        with db.get_db(db_path) as conn:
            # From the host's own private chat, linked to the thread room.
            private = db.create_web_chat_room(conn, HOST, "Mine").token
            ident = db.create_task(conn, user_id=HOST, source_type="web",
                                   prompt="post it", conversation_token=private,
                                   about_room_token=_room_token(config))
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
            private_replies.hold_room_post(conn, config, actor_user_id=HOST, task_id=ident,
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

        from istota.rooms import private_replies
        from istota.relay import requests

        task_ids = _start_thread(config)
        with db.get_db(db_path) as conn:
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_ids[0],))
            private_replies.enqueue_whisper(conn, config, actor_user_id=HOST,
                                       task_id=task_ids[0], request_key="w1",
                                       text="Only for you.")
        with patch("istota.rooms.private_replies._send_private_mail") as send:
            asyncio.run(requests.drain_requests(config))

        kwargs = send.call_args.kwargs
        assert kwargs["to"] == HOST_ADDR
        assert kwargs["subject"] == "re: Dinner plans"
        assert kwargs["body"].startswith("Only for you.\n\n")
        assert "private chat with the bot" in kwargs["body"]


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
            assert not threads.is_present(conn, _room_token(config), outsider)

    def test_a_held_body_is_not_in_the_rooms_transcript(self, config, db_path):
        _start_thread(config)
        _poll(config, sender="mallory@elsewhere.example", to=(BOT,),
              message_id="<m1@elsewhere.example>", references=ROOT, body="EVIL-BODY")

        rows = _rows(db_path, "SELECT body FROM messages WHERE room_token=?",
                     (_room_token(config),))
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
                     "AND role='user' ORDER BY id", (_room_token(config),))
        assert rows[-1]["author_label"] == BOB


    def test_a_reply_to_the_bots_own_mail_alone_lands_in_the_room(self, config, db_path):
        _start_thread(config)
        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn, user_id=HOST, message_id="<bot-out@test.com>", to_addr=ALICE,
                subject="Re: Dinner plans", conversation_token=_room_token(config),
            )
        _poll(config, sender=ALICE, to=(HOST_ADDR, BOB), cc=(BOT,),
              message_id="<a5@ext.example>", references="<bot-out@test.com>")

        assert len(_rows(db_path, "SELECT token FROM rooms WHERE origin='email'")) == 1
        rows = _rows(db_path, "SELECT author_label FROM messages WHERE room_token=? "
                     "AND role='user' ORDER BY id", (_room_token(config),))
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
    def test_the_hosts_question_goes_to_their_private_room_not_the_thread(
        self, mock_run_coro, mock_post_email, config, db_path,
    ):
        from istota.scheduler import process_one_task

        task_ids = _start_thread(config)
        with db.get_db(db_path) as conn:
            private = db.create_web_chat_room(conn, HOST, "Mine").token
            rooms_before = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "Shall I tell Alice Thursday works? Please confirm.",
                          None, None),
        ):
            process_one_task(config)

        with db.get_db(db_path) as conn:
            assert db.get_task(conn, task_ids[0]).status == "pending_confirmation"
            rows = conn.execute(
                "SELECT about_room_token, delivery_reference FROM messages "
                "WHERE room_token = ?", (private,)).fetchall()
            rooms_after = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
        (row,) = rows
        assert row["about_room_token"] == _room_token(config)
        assert row["delivery_reference"].startswith(f"private-confirmation:{task_ids[0]}:")
        assert rooms_after == rooms_before
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


# ---------------------------------------------------------------------------
# ISSUE-612: a mail the bot sent into the thread is the bot's turn, with a card
# ---------------------------------------------------------------------------


def _mail_rows(db_path, token):
    return _rows(db_path, "SELECT id, role, task_id, body, outgoing_mail FROM messages "
                 "WHERE room_token=? AND outgoing_mail IS NOT NULL ORDER BY id", (token,))


def _approve_member_post(config, db_path, text):
    """The host's own `room post`, from their private chat into the thread."""
    from istota import confirmations
    from istota.relay import requests
    from istota.rooms import private_replies

    with db.get_db(db_path) as conn:
        private = db.create_web_chat_room(conn, HOST, "Mine").token
        ident = db.create_task(conn, user_id=HOST, source_type="web",
                               prompt="post it", conversation_token=private,
                               about_room_token=_room_token(config))
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
        private_replies.hold_room_post(conn, config, actor_user_id=HOST, task_id=ident,
                                       request_key="p1", text=text)
        requests.park_question(conn, config, task=db.get_task(conn, ident))
        confirmations.approve(conn, db.get_task(conn, ident), config=config, by="web")
    return ident


def _drain(config):
    import asyncio

    from istota.relay import requests
    asyncio.run(requests.drain_requests(config))
    asyncio.run(requests.drain_requests(config))


class TestTheBotsMailIsACard:
    def test_an_approved_post_is_the_bots_turn_carrying_the_mail(self, config, db_path):
        _start_thread(config)
        _approve_member_post(config, db_path, "Thursday after 7 works")
        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<post@test.com>"):
            _drain(config)

        (row,) = _mail_rows(db_path, _room_token(config))
        assert row["role"] == "assistant"
        assert row["body"] == "Thursday after 7 works"
        # The member's own task ran in their private room: tying the shared
        # room's row to it would put that task's trace in the shared room.
        assert row["task_id"] is None
        mail = json.loads(row["outgoing_mail"])
        assert mail == {"to": [HOST_ADDR], "cc": [ALICE, BOB],
                        "subject": "Re: Dinner plans", "state": "sent"}

    def test_a_post_whose_send_fails_says_so(self, config, db_path):
        _start_thread(config)
        _approve_member_post(config, db_path, "Thursday after 7 works")
        with patch("istota.transport.email.outbound.reply_to_email",
                   side_effect=OSError("smtp down")):
            _drain(config)

        (row,) = _mail_rows(db_path, _room_token(config))
        assert json.loads(row["outgoing_mail"])["state"] == "failed"

    def test_a_post_that_fails_before_its_recipients_are_known_says_not_sent(
        self, config, db_path,
    ):
        _start_thread(config)
        _approve_member_post(config, db_path, "Thursday after 7 works")
        with patch("istota.transport.email.outbound.email_threads.reply_all",
                   side_effect=RuntimeError("boom")):
            _drain(config)

        (row,) = _mail_rows(db_path, _room_token(config))
        assert json.loads(row["outgoing_mail"]) == {"to": [], "cc": [], "state": "failed"}

    def test_the_footer_does_not_put_the_body_on_the_card(self, config, db_path):
        config.email.thread_disclosure_footer = True
        _start_thread(config)
        _approve_member_post(config, db_path, "Thursday after 7 works")
        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<post@test.com>") as reply:
            _drain(config)

        assert "\n\n--\n" in reply.call_args.kwargs["body"]
        (row,) = _mail_rows(db_path, _room_token(config))
        assert "body" not in json.loads(row["outgoing_mail"])

    @pytest.mark.parametrize(("row", "mailed", "differs"), [
        ("OK", "OK", False),
        ("OK", "OK\n\n--\nWritten by Zorg.", False),
        # A row that is only the start of the mail hides nothing.
        ("OK", "OK, but Bob cannot come.", True),
        ("I told them.", "Thursday works.", True),
    ])
    def test_what_counts_as_a_different_mail(self, row, mailed, differs):
        assert db.mailed_body_differs(row, mailed) is differs

    def test_an_approved_guest_proposal_answers_the_guests_turn(self, config, db_path):
        from istota import confirmations
        from istota.rooms import private_replies

        _start_thread(config)
        task_ids = _poll(config, sender=ALICE, to=(BOT,), cc=(HOST_ADDR, BOB),
                         message_id="<a2@ext.example>", references=ROOT,
                         body="Zorg, is Carol free Thursday?")
        with db.get_db(db_path) as conn:
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_ids[0],))
            task = db.get_task(conn, task_ids[0])
            assert private_replies.propose_guest_reply(conn, config, task, "She is.")
            confirmations.approve(conn, db.get_task(conn, task.id), config=config, by="web")
        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<post@test.com>"):
            _drain(config)

        (row,) = _mail_rows(db_path, _room_token(config))
        # The guest's turn and its answer share a task, so the exchange pairs
        # into the room's later history like any answered turn.
        assert row["role"] == "assistant" and row["task_id"] == task.id
        assert json.loads(row["outgoing_mail"])["state"] == "sent"
        with db.get_db(db_path) as conn:
            history = db.get_conversation_history(conn, _room_token(config))
        assert any(turn.result == row["body"] for turn in history)

    @pytest.mark.asyncio
    async def test_an_ordinary_answer_is_stamped_with_the_mail_it_sent(self, config, db_path):
        task_ids = _start_thread(config)
        token = _room_token(config)
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            # What the scheduler stores before delivery: the answer to the host.
            db.store_turn_message(conn, token, role="assistant", task_id=task.id,
                                  body="I told them Thursday at 7.", origin_surface="email")

        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<out@test.com>"):
            await deliver_email_result(config, task, _structured("Thursday at 7 works."))

        (row,) = _mail_rows(db_path, token)
        assert row["task_id"] == task.id
        mail = json.loads(row["outgoing_mail"])
        # The mailed text differs from the answer shown, so the card carries it.
        assert mail == {"to": [HOST_ADDR], "cc": [ALICE, BOB], "subject": "Re: Dinner plans",
                        "state": "sent", "body": "Thursday at 7 works."}

    @pytest.mark.asyncio
    async def test_a_held_reply_reads_held_then_sent_once_released(self, config, db_path):
        from istota.mail import drafts

        config.users[HOST].trusted_email_senders = []
        task_ids = _start_thread(config)
        token = _room_token(config)
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            db.store_turn_message(conn, token, role="assistant", task_id=task.id,
                                  body="Thursday at 7 works.", origin_surface="email")

        with patch("istota.transport.email.outbound.reply_to_email") as reply:
            await deliver_email_result(config, task, _structured())
        reply.assert_not_called()
        (row,) = _mail_rows(db_path, token)
        mail = json.loads(row["outgoing_mail"])
        assert mail["state"] == "held" and isinstance(mail["draft_id"], int)
        assert "body" not in mail

        with patch("istota.skills.email.send_email", return_value="<rel@test.com>"):
            drafts.release(config, mail["draft_id"], by="web")
        assert json.loads(_mail_rows(db_path, token)[0]["outgoing_mail"])["state"] == "sent"

    @pytest.mark.asyncio
    async def test_a_discarded_draft_reads_discarded(self, config, db_path):
        from istota.mail import drafts

        config.users[HOST].trusted_email_senders = []
        task_ids = _start_thread(config)
        token = _room_token(config)
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            db.store_turn_message(conn, token, role="assistant", task_id=task.id,
                                  body="Thursday at 7 works.", origin_surface="email")
        with patch("istota.transport.email.outbound.reply_to_email"):
            await deliver_email_result(config, task, _structured())
        draft_id = json.loads(_mail_rows(db_path, token)[0]["outgoing_mail"])["draft_id"]

        with db.get_db(db_path) as conn:
            drafts.discard(conn, draft_id, by="web")
        assert json.loads(_mail_rows(db_path, token)[0]["outgoing_mail"])["state"] == "discarded"

    @pytest.mark.asyncio
    async def test_an_answer_the_room_does_not_hold_gets_no_row(self, config, db_path):
        """Only a row the room already holds is stamped; none is created."""
        task_ids = _start_thread(config)
        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<out@test.com>"):
            await deliver_email_result(config, task, _structured())
        assert _mail_rows(db_path, _room_token(config)) == []

    def test_the_web_transcript_carries_the_card(self, config, db_path):
        pytest.importorskip("fastapi")
        from istota.webui import app as web_app

        _start_thread(config)
        _approve_member_post(config, db_path, "Thursday after 7 works")
        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<post@test.com>"):
            _drain(config)
        prev = web_app._config
        web_app._config = config
        try:
            page = web_app._chat_room_messages(HOST, _room_token(config), 20)
        finally:
            web_app._config = prev

        (bubble,) = [m for m in page["messages"] if m.get("mail")]
        assert bubble["role"] == "assistant"
        assert bubble["text"] == "Thursday after 7 works"
        assert bubble["mail"] == {"to": [HOST_ADDR], "cc": [ALICE, BOB],
                                  "subject": "Re: Dinner plans", "state": "sent"}
