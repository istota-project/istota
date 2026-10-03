"""Email thread rooms as the host's correspondence (ISSUE-605, 606, 607).

- No room announcement on an email thread; an optional disclosure footer on
  every mail into one, off by default (605).
- Nobody but the host is a member of an email thread room: the web refuses
  the add, a migration removes earlier ones, and another istota user's mail
  runs as a guest turn (606).
- Being named in the new text counts as being addressed while in Cc, and the
  host's own authenticated, addressed question is answered reply-all with no
  outbound hold (607).

Driven through `poll_emails` and `deliver_email_result`, the two places the
behaviour lives.
"""

import asyncio
import json
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, EmailConfig, UserConfig
from istota.rooms import side_rooms
from istota.skills.email import Email, EmailEnvelope
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
PASS = "mx.test; spf=pass; dmarc=pass header.from=test.com"
FAIL = "mx.test; spf=fail; dmarc=fail header.from=test.com"


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "istota.db"
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
        imap_password="pass", smtp_host="smtp.test", smtp_port=587, bot_email=BOT,
    )
    config.users = {
        HOST: UserConfig(display_name="Carol", email_addresses=[HOST_ADDR],
                         trusted_email_senders=["*@ext.example"]),
    }
    return config


_UID = [900]


def _poll(config, *, sender, to=(BOT,), cc=(), message_id, references=ROOT,
          body="hello", auth=None):
    _UID[0] += 1
    uid = str(_UID[0])
    envelope = EmailEnvelope(id=uid, subject="Dinner", sender=sender,
                             date="Mon, 01 Jan 2026 12:00:00 +0000", is_read=False)
    email = Email(id=uid, subject="Dinner", sender=sender,
                  date="Mon, 01 Jan 2026 12:00:00 +0000", body=body, attachments=[],
                  message_id=message_id, references=references, to=tuple(to),
                  cc=tuple(cc), authentication_results=auth)
    with (
        patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
        patch("istota.transport.email.inbound.read_email", return_value=email),
        patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        patch("istota.transport.email.inbound._deliver_confirmation_prompts"),
        patch("istota.transport.email.inbound._deliver_dmarc_alerts"),
    ):
        return poll_emails(config)


def _start_thread(config, **kw):
    """The host mails the bot and two friends: a thread with three humans."""
    return _poll(config, sender=HOST_ADDR, to=(BOT, ALICE), cc=(BOB,),
                 message_id=ROOT, references=None, **kw)


def _room(config):
    with db.get_db(config.db_path) as conn:
        return db.resolve_room_token(conn, "email", ROOT)


def _task(config, task_id):
    with db.get_db(config.db_path) as conn:
        return db.get_task(conn, task_id)


def _deliver(config, task_id, body="Thursday works."):
    envelope = json.dumps({"subject": "", "body": body, "format": "plain"})
    with patch("istota.transport.email.outbound.reply_to_email",
               return_value=f"<out-{task_id}@test.com>") as reply:
        assert asyncio.run(deliver_email_result(config, _task(config, task_id), envelope))
    return reply


def _drafts(config):
    with db.get_db(config.db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM outbound_drafts").fetchone()[0]


# ---------------------------------------------------------------------------
# 605: no announcement; the footer
# ---------------------------------------------------------------------------


class TestNoAnnouncementOnEmail:
    def test_no_mail_on_the_thread_carries_an_announcement(self, config):
        (task_id,) = _start_thread(config)
        reply = _deliver(config, task_id)
        reply2 = _deliver(config, task_id)
        assert reply.call_args.kwargs["body"] == "Thursday works."
        assert reply2.call_args.kwargs["body"] == "Thursday works."
        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT announced_at FROM room_policy WHERE room_token = ?",
                (_room(config),),
            ).fetchone()
        assert row is None or row["announced_at"] is None

    def test_a_guest_proposal_carries_nothing_by_default(self, config):
        _start_thread(config)
        (task_id,) = _poll(config, sender=ALICE, to=(BOT,), cc=(HOST_ADDR, BOB),
                           message_id="<a2@ext.example>", body="Zorg, Thursday?")
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
            proposal = side_rooms.propose_guest_reply(
                conn, config, db.get_task(conn, task_id), "Thursday at 7.",
            )
        assert proposal.preview.endswith("Message:\nThursday at 7.")


