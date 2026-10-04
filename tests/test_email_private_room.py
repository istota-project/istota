"""Email on rooms, stage 3: the private email room and stranger first contact.

Section 7 of the spec. Mail between the user and the bot alone is a turn in
the user's private email room, owned by the email surface the way SMS and
WhatsApp each own one: minted on the first such mail through
`ingest.record_phone_turn`, bound under `email_conversation_token(user)`,
read-only in web, and answered by a reply to the user alone. A stranger's mail
at ``bot+<user>@``, once the untrusted-sender gate admits it, is a thread room
with one person on it, and a held one is minted when the user approves it.

The ``office`` regression is the case the decision "no guest mode on email"
was taken for: a user who trusts every sender answers each stranger's mail at
their own reach, with USER.md loaded, and the reply goes out with no draft, no
proposal and no private item, however many messages the exchange runs to.

Driven through `poll_emails`, `confirmations.approve`, `process_one_task` and
`deliver_email_result`, where the behaviour lives, with the fixtures of
`test_email_thread_rooms.py`.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from istota import db
from istota.config import UserConfig
from istota.transport.email import email_conversation_token
from istota.transport.email import threads
from istota.transport.email.outbound import deliver_email_result

from .test_email_thread_rooms import BOT, HOST, HOST_ADDR, _poll, _rows
from . import test_email_thread_rooms as _base

config = _base.config
db_path = _base.db_path

STRANGER = "erin@elsewhere.example"
PLUS = "bot+carol@test.com"


def _task(db_path, task_id):
    with db.get_db(db_path) as conn:
        return db.get_task(conn, task_id)


def _private_room(db_path, user=HOST):
    with db.get_db(db_path) as conn:
        return db.resolve_room_token(conn, "email", email_conversation_token(user))


def _room_for(db_path, root):
    with db.get_db(db_path) as conn:
        return db.resolve_room_token(conn, "email", root)


def _structured(body="Thursday works."):
    return json.dumps({"subject": "", "body": body, "format": "plain"})


# ---------------------------------------------------------------------------
# The private email room
# ---------------------------------------------------------------------------


class TestThePrivateEmailRoom:
    def test_the_first_mail_mints_it_under_the_users_own_ref(self, config, db_path):
        (task_id,) = _poll(config, sender=HOST_ADDR, to=(BOT,),
                           message_id="<p1@test.com>", body="What is on today?")
        ref = email_conversation_token(HOST)
        assert ref.startswith("email-") and len(ref) == len("email-") + 24

        token = _private_room(db_path)
        assert token is not None
        task = _task(db_path, task_id)
        assert task.conversation_token == token
        assert task.output_target == "email"
        with db.get_db(db_path) as conn:
            assert db.list_room_members(conn, token) == [HOST]
            (row,) = conn.execute(
                "SELECT role, task_id, author_user_id, author_label, origin_surface "
                "FROM messages WHERE room_token = ?", (token,)).fetchall()
            assert dict(row) == {"role": "user", "task_id": task_id,
                                 "author_user_id": HOST, "author_label": None,
                                 "origin_surface": "email"}
            # The pre-room alias, as a phone room keeps one.
            assert conn.execute(
                "SELECT new_token FROM room_token_migration WHERE old_token = ?",
                (ref,)).fetchone()[0] == token

    def test_a_later_mail_lands_in_the_same_room(self, config, db_path):
        _poll(config, sender=HOST_ADDR, to=(BOT,), message_id="<p1@test.com>")
        (second,) = _poll(config, sender=HOST_ADDR, to=(PLUS,),
                          message_id="<p2@test.com>", subject="Another")
        assert _task(db_path, second).conversation_token == _private_room(db_path)
        assert len(_rows(db_path, "SELECT token FROM rooms")) == 1

    def test_it_is_not_a_thread(self, config, db_path):
        from istota.rooms.scopes import is_email_thread_room

        (task_id,) = _poll(config, sender=HOST_ADDR, to=(BOT,), message_id="<p1@test.com>")
        token = _private_room(db_path)
        with db.get_db(db_path) as conn:
            assert not is_email_thread_room(conn, token)
            assert threads.thread_room_for_task(conn, _task(db_path, task_id)) is None

    @pytest.mark.asyncio
    async def test_the_answer_is_a_reply_to_the_user_alone(self, config, db_path):
        (task_id,) = _poll(config, sender=HOST_ADDR, to=(BOT,), message_id="<p1@test.com>")
        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<out@test.com>") as reply:
            assert await deliver_email_result(config, _task(db_path, task_id), _structured())
        kwargs = reply.call_args.kwargs
        assert kwargs["to_addr"] == HOST_ADDR
        assert not kwargs.get("cc")
        assert kwargs["in_reply_to"] == "<p1@test.com>"
        # The reply to the user mints no thread room of its own.
        assert len(_rows(db_path, "SELECT token FROM rooms")) == 1

    def test_the_scheduler_stores_the_answer_in_it(self, config, db_path):
        from istota.scheduler import process_one_task

        _poll(config, sender=HOST_ADDR, to=(BOT,), message_id="<p1@test.com>")
        with (
            patch("istota.scheduler.execute_task",
                  return_value=(True, "Nothing today.", None, None)),
            patch("istota.scheduler.run_coro", return_value=True),
            patch("istota.scheduler.post_result_to_email", return_value=True),
        ):
            process_one_task(config)
        roles = [r["role"] for r in _rows(
            db_path, "SELECT role FROM messages WHERE room_token = ? ORDER BY id",
            (_private_room(db_path),))]
        assert roles == ["user", "assistant"]

    def test_someone_elses_mail_naming_its_ids_does_not_land_in_it(self, config, db_path):
        """A Message-ID is a bearer token anyone on a forward holds: a reply
        naming the user's private mail is not a turn in the private room."""
        config.users[HOST].trusted_email_senders = ["*"]
        _poll(config, sender=HOST_ADDR, to=(BOT,), message_id="<p1@test.com>")
        (task_id,) = _poll(config, sender=STRANGER, to=(PLUS,),
                           message_id="<s1@elsewhere.example>", references="<p1@test.com>")
        assert _task(db_path, task_id).conversation_token != _private_room(db_path)

    def test_the_bots_own_mail_to_the_user_lands_in_it_once_it_exists(self, config, db_path):
        with db.get_db(db_path) as conn:
            assert threads.register_sent_thread(
                conn, config, user_id=HOST, message_id="<b0@test.com>", to=[HOST_ADDR],
                subject="Briefing", body="Morning.") is None
        assert _rows(db_path, "SELECT token FROM rooms") == []

        _poll(config, sender=HOST_ADDR, to=(BOT,), message_id="<p1@test.com>")
        with db.get_db(db_path) as conn:
            threads.register_sent_thread(
                conn, config, user_id=HOST, message_id="<b1@test.com>", to=[HOST_ADDR],
                subject="Briefing", body="Evening.")
        rows = _rows(db_path, "SELECT role, body, outgoing_mail FROM messages "
                     "WHERE room_token = ? ORDER BY id", (_private_room(db_path),))
        assert [(r["role"], r["body"]) for r in rows] == [
            ("user", rows[0]["body"]), ("assistant", "Evening.")]
        assert json.loads(rows[1]["outgoing_mail"])["to"] == [HOST_ADDR]

    def test_a_message_id_shaped_like_the_ref_cannot_take_it(self, config, db_path):
        """The sender chooses the Message-ID: a thread bound under the user's
        private token would take their own mail as a thread."""
        config.users[HOST].trusted_email_senders = ["*"]
        _poll(config, sender=STRANGER, to=(PLUS,),
              message_id=email_conversation_token(HOST))
        assert _private_room(db_path) is None
        _poll(config, sender=HOST_ADDR, to=(BOT,), message_id="<p1@test.com>")
        assert _private_room(db_path) is not None

    def test_the_header_names_email_as_the_target(self, config, db_path):
        from istota.executor import room_identity_line

        (task_id,) = _poll(config, sender=HOST_ADDR, to=(BOT,), message_id="<p1@test.com>")
        with db.get_db(db_path) as conn:
            line = room_identity_line(config, _task(db_path, task_id), conn,
                                      rooms_cli_available=True)
        assert 'target = "email"' in line
        assert "readable but not writable in web chat" in line
        assert "mails it to the user" in line

    def test_a_held_self_claim_is_admitted_on_approval(self, config, db_path):
        from istota import confirmations

        config.email.confirm_sender_match = "gate"
        (task_id,) = _poll(config, sender=HOST_ADDR, to=(BOT,), message_id="<p1@test.com>")
        assert _task(db_path, task_id).status == "pending_confirmation"
        assert _private_room(db_path) is None

        with db.get_db(db_path) as conn:
            confirmations.approve(conn, _task(db_path, task_id), config=config)
        token = _private_room(db_path)
        assert _task(db_path, task_id).conversation_token == token
        assert [r["task_id"] for r in _rows(
            db_path, "SELECT task_id FROM messages WHERE room_token = ?", (token,))] == [task_id]


