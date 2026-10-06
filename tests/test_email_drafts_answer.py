"""ISSUE-662: answering a held outbound draft by email.

The draft notice goes out on the alert route, which can be email, and named
`!drafts send <id>` / `!drafts discard <id>`. Mailed back, that answer became
an ordinary task and the draft stayed pending, so a user whose alerts reach
only email could never release or discard it. It is now read before routing,
under ISSUE-649's rule: acted on only with a DMARC pass under our own
``authserv_id`` aligned with the user's address, whatever
``confirm_sender_match`` says, and dropped and recorded otherwise.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from istota import db
from istota.mail import drafts
from istota.skills.email import Email, EmailEnvelope
from istota.notifications.resolvers import outbound_draft as draft_source
from istota.transport.email import answers as email_answers
from istota.transport.email import inbound as _inbound

from . import test_email_confirmation_answer as _answers
from . import test_email_thread_rooms as _base
from .test_email_thread_rooms import HOST, HOST_ADDR

config = _base.config
db_path = _base.db_path
authenticated = _answers.authenticated

AUTHSERV = _answers.AUTHSERV
PASS = _answers.PASS
FAIL = (f"{AUTHSERV}; dmarc=fail header.from=test.com",)
_poll = _answers._poll
_ledger = _answers._ledger
_task_count = _answers._task_count


@pytest.fixture(autouse=True)
def _fresh_volume_state():
    _inbound._reset_volume_state()
    yield
    _inbound._reset_volume_state()


def _held_draft(db_path, user_id=HOST, to="dave@example.org"):
    with db.get_db(db_path) as conn:
        return drafts.hold(
            conn, user_id=user_id, task_id=None, room_token=None,
            to_addrs=[to], cc_addrs=[], bcc_addrs=[], subject="Re: lunch",
            body="Thursday works.", html=False, in_reply_to=None,
            references=None, attachments=[], origin_target=None,
            hold_reason="new recipient",
        )


def _draft_status(db_path, draft_id):
    with db.get_db(db_path) as conn:
        return drafts.get(conn, draft_id).status


class TestAnAuthenticatedDraftsAnswer:
    def test_send_releases_the_draft_after_the_transaction(self, authenticated, db_path):
        draft_id = _held_draft(db_path)
        before = _task_count(db_path)
        created, queued, _, acked = _poll(
            authenticated, sender=HOST_ADDR, auth=PASS,
            body=f"!drafts send {draft_id}\n\n-- \nCarol",
        )
        assert created == [] and queued == []
        assert _task_count(db_path) == before
        # Nothing is sent inside the poll's transaction: `release` opens its
        # own connection, so it runs with the ack, after the commit.
        assert _draft_status(db_path, draft_id) == drafts.STATUS_PENDING
        assert _ledger(db_path) == {
            "routing_method": "drafts_answer", "task_id": None, "user_id": HOST,
        }
        (ack,) = acked
        assert ack.user_id == HOST and ack.release_draft_id == draft_id

        with (
            patch("istota.mail.drafts.release", return_value="<sent@test>") as release,
            patch("istota.notifications.delivery.send_notification") as send,
        ):
            email_answers.deliver_acks(authenticated, acked)
        release.assert_called_once_with(authenticated, draft_id, by="email")
        text = send.call_args.args[2]
        assert f"Sent #{draft_id}" in text and "dave@example.org" in text

    def test_a_failed_send_says_the_draft_is_still_waiting(self, authenticated, db_path):
        draft_id = _held_draft(db_path)
        _, _, _, acked = _poll(
            authenticated, sender=HOST_ADDR, auth=PASS, body=f"!drafts send #{draft_id}",
        )
        with (
            patch("istota.mail.drafts.release", side_effect=OSError("smtp down")),
            patch("istota.notifications.delivery.send_notification") as send,
        ):
            email_answers.deliver_acks(authenticated, acked)
        assert "still waiting" in send.call_args.args[2]

    def test_discard_bins_the_draft(self, authenticated, db_path):
        draft_id = _held_draft(db_path)
        created, _, _, acked = _poll(
            authenticated, sender=HOST_ADDR, auth=PASS, body=f"!drafts discard {draft_id}",
        )
        assert created == []
        assert _draft_status(db_path, draft_id) == drafts.STATUS_DISCARDED
        (ack,) = acked
        assert ack.release_draft_id is None and f"Discarded #{draft_id}" in ack.text

    def test_a_bare_drafts_mails_back_the_listing(self, authenticated, db_path):
        draft_id = _held_draft(db_path)
        created, _, _, acked = _poll(
            authenticated, sender=HOST_ADDR, auth=PASS, body="!drafts",
        )
        assert created == []
        assert f"#{draft_id}" in acked[0].text
        assert _draft_status(db_path, draft_id) == drafts.STATUS_PENDING

    def test_a_verb_without_an_id_acts_on_nothing(self, authenticated, db_path):
        """By mail the id is required, even with one draft open: an answer can
        arrive long after the notice, and a release cannot be taken back."""
        draft_id = _held_draft(db_path)
        created, _, _, acked = _poll(
            authenticated, sender=HOST_ADDR, auth=PASS, body="!drafts send",
        )
        assert created == []
        assert acked[0].release_draft_id is None
        assert f"!drafts send {draft_id}" in acked[0].text
        assert _draft_status(db_path, draft_id) == drafts.STATUS_PENDING

    def test_another_users_draft_is_not_waiting(self, authenticated, db_path):
        _held_draft(db_path)
        draft_id = _held_draft(db_path, user_id="dan")
        _, _, _, acked = _poll(
            authenticated, sender=HOST_ADDR, auth=PASS, body=f"!drafts discard {draft_id}",
        )
        assert "isn't waiting" in acked[0].text
        assert _draft_status(db_path, draft_id) == drafts.STATUS_PENDING


class TestTheReleaseThroughThePoll:
    def test_a_mailed_send_goes_out_once_after_the_poll_commits(
            self, authenticated, db_path):
        """The whole seam: `poll_emails` reads the answer, commits, and its
        `finally` releases through the real `drafts.release`, whose claim
        needs the write lock the poll's message transaction held."""
        draft_id = _held_draft(db_path)
        uid = "662001"
        body = f"!drafts send {draft_id}"
        envelope = EmailEnvelope(
            id=uid, subject="Hi", sender=HOST_ADDR,
            date="Mon, 01 Jan 2026 12:00:00 +0000", is_read=False,
        )
        email = Email(
            id=uid, subject="Hi", sender=HOST_ADDR,
            date="Mon, 01 Jan 2026 12:00:00 +0000", body=body, attachments=[],
            message_id=f"<m{uid}@test>", references=None, in_reply_to=None,
            to=(_base.BOT,), cc=(), authentication_results=PASS[0],
            authentication_results_all=PASS,
        )
        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.transport.email.inbound.deliver_pending"),
            patch("istota.skills.email.send_email", return_value="<out@test.invalid>") as smtp,
            patch("istota.notifications.delivery.send_notification") as acked,
        ):
            created = _inbound.poll_emails(authenticated)

        assert created == []
        smtp.assert_called_once()
        with db.get_db(db_path) as conn:
            draft = drafts.get(conn, draft_id)
            sent = conn.execute(
                "SELECT COUNT(*) FROM sent_emails WHERE message_id = ?",
                ("<out@test.invalid>",)).fetchone()[0]
        assert draft.status == drafts.STATUS_SENT
        assert draft.sent_message_id == "<out@test.invalid>"
        assert sent == 1
        assert f"Sent #{draft_id}" in acked.call_args.args[2]