class TestTheDisclosureFooter:
    FOOTER = ("Written by Zorg, an AI assistant, for Carol. To stop it replying "
              "on this thread, reply with `!zorg off` as the first line.")

    def test_off_by_default(self):
        assert EmailConfig().thread_disclosure_footer is False

    def test_every_mail_carries_it_when_on(self, config):
        config.email.thread_disclosure_footer = True
        (task_id,) = _start_thread(config)
        first = _deliver(config, task_id).call_args.kwargs["body"]
        second = _deliver(config, task_id).call_args.kwargs["body"]
        assert first == f"Thursday works.\n\n--\n{self.FOOTER}"
        assert second == first
        # A newcomer is Cc'd on a later message; the next mail still carries it.
        (task2,) = _poll(config, sender=HOST_ADDR, to=(BOT, ALICE), cc=(BOB, DAVE),
                         message_id="<c2@test.com>", body="Zorg, and Dave?")
        third = _deliver(config, task2, "Dave too.").call_args.kwargs["body"]
        assert third == f"Dave too.\n\n--\n{self.FOOTER}"

    def test_a_held_proposal_shows_it_once_and_sends_it_once(self, config):
        config.email.thread_disclosure_footer = True
        _start_thread(config)
        (task_id,) = _poll(config, sender=ALICE, to=(BOT,), cc=(HOST_ADDR, BOB),
                           message_id="<a2@ext.example>", body="Zorg, Thursday?")
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
            proposal = side_rooms.propose_guest_reply(
                conn, config, db.get_task(conn, task_id), "Thursday at 7.",
            )
        assert proposal.preview.count(self.FOOTER) == 1

    def test_the_veto_still_works_by_mail_with_the_footer_off(self, config):
        from istota.rooms import veto as room_veto

        _start_thread(config)
        _poll(config, sender=ALICE, cc=(HOST_ADDR, BOB), message_id="<a3@ext.example>",
              body="!zorg off")
        with db.get_db(config.db_path) as conn:
            assert room_veto.is_vetoed(conn, _room(config))


# ---------------------------------------------------------------------------
# 606: nobody but the host is a member
# ---------------------------------------------------------------------------


class TestNoMembersOnAThread:
    def _with_dan(self, config):
        config.users["dan"] = UserConfig(email_addresses=["dan@test.com"])
        config.users[HOST].trusted_email_senders.append("dan@test.com")

    def test_a_member_added_earlier_is_removed_by_the_migration(self, config):
        self._with_dan(config)
        _start_thread(config)
        token = _room(config)
        with db.get_db(config.db_path) as conn:
            db.add_web_room_member(conn, token, "dan")
            web = db.create_web_chat_room(conn, HOST, "plans")
            db.add_web_room_member(conn, web.token, "dan")
            conn.execute("DELETE FROM _migration_state "
                         "WHERE name = 'email_thread_members_v1'")
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            assert db.list_room_members(conn, token) == [HOST]
            assert sorted(db.list_room_members(conn, web.token)) == ["carol", "dan"]
            assert conn.execute(
                "SELECT COUNT(*) FROM room_participants WHERE room_token = ? "
                "AND user_id = 'dan' AND kind = 'principal' AND left_at IS NULL",
                (token,),
            ).fetchone()[0] == 0

    def test_after_it_their_mail_is_a_guest_turn_under_the_host(self, config):
        self._with_dan(config)
        _start_thread(config)
        token = _room(config)
        with db.get_db(config.db_path) as conn:
            db.add_web_room_member(conn, token, "dan")
            conn.execute("DELETE FROM _migration_state "
                         "WHERE name = 'email_thread_members_v1'")
        db.init_db(config.db_path)
        (task_id,) = _poll(config, sender="dan@test.com", to=(BOT,), cc=(HOST_ADDR,),
                           message_id="<d2@test.com>", body="Zorg, Thursday?")
        task = _task(config, task_id)
        assert task.user_id == HOST
        assert task.guest_participant_id is not None

    def test_a_trusted_senders_mail_needs_no_confirmation(self, config):
        _start_thread(config)
        (task_id,) = _poll(config, sender=ALICE, to=(BOT,), cc=(HOST_ADDR, BOB),
                           message_id="<a2@ext.example>", body="Zorg, Thursday?")
        assert _task(config, task_id).status == "pending"