# ---------------------------------------------------------------------------
# Stranger first contact
# ---------------------------------------------------------------------------


class TestStrangerFirstContact:
    def test_an_admitted_stranger_mints_a_thread_room(self, config, db_path):
        config.users[HOST].trusted_email_senders = ["*"]
        (task_id,) = _poll(config, sender=STRANGER, to=(PLUS,),
                           message_id="<s1@elsewhere.example>", body="Do you have a table?")
        token = _room_for(db_path, "<s1@elsewhere.example>")
        assert token is not None
        task = _task(db_path, task_id)
        assert task.conversation_token == token
        assert task.user_id == HOST
        assert task.guest_participant_id is None
        assert task.host_absent
        with db.get_db(db_path) as conn:
            from istota.rooms.scopes import withheld_for_task
            from istota.skills._types import SkillMeta

            index = {"calendar": SkillMeta(name="calendar", description="x",
                                           shared_room="private")}
            assert db.list_room_members(conn, token) == [HOST]
            assert withheld_for_task(conn, task, skill_index=index) == frozenset()

    def test_a_held_stranger_mints_nothing_until_approved(self, config, db_path):
        from istota import confirmations

        config.users[HOST].trusted_email_senders = []
        (task_id,) = _poll(config, sender=STRANGER, to=(PLUS,),
                           message_id="<s1@elsewhere.example>", body="Do you have a table?")
        assert _task(db_path, task_id).status == "pending_confirmation"
        assert _rows(db_path, "SELECT token FROM rooms") == []

        with db.get_db(db_path) as conn:
            confirmations.approve(conn, _task(db_path, task_id), config=config)

        token = _room_for(db_path, "<s1@elsewhere.example>")
        assert token is not None
        task = _task(db_path, task_id)
        assert task.status == "pending"
        assert task.conversation_token == token
        assert task.output_target == "email"
        assert task.host_absent
        rows = _rows(db_path, "SELECT role, task_id, author_label FROM messages "
                     "WHERE room_token = ?", (token,))
        assert rows == [{"role": "user", "task_id": task_id, "author_label": STRANGER}]
        assert _rows(db_path, "SELECT thread_id FROM processed_emails WHERE task_id = ?",
                     (task_id,)) == [{"thread_id": token}]

    @pytest.mark.asyncio
    async def test_the_approved_turns_answer_is_a_reply_all(self, config, db_path):
        from istota import confirmations

        config.users[HOST].trusted_email_senders = []
        (task_id,) = _poll(config, sender=STRANGER, to=(PLUS,),
                           message_id="<s1@elsewhere.example>")
        with db.get_db(db_path) as conn:
            confirmations.approve(conn, _task(db_path, task_id), config=config,
                                  trust_sender=True)
        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<out@test.com>") as reply:
            assert await deliver_email_result(config, _task(db_path, task_id), _structured())
        assert reply.call_args.kwargs["to_addr"] == STRANGER
        assert _rows(db_path, "SELECT id FROM outbound_drafts") == []

    def test_a_declined_stranger_mints_nothing(self, config, db_path):
        from istota import confirmations

        config.users[HOST].trusted_email_senders = []
        (task_id,) = _poll(config, sender=STRANGER, to=(PLUS,),
                           message_id="<s1@elsewhere.example>")
        with db.get_db(db_path) as conn:
            confirmations.decline(conn, _task(db_path, task_id))
        assert _rows(db_path, "SELECT token FROM rooms") == []


