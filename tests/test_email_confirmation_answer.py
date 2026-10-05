"""ISSUE-649: answering a held task by email, and only an authenticated answer.

A user whose alerts reach only email gets the gate's request by mail and had
no way to answer it there: a mailed ``!confirm N`` became an ordinary task and
the held mail expired. An answer by mail is now accepted only when it carries
a DMARC pass under our own ``authserv_id``, aligned with the user's address,
whatever ``confirm_sender_match`` says. A forged answer approves nothing,
creates no task and raises no second request.

Driven through `poll_emails`, where the behaviour lives, with the fixtures of
`test_email_thread_rooms.py`.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from istota import confirmations, db
from istota.skills.email import Email, EmailEnvelope
from istota.transport.email import inbound as _inbound
from istota.transport.email.inbound import poll_emails

from . import test_email_thread_rooms as _base
from .test_email_thread_rooms import BOT, HOST, HOST_ADDR

config = _base.config
db_path = _base.db_path

AUTHSERV = "mx.test.com"
PASS = (f"{AUTHSERV}; dmarc=pass header.from=test.com",)
STRANGER = "erin@elsewhere.example"
PLUS = "bot+carol@test.com"
REQUEST_ID = "<confirm-request@test.com>"

_UID = [5000]


def _poll(config, *, sender, to=(BOT,), subject="Hello", body="hello", auth=(),
          in_reply_to=None):
    """Poll one message. Returns (created task ids, prompts queued, notices, acks)."""
    _UID[0] += 1
    uid = str(_UID[0])
    envelope = EmailEnvelope(
        id=uid, subject=subject, sender=sender,
        date="Mon, 01 Jan 2026 12:00:00 +0000", is_read=False,
    )
    email = Email(
        id=uid, subject=subject, sender=sender,
        date="Mon, 01 Jan 2026 12:00:00 +0000",
        body=body, attachments=[], message_id=f"<m{uid}@test>",
        references=None, in_reply_to=in_reply_to, to=tuple(to), cc=(),
        authentication_results=auth[0] if auth else None,
        authentication_results_all=tuple(auth),
    )
    with (
        patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
        patch("istota.transport.email.inbound.read_email", return_value=email),
        patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        patch("istota.transport.email.inbound._deliver_confirmation_prompts") as prompts,
        patch("istota.transport.email.inbound._deliver_dmarc_alerts"),
        patch("istota.transport.email.inbound.deliver_pending") as notices,
        patch("istota.transport.email.inbound._deliver_answer_acks") as acks,
    ):
        created = poll_emails(config)
    queued = [p for call in prompts.call_args_list for p in call.args[1]]
    raised = [n for call in notices.call_args_list for n in call.args[1] if n is not None]
    acked = [a for call in acks.call_args_list for a in call.args[1]]
    return created, queued, raised, acked


def _hold(config):
    """A stranger's mail at the plus address, held for the user."""
    (task_id,), queued, _, _ = _poll(
        config, sender=STRANGER, to=(PLUS,), subject="Invoice", body="pay me",
    )
    assert len(queued) == 1
    return task_id


def _status(db_path, task_id):
    with db.get_db(db_path) as conn:
        return db.get_task(conn, task_id).status