class TestARefusedDraftsAnswer:
    @pytest.mark.parametrize("policy", ["off", "verify", "gate"])
    def test_a_forged_send_is_dropped_under_every_policy(self, authenticated, db_path, policy):
        authenticated.email.confirm_sender_match = policy
        draft_id = _held_draft(db_path)
        before = _task_count(db_path)
        created, queued, raised, acked = _poll(
            authenticated, sender=HOST_ADDR, auth=FAIL, body=f"!drafts send {draft_id}",
        )
        assert created == [] and queued == [] and acked == []
        assert _task_count(db_path) == before
        assert _draft_status(db_path, draft_id) == drafts.STATUS_PENDING
        assert _ledger(db_path)["routing_method"] == "answer_refused:fail"
        assert len(raised) == 1
        with db.get_db(db_path) as conn:
            row = conn.execute(
                "SELECT body FROM notifications WHERE user_id = ? "
                "AND dedup_key LIKE 'email-draft-answer-refused:%'", (HOST,)).fetchone()
        assert "held mail" in row["body"] and f"{draft_id}" not in row["body"]

    def test_without_an_authserv_id_it_is_dropped(self, config, db_path):
        config.email.authserv_id = ""
        draft_id = _held_draft(db_path)
        created, _, _, acked = _poll(
            config, sender=HOST_ADDR, auth=PASS, body=f"!drafts discard {draft_id}",
        )
        assert created == [] and acked == []
        assert _draft_status(db_path, draft_id) == drafts.STATUS_PENDING
        assert _ledger(db_path)["routing_method"] == "answer_refused:unavailable"


class TestTheNoticeWording:
    def test_it_offers_the_verbs_by_email_when_mail_answers_work(self, authenticated):
        body = draft_source.delivery_body_for("Hi", 7, "a@b.c", config=authenticated)
        assert "`!drafts send 7`" in body and "not accepted by email" not in body

    def test_it_says_where_to_answer_when_mail_answers_do_not(self, config):
        config.email.authserv_id = ""
        body = draft_source.delivery_body_for("Hi", 7, "a@b.c", config=config)
        assert "not accepted by email" in body