# ---------------------------------------------------------------------------
# 607: named in Cc counts; the host's own question is not held
# ---------------------------------------------------------------------------


class TestNamedInCc:
    @pytest.mark.parametrize("body", [
        "Zorg, when did we last meet them?",
        "Hi all,\n\nZorg, when did we last meet them?",
        "Could @zorg look this up?",
    ])
    def test_named_in_the_new_text_is_addressed(self, config, body):
        _start_thread(config)
        task_ids = _poll(config, sender=HOST_ADDR, to=(ALICE,), cc=(BOB, BOT),
                         message_id="<c2@test.com>", body=body)
        assert len(task_ids) == 1

    @pytest.mark.parametrize("body", [
        "Ask zorg about it later.",
        "Sounds good.\n\nOn Mon, 1 Jan 2026 at 12:00, Carol <carol@test.com> wrote:\n"
        "> Zorg, when did we last meet them?",
        "Sounds good.\n> Zorg, when did we last meet them?",
        "Sounds good.\n\nOn Mon, 1 Jan 2026 at 12:00, Carol\n<carol@test.com> wrote:\n"
        "Zorg, when did we last meet them?",
        "FYI\n\n---------- Forwarded message ---------\nZorg, look at this",
        "Sounds good.\n\n________________________________\nFrom: Carol <carol@test.com>\n"
        "Sent: Monday\nTo: Alice\nSubject: Dinner\n\nZorg, when did we last meet them?",
        "Sounds good.\n\nFrom: Carol <carol@test.com>\nDate: Monday\n\nZorg, when?",
        "Klingt gut.\n\nAm Mo., 1. Jan. 2026 um 12:00 Uhr schrieb Carol <carol@test.com>:\n\n"
        "Zorg, when did we last meet them?",
        "Ok.\n\nOn Mon, 1 Jan 2026 at 12:00,\nCarol Smith\n<carol@test.com> wrote:\n"
        "Zorg, when?",
    ])
    def test_not_named_in_the_new_text_is_recorded_only(self, config, body):
        _start_thread(config)
        task_ids = _poll(config, sender=HOST_ADDR, to=(ALICE,), cc=(BOB, BOT),
                         message_id="<c2@test.com>", body=body)
        assert task_ids == []

    def test_in_to_is_addressed_unchanged(self, config):
        _start_thread(config)
        task_ids = _poll(config, sender=HOST_ADDR, to=(BOT,), cc=(ALICE, BOB),
                         message_id="<c2@test.com>", body="Sounds good.")
        assert len(task_ids) == 1

    @pytest.mark.parametrize("body, new", [
        ("Hi\n\nOn Tue, X wrote:\n> old", "Hi"),
        ("Hi\n> old\nmore", "Hi\nmore"),
        ("Hi\n-----Original Message-----\nFrom: x", "Hi"),
        ("Hi\nBegin forwarded message:\nZorg", "Hi"),
    ])
    def test_new_text(self, body, new):
        assert threads.new_text(body).strip() == new