def _ledger(db_path):
    with db.get_db(db_path) as conn:
        row = conn.execute(
            "SELECT routing_method, task_id, user_id FROM processed_emails "
            "ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row)


def _task_count(db_path):
    with db.get_db(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


@pytest.fixture(autouse=True)
def _fresh_volume_state():
    """The prompt budget is in-process and keyed by sender."""
    _inbound._reset_volume_state()
    yield
    _inbound._reset_volume_state()


@pytest.fixture
def authenticated(config):
    config.email.authserv_id = AUTHSERV
    return config


class TestAnAuthenticatedAnswer:
    def test_a_confirm_line_approves_the_held_mail(self, authenticated, db_path):
        task_id = _hold(authenticated)
        before = _task_count(db_path)
        created, queued, _, acked = _poll(
            authenticated, sender=HOST_ADDR, body=f"!confirm {task_id}\n\n-- \nCarol",
            auth=PASS,
        )
        assert created == [] and queued == []
        assert _task_count(db_path) == before
        assert _status(db_path, task_id) == "pending"
        assert _ledger(db_path) == {
            "routing_method": "confirm_answer", "task_id": task_id, "user_id": HOST,
        }
        ((user, ack),) = [(a.user_id, a.text) for a in acked]
        assert user == HOST and f"#{task_id}" in ack

    def test_a_reply_to_the_request_declines_it(self, authenticated, db_path):
        task_id = _hold(authenticated)
        confirmations.record_request_message_id(authenticated, HOST, task_id, REQUEST_ID)
        _poll(authenticated, sender=HOST_ADDR, subject=f"Re: Confirm task #{task_id}",
              body="no\n\nOn Monday, bot wrote:\n> Task: #1", auth=PASS,
              in_reply_to=REQUEST_ID)
        assert _status(db_path, task_id) == "cancelled"

    def test_a_reply_to_a_lookalike_of_the_request_is_not_an_answer(
            self, authenticated, db_path):
        """A phishing mail titled like the request, answered by the user, must
        not approve: the reply has to name the request's own Message-ID."""
        task_id = _hold(authenticated)
        confirmations.record_request_message_id(authenticated, HOST, task_id, REQUEST_ID)
        created, _, _, acked = _poll(
            authenticated, sender=HOST_ADDR, subject=f"Re: Confirm task #{task_id}",
            body="yes", auth=PASS, in_reply_to="<lookalike@attacker.example>",
        )
        assert _status(db_path, task_id) == "pending_confirmation"
        assert acked == [] and len(created) == 1

    @pytest.mark.parametrize("line", ["!yes {id}", "!approve {id}", "!no {id}"])
    def test_the_aliases_answer_too(self, authenticated, db_path, line):
        task_id = _hold(authenticated)
        _poll(authenticated, sender=HOST_ADDR, body=line.format(id=task_id), auth=PASS)
        expected = "cancelled" if line.startswith("!no") else "pending"
        assert _status(db_path, task_id) == expected

    def test_a_quoted_confirm_below_new_text_is_not_an_answer(self, authenticated, db_path):
        task_id = _hold(authenticated)
        created, _, _, acked = _poll(
            authenticated, sender=HOST_ADDR, auth=PASS,
            body=f"Thanks, will look later.\n\n> !confirm {task_id}",
        )
        assert _status(db_path, task_id) == "pending_confirmation"
        assert acked == [] and len(created) == 1

    def test_yes_trust_approves_and_trusts_the_sender(self, authenticated, db_path):
        task_id = _hold(authenticated)
        _poll(authenticated, sender=HOST_ADDR, body=f"!confirm {task_id} yes trust",
              auth=PASS)
        assert _status(db_path, task_id) == "pending"
        with db.get_db(db_path) as conn:
            assert STRANGER in [s["sender_email"].lower() for s in db.list_trusted_senders(conn, HOST)]

    def test_an_answer_naming_no_held_task_changes_nothing(self, authenticated, db_path):
        task_id = _hold(authenticated)
        before = _task_count(db_path)
        created, queued, _, acked = _poll(
            authenticated, sender=HOST_ADDR, body=f"!confirm {task_id + 50}", auth=PASS,
        )
        assert created == [] and queued == []
        assert _task_count(db_path) == before
        assert _status(db_path, task_id) == "pending_confirmation"
        assert "isn't waiting" in acked[0].text


class TestARefusedAnswer:
    @pytest.mark.parametrize("policy", ["off", "verify", "gate"])
    def test_a_forged_answer_is_dropped_under_every_policy(self, authenticated, db_path, policy):
        authenticated.email.confirm_sender_match = policy
        task_id = _hold(authenticated)
        before = _task_count(db_path)
        body = f"!confirm {task_id} yes"
        created, queued, raised, acked = _poll(
            authenticated, sender=HOST_ADDR, body=body,
            auth=(f"{AUTHSERV}; dmarc=fail header.from=test.com",),
        )
        assert created == [] and queued == [] and acked == []
        assert _task_count(db_path) == before
        assert _status(db_path, task_id) == "pending_confirmation"
        assert _ledger(db_path)["routing_method"] == "answer_refused:fail"
        (notice,) = raised
        with db.get_db(db_path) as conn:
            row = conn.execute(
                "SELECT title, body FROM notifications WHERE user_id = ? "
                "AND dedup_key LIKE 'email-answer-refused:%'", (HOST,)).fetchone()
        assert row is not None
        assert body not in row["body"] and f"{task_id}" not in row["body"]

    @pytest.mark.parametrize("auth, reason", [
        # Our stamp carries no DMARC verdict; a lower header naming our id,
        # which the sender could have written, says pass.
        ((f"{AUTHSERV}; spf=softfail smtp.mailfrom=evil.example",
          f"{AUTHSERV}; dmarc=pass header.from=test.com"), "unevaluated"),
        # A pass tied to no address.
        ((f"{AUTHSERV}; dmarc=pass",), "no_header_from"),
        # A pass about another domain.
        ((f"{AUTHSERV}; dmarc=pass header.from=evil.example",), "misaligned"),
        # Someone else's stamp on top of ours.
        (("mx.evil.example; dmarc=pass header.from=test.com",) + PASS, "unstamped"),
        # A lower stamp of ours disagreeing vetoes the top one.
        (PASS + (f"{AUTHSERV}; dmarc=fail header.from=test.com",), "fail"),
    ])
    def test_a_pass_the_sender_could_have_written_is_refused(
            self, authenticated, db_path, auth, reason):
        task_id = _hold(authenticated)
        _poll(authenticated, sender=HOST_ADDR, body=f"!confirm {task_id}", auth=auth)
        assert _status(db_path, task_id) == "pending_confirmation"
        assert _ledger(db_path)["routing_method"] == f"answer_refused:{reason}"

    def test_an_unstamped_answer_is_dropped(self, authenticated, db_path):
        task_id = _hold(authenticated)
        _poll(authenticated, sender=HOST_ADDR, body=f"!confirm {task_id}")
        assert _status(db_path, task_id) == "pending_confirmation"
        assert _ledger(db_path)["routing_method"] == "answer_refused:unstamped"

    def test_without_an_authserv_id_no_answer_by_mail_is_accepted(self, config, db_path):
        task_id = _hold(config)
        created, queued, raised, _ = _poll(
            config, sender=HOST_ADDR, body=f"!confirm {task_id}",
            auth=("mx.any; dmarc=pass header.from=test.com",),
        )
        assert created == [] and queued == []
        assert _status(db_path, task_id) == "pending_confirmation"
        assert _ledger(db_path)["routing_method"] == "answer_refused:unavailable"
        assert len(raised) == 1

    def test_repeated_refusals_raise_one_open_notice(self, authenticated, db_path):
        task_id = _hold(authenticated)
        for _ in range(3):
            _poll(authenticated, sender=HOST_ADDR, body=f"!confirm {task_id}")
        with db.get_db(db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id = ? "
                "AND dedup_key LIKE 'email-answer-refused:%'", (HOST,)).fetchone()[0] == 1


class TestOrdinaryMailIsUntouched:
    def test_mail_that_is_not_an_answer_still_runs(self, authenticated, db_path):
        created, _, _, acked = _poll(
            authenticated, sender=HOST_ADDR, body="What is on today?", auth=PASS,
        )
        assert len(created) == 1 and acked == []

    def test_a_bare_yes_outside_a_reply_is_ordinary_mail(self, authenticated, db_path):
        _hold(authenticated)
        created, _, _, _ = _poll(authenticated, sender=HOST_ADDR, body="yes", auth=PASS)
        assert len(created) == 1


class TestTheRequestWording:
    def test_it_offers_email_when_an_answer_by_mail_works(self, authenticated, db_path):
        task_id = _hold(authenticated)
        with db.get_db(db_path) as conn:
            prompt = db.get_task(conn, task_id).confirmation_prompt
        assert "email included" in prompt

    def test_it_names_somewhere_else_when_it_does_not(self, config, db_path):
        task_id = _hold(config)
        with db.get_db(db_path) as conn:
            prompt = db.get_task(conn, task_id).confirmation_prompt
        assert "email included" not in prompt
        assert "not accepted by email" in prompt
        assert "Talk" in prompt


class TestTheRequestSubject:
    def test_the_mailed_request_names_its_task(self, config):
        from istota.notifications import delivery
        from istota.transport import Destination

        with (
            patch.object(delivery, "resolve_destinations",
                         return_value=[Destination("email", None)]),
            patch.object(delivery, "_send_email", return_value=True) as send,
        ):
            delivery.send_confirmation_prompt(config, HOST, "body", task_id=7)
        assert send.call_args.args[2] == "Confirm task #7"


class TestTheRequestMessageId:
    def test_the_mailed_request_records_its_message_id(self, config, db_path):
        from istota.notifications import delivery
        from istota.transport import Destination

        with (
            patch.object(delivery, "resolve_destinations",
                         return_value=[Destination("email", None)]),
            patch("istota.skills.email.send_email", return_value=REQUEST_ID),
            patch("istota.mail.support.get_email_config"),
        ):
            delivery.send_confirmation_prompt(config, HOST, "body", task_id=9)
        with db.get_db(db_path) as conn:
            row = db.kv_get(conn, HOST, confirmations.REQUEST_MESSAGE_IDS_NAMESPACE, "9")
        assert row["value"] == REQUEST_ID


class TestTheConfirmGrammar:
    def test_yes_trust_is_one_answer(self):
        from istota.commands import parse_confirm_words

        assert parse_confirm_words("confirm", "41 yes trust") == (41, "trust")
        assert parse_confirm_words("no", "41") == (41, "decline")
        assert isinstance(parse_confirm_words("no", "41 trust"), str)


class TestTheDoctorCheck:
    def test_it_warns_when_alerts_reach_only_email_and_answers_cannot(self, config):
        from istota import doctor
        from istota.transport import Destination

        with patch("istota.notifications.delivery.resolve_destinations",
                   return_value=[Destination("email", None)]):
            result = doctor.check_email_confirmation_answers(config, probe=False)
        assert result.status == doctor.WARN

    def test_email_and_ntfy_counts_as_unanswerable(self, config):
        from istota import doctor
        from istota.transport import Destination

        with patch("istota.notifications.delivery.resolve_destinations",
                   return_value=[Destination("email", None), Destination("ntfy", None)]):
            result = doctor.check_email_confirmation_answers(config, probe=False)
        assert result.status == doctor.WARN

    def test_it_is_ok_once_answers_by_mail_work(self, authenticated):
        from istota import doctor
        from istota.transport import Destination

        with patch("istota.notifications.delivery.resolve_destinations",
                   return_value=[Destination("email", None)]):
            result = doctor.check_email_confirmation_answers(authenticated, probe=False)
        assert result.status == doctor.OK