# ---------------------------------------------------------------------------
# The office regression
# ---------------------------------------------------------------------------


def _office(config):
    """The production shape: every sender trusted, the default outbound policy."""
    config.users[HOST] = UserConfig(email_addresses=[HOST_ADDR], trusted_email_senders=["*"])


def _answer(config, result):
    from istota.scheduler import process_one_task

    with (
        patch("istota.scheduler.execute_task", return_value=(True, result, None, None)),
        patch("istota.scheduler.run_coro", return_value=True),
        patch("istota.scheduler.post_result_to_email", return_value=True) as mail,
    ):
        process_one_task(config)
    return mail


def _private_items(db_path):
    """Anything that reached the host privately: a private reply row, a held
    request, a draft, a notification."""
    return {
        "private_rows": _rows(db_path, "SELECT id FROM messages "
                              "WHERE about_room_token IS NOT NULL"),
        "requests": _rows(db_path, "SELECT id FROM whatsapp_skill_requests"),
        "drafts": _rows(db_path, "SELECT id FROM outbound_drafts"),
        "notifications": _rows(db_path, "SELECT id FROM notifications"),
    }


class TestTheOfficeRegression:
    def test_the_answer_is_delivered_with_nothing_private(self, config, db_path):
        _office(config)
        (task_id,) = _poll(config, sender=STRANGER, to=(PLUS,),
                           message_id="<s1@elsewhere.example>", body="Are you open Sunday?")
        task = _task(db_path, task_id)
        assert task.user_id == HOST and task.guest_participant_id is None

        mail = _answer(config, _structured("Yes, from ten."))
        assert mail.called
        assert _task(db_path, task_id).status == "completed"
        assert _private_items(db_path) == {
            "private_rows": [], "requests": [], "drafts": [], "notifications": []}

    @pytest.mark.asyncio
    async def test_the_reply_goes_out_with_no_draft(self, config, db_path):
        _office(config)
        (task_id,) = _poll(config, sender=STRANGER, to=(PLUS,),
                           message_id="<s1@elsewhere.example>", body="Are you open Sunday?")
        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<out1@test.com>") as reply:
            assert await deliver_email_result(
                config, _task(db_path, task_id), _structured("Yes, from ten."))
        assert reply.call_args.kwargs["to_addr"] == STRANGER
        assert _private_items(db_path) == {
            "private_rows": [], "requests": [], "drafts": [], "notifications": []}

    def test_every_message_of_a_long_exchange_gets_a_task(self, config, db_path):
        """No loop cap on email (D9 exempted): today's path answered every
        message, and the inbound volume budget is what bounds a mail loop."""
        _office(config)
        root = "<s1@elsewhere.example>"
        refs = root
        for n in range(1, 7):
            message_id = root if n == 1 else f"<s{n}@elsewhere.example>"
            ids = _poll(config, sender=STRANGER, to=(PLUS,), message_id=message_id,
                        references=None if n == 1 else refs, body=f"Question {n}?")
            assert len(ids) == 1, f"message {n} was only recorded"
            if n > 1:
                refs = f"{refs} {message_id}"
            _answer(config, _structured(f"Answer {n}."))
        token = _room_for(db_path, root)
        roles = [r["role"] for r in _rows(
            db_path, "SELECT role FROM messages WHERE room_token = ? ORDER BY id", (token,))]
        assert roles == ["user", "assistant"] * 6

    def test_the_cap_still_holds_a_talk_or_web_guest(self):
        """Control: the exemption is email's alone."""
        from istota.transport import ingest, participants

        class _Policy:
            max_bot_turns_without_human = 3
            guest_reply = "direct"
            host_user_id = HOST

        with (
            patch.object(ingest.room_policy, "ensure_policy", return_value=_Policy()),
            patch.object(ingest.room_policy, "current_host", return_value=HOST),
            patch.object(ingest.room_policy, "bot_turns_since_principal", return_value=3),
        ):
            capped = ingest._ask_policy(None, "rm_x", author_kind=participants.GUEST,
                                        multi_human=True, is_command=False)
            email = ingest._ask_policy(None, "rm_x", author_kind=participants.GUEST,
                                       multi_human=True, is_command=False,
                                       email_thread=True)
        assert capped.loop_capped
        assert not email.loop_capped