class TestTheHostsOwnQuestion:
    @pytest.fixture
    def strict(self, config):
        """Nobody on the thread is trusted, so the gate would hold the answer,
        and the MTA's own stamp is known, so the DMARC verdict is ours."""
        config.users[HOST].trusted_email_senders = []
        config.email.authserv_id = "mx.test"
        return config

    def _seed(self, config):
        # The founding mail is from the host and passes the gate (own address).
        _start_thread(config, auth=PASS)

    def test_an_authenticated_addressed_question_is_answered_unheld(self, strict):
        self._seed(strict)
        (task_id,) = _poll(strict, sender=HOST_ADDR, to=(ALICE,), cc=(BOB, BOT),
                           message_id="<c2@test.com>", body="Zorg, Thursday?",
                           auth=PASS)
        reply = _deliver(strict, task_id)
        assert reply.call_count == 1
        assert reply.call_args.kwargs["to_addr"] == HOST_ADDR
        assert reply.call_args.kwargs["cc"] == [ALICE, BOB]
        assert _drafts(strict) == 0

    def test_an_unauthenticated_one_is_held(self, strict):
        self._seed(strict)
        (task_id,) = _poll(strict, sender=HOST_ADDR, to=(ALICE,), cc=(BOB, BOT),
                           message_id="<c2@test.com>", body="Zorg, Thursday?",
                           auth=FAIL)
        reply = _deliver(strict, task_id)
        reply.assert_not_called()
        assert _drafts(strict) == 1

    def test_without_authserv_id_a_pass_is_not_trusted_to_release(self, strict):
        # Blank authserv_id reads the topmost header, which the sender can
        # write: a forged pass would otherwise pick the recipients.
        strict.email.authserv_id = ""
        self._seed(strict)
        (task_id,) = _poll(strict, sender=HOST_ADDR, to=(ALICE,),
                           cc=(BOB, BOT, "eve@evil.example"),
                           message_id="<c2@test.com>", body="Zorg, my calendar?",
                           auth=PASS)
        reply = _deliver(strict, task_id)
        reply.assert_not_called()
        assert _drafts(strict) == 1

    def test_a_statement_naming_the_bot_runs_but_is_held(self, strict):
        self._seed(strict)
        (task_id,) = _poll(strict, sender=HOST_ADDR, to=(ALICE,), cc=(BOB, BOT),
                           message_id="<c2@test.com>",
                           body="Hi all,\n\nZorg booked the table for 7.", auth=PASS)
        reply = _deliver(strict, task_id)
        reply.assert_not_called()
        assert _drafts(strict) == 1

    def test_a_thread_that_gained_a_recipient_since_is_held(self, strict):
        self._seed(strict)
        (task_id,) = _poll(strict, sender=HOST_ADDR, to=(ALICE,), cc=(BOB, BOT),
                           message_id="<c2@test.com>", body="Zorg, Thursday?",
                           auth=PASS)
        # Recorded only (bot in Cc, not named), but it moves the reply-all.
        strict.users[HOST].trusted_email_senders = ["*@ext.example"]
        assert _poll(strict, sender=ALICE, to=(HOST_ADDR,), cc=(BOB, BOT, "eve@other.example"),
                     message_id="<a3@ext.example>", body="Adding Eve.") == []
        strict.users[HOST].trusted_email_senders = []
        reply = _deliver(strict, task_id)
        reply.assert_not_called()
        assert _drafts(strict) == 1

    def test_a_guests_question_is_still_held_for_the_host(self, strict):
        strict.users[HOST].trusted_email_senders = ["*@ext.example"]
        self._seed(strict)
        (task_id,) = _poll(strict, sender=ALICE, to=(BOT,), cc=(HOST_ADDR, BOB),
                           message_id="<a2@ext.example>", body="Zorg, Thursday?",
                           auth=PASS)
        strict.users[HOST].trusted_email_senders = []
        assert _task(strict, task_id).guest_participant_id is not None
        reply = _deliver(strict, task_id)
        reply.assert_not_called()
        assert _drafts(strict) == 1

    def test_another_task_posting_into_the_thread_is_held(self, strict):
        self._seed(strict)
        token = _room(strict)
        with db.get_db(strict.db_path) as conn:
            task_id = db.create_task(conn, prompt="digest", user_id=HOST,
                                     source_type="email", conversation_token=token)
        reply = _deliver(strict, task_id)
        reply.assert_not_called()
        assert _drafts(strict) == 1
