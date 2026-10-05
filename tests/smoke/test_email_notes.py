"""The email note and the thread transcript on the lean stack (#636).

One row of the note table per test, each on a thread minted fresh for it. A
thread task's note is written after delivery by `scheduler._write_email_note`,
which reads the outcome off the thread row's outgoing card
(`scheduler._thread_reply_outcome`), the remark through
`private_replies.email_note_remark`, and asks `private_replies.email_note_due`
whether one is owed. The thread room's bot row is the mailed body or nothing
(`transport.email.outbound.composed_mail_body`, from `1c44a3f6`). Lean has no
private room, so every note is the `private-note:<task>` bell row
(`private_replies._bell_note`), pushed as `PRIVATE_NOTE_POINTER`.

| Case | Host on the mail | Model turns | Thread bot row | Note |
|---|---|---|---|---|
| Report beside the mail | no | `email output` B, then R | B, `sent` | `Replied.` then R |
| Envelope only | no | envelope B | B, `sent` | `Replied.`, no remark |
| Bare `NO_ACTION:` | no | `NO_ACTION:` | none | `No reply sent.`, no remark |
| Sent, host on it | yes | envelope B | B, `sent` | none |
| `NO_ACTION:` with a reason, host on it | yes | `NO_ACTION: R` | none | none (`b600dd60`) |
| Answered only to the host | yes | plain R | none | `No reply sent.` then R |

Every case asserts all eight dimensions through `email_flow.assert_outcome`,
which also checks the note's `task_logs` line (`Private note to the host:
<outcome>`) is there exactly when a note is, and that no push or alert mail
carries the sender's words or the remark. `_assert_kept_private` adds the
transcript rule: the remark is in no row of the thread room and the sender's
words are in no bot row there.

The last case is a scheduler retry: an attempt that fails writes no note, and
the attempt that succeeds writes one row and pushes once.
"""

from __future__ import annotations

import pytest

from testbed.services import mail

from ..support import email_flow as flow

pytestmark = [pytest.mark.smoke, pytest.mark.profile("email")]


#: How long the retry case waits for its second attempt: the first backoff is
#: one minute (`scheduler.decide_retry`), plus the run itself.
RETRY_TIMEOUT = 240.0


def _absent_thread(stack, email_people, nonce: str, turns: list[dict]):
    """A trusted correspondent mails the host's plus-address, with the host
    not on it: a thread room minted on receipt, `host_absent`, not held
    (`config.is_trusted_email_sender`, the pattern branch)."""
    stack.script([flow.route(nonce, turns)])
    sender = flow.person("trusted", nonce)
    plus = mail.tagged(email_people.host_id)
    sent = flow.send(
        stack, sender, to=[plus], subject=f"note {nonce}",
        text=f"could you look at the invoice {nonce}", marker=nonce,
    )
    return sent, sender, plus


def _absent(email_people, sender, plus, *, bot: flow.BotRow | None,
            replied: bool, note: flow.Note) -> flow.Expected:
    """A host-absent mail's outcome. A note is always due when the host was
    not on the mail (`email_note_due`), so the bell row and one pointer push
    (ntfy and the alert mail) are always there."""
    return flow.Expected(
        processed=flow.Processed(
            routing_method="plus_address", user_id=email_people.host_id,
            host_asked=False, sender_check="verified",
        ),
        task=flow.TaskState(status="completed", host_absent=True),
        room="thread",
        participants=frozenset({(sender.address, "guest", None)}),
        transcript=flow.Transcript(
            incoming=flow.Incoming(to=(plus,), cc=(), sender_check="verified",
                                   trusted=True),
            bot=bot,
        ),
        reply=flow.Reply(to=(sender.address,), cc=()) if replied else None,
        note=note,
        notices=flow.Notices(
            rows=frozenset({("task_alert", "private-note:{task}")}),
            pushes=(flow.PRIVATE_NOTE_POINTER,),
            alert_mails=1,
        ),
    )


def _present_thread(stack, email_people, nonce: str, turns: list[dict]):
    """The host writes to a trusted correspondent with the bot's bare address
    in To: routed `sender_match`, minted on receipt with the host as
    principal, `host_asked` since the mail authenticated."""
    stack.script([flow.route(nonce, turns)])
    friend = flow.person("trusted", nonce)
    sent = flow.send(
        stack, flow.person("host", nonce), to=[mail.BOT_ADDRESS, friend.address],
        subject=f"note {nonce}", text=f"could you book the room {nonce}",
        marker=nonce,
    )
    return sent, friend


def _present(email_people, friend, *, bot: flow.BotRow | None, replied: bool,
             note: flow.Note | None) -> flow.Expected:
    host = email_people.host_address
    return flow.Expected(
        processed=flow.Processed(
            routing_method="sender_match", user_id=email_people.host_id,
            host_asked=True, sender_check="verified",
        ),
        task=flow.TaskState(status="completed", host_absent=False),
        room="thread",
        participants=frozenset({(host, "principal", email_people.host_id),
                                (friend.address, "guest", None)}),
        transcript=flow.Transcript(
            incoming=flow.Incoming(to=(mail.BOT_ADDRESS, friend.address), cc=(),
                                   sender_check="verified", trusted=False),
            bot=bot,
        ),
        reply=flow.Reply(to=(host,), cc=(friend.address,)) if replied else None,
        note=note,
        notices=(
            flow.Notices(rows=frozenset({("task_alert", "private-note:{task}")}),
                         pushes=(flow.PRIVATE_NOTE_POINTER,), alert_mails=1)
            if note is not None
            else flow.Notices(rows=frozenset(), pushes=(), alert_mails=0)
        ),
    )


def _assert_kept_private(stack, sent: flow.Sent, seen: flow.Outcome,
                         remark: str | None, mailed: str | None) -> None:
    """The transcript rule (#636): the thread room records the mail and never
    the bot's report, and what went out carries only the mailed body."""
    assert seen.room is not None
    for row in stack.probe.room_messages(seen.room):
        body = row.get("body") or ""
        if remark:
            assert remark not in body, (row["role"], body)
        if row["role"] == "assistant":
            assert sent.text not in body, body
    if remark:
        for call in stack.service("ntfy").pushes():
            carried = call.body.decode("utf-8", "replace") + " " + " ".join(
                call.headers.values())
            assert remark not in carried, carried
    if seen.reply is not None:
        assert mailed is not None
        assert mailed in seen.reply.body_text, seen.reply.body_text
        if remark:
            assert remark not in seen.reply.body_text, seen.reply.body_text


class TestTheHostWasNotOnTheMail:
    def test_a_report_beside_the_mail_is_the_note_and_not_the_thread(
        self, stack, email_people,
    ):
        """The regression #636 started from. The model mails B with
        `email output` and ends with a report R to its user. The thread row is
        B (`composed_mail_body` reads the deferred file), the mail is B, and R
        is the note's remark under `Replied.`; B is not repeated as the remark
        (`email_note_remark` drops a remark equal to the mailed body)."""
        nonce = flow.new_nonce()
        body = f"The invoice is paid in full {nonce}."
        remark = f"I told them it was paid; nothing for you to do {nonce}."
        sent, sender, plus = _absent_thread(
            stack, email_people, nonce, flow.email_output_then(body, remark))
        seen = flow.assert_outcome(stack, sent, _absent(
            email_people, sender, plus,
            bot=flow.BotRow(body=body, mail_state="sent"), replied=True,
            note=flow.Note(outcome="sent", without_you=True, remark=remark),
        ))
        assert sent.text in seen.note_body
        _assert_kept_private(stack, sent, seen, remark, body)

    def test_the_envelope_alone_is_mailed_and_carries_no_remark(
        self, stack, email_people,
    ):
        """An answer that is only the envelope: the envelope is the mail, and
        with it removed nothing is left to be a remark
        (`email_note_remark` through `without_email_envelope`)."""
        nonce = flow.new_nonce()
        body = f"Paid on the third {nonce}."
        sent, sender, plus = _absent_thread(
            stack, email_people, nonce, [flow.email_answer(body)])
        seen = flow.assert_outcome(stack, sent, _absent(
            email_people, sender, plus,
            bot=flow.BotRow(body=body, mail_state="sent"), replied=True,
            note=flow.Note(outcome="sent", without_you=True, remark=None),
        ))
        assert sent.text in seen.note_body
        _assert_kept_private(stack, sent, seen, None, body)

    def test_a_bare_no_action_sends_nothing_and_still_tells_the_host(
        self, stack, email_people,
    ):
        """`NO_ACTION:` with no reason: nothing composed, so no thread row and
        no mail; the outcome is `none` (`_thread_reply_outcome`), and a
        host-absent turn owes a note whatever the outcome (`email_note_due`)."""
        nonce = flow.new_nonce()
        sent, sender, plus = _absent_thread(
            stack, email_people, nonce, [{"text": "NO_ACTION:"}])
        seen = flow.assert_outcome(stack, sent, _absent(
            email_people, sender, plus, bot=None, replied=False,
            note=flow.Note(outcome="none", without_you=True, remark=None),
        ))
        assert sent.text in seen.note_body
        _assert_kept_private(stack, sent, seen, None, None)


class TestTheHostWasOnTheMail:
    def test_a_reply_the_host_received_writes_no_note(self, stack, email_people):
        """The reply went out unheld to the host and the correspondent, so it
        is already in the host's inbox: no note (`email_note_due`)."""
        nonce = flow.new_nonce()
        body = f"Booked for Thursday {nonce}."
        sent, friend = _present_thread(
            stack, email_people, nonce, [flow.email_answer(body)])
        seen = flow.assert_outcome(stack, sent, _present(
            email_people, friend, bot=flow.BotRow(body=body, mail_state="sent"),
            replied=True, note=None,
        ))
        _assert_kept_private(stack, sent, seen, None, body)

    def test_a_no_action_reason_on_a_mail_the_host_was_on_writes_no_note(
        self, stack, email_people,
    ):
        """`NO_ACTION: R` with the host on the mail: the reason says nothing
        needs the host and is not an answer to them, so it does not count as
        a remark toward the decision (`_write_email_note`, `b600dd60`). No
        thread row, no mail, no note, and R reaches nobody."""
        nonce = flow.new_nonce()
        reason = f"they only said thanks {nonce}"
        sent, friend = _present_thread(
            stack, email_people, nonce, [{"text": f"NO_ACTION: {reason}"}])
        seen = flow.assert_outcome(stack, sent, _present(
            email_people, friend, bot=None, replied=False, note=None,
        ))
        _assert_kept_private(stack, sent, seen, reason, None)

    def test_an_answer_to_the_host_alone_is_a_note(self, stack, email_people):
        """Plain text with no envelope and no `email output`: nothing is
        mailed (`deliver_email_result` skips a task with no structured
        output), no thread row is stored, and the text is the bot answering
        the host alone, which owes a note with it as the remark
        (`email_note_due`: outcome `none` with a remark)."""
        nonce = flow.new_nonce()
        remark = f"The room is free on Thursday, shall I book it {nonce}?"
        sent, friend = _present_thread(stack, email_people, nonce, [{"text": remark}])
        seen = flow.assert_outcome(stack, sent, _present(
            email_people, friend, bot=None, replied=False,
            note=flow.Note(outcome="none", without_you=False, remark=remark),
        ))
        assert sent.text in seen.note_body
        _assert_kept_private(stack, sent, seen, remark, None)


class TestOneNotePerTask:
    def test_a_retried_thread_task_writes_one_note(self, stack, email_people):
        """The first attempt gets the endpoint's exhausted frame and fails;
        the scheduler retries it a minute later (`decide_retry`), and that
        attempt answers. `_write_email_note` runs only on a successful attempt,
        so the task has one `private-note:<task>` row, written once
        (`occurrences` 1), one `Private note to the host:` log line and one
        push."""
        nonce = flow.new_nonce()
        body = f"Second time lucky {nonce}."
        sent, sender, plus = _absent_thread(stack, email_people, nonce, [])
        row = flow.filed_row(stack, sent)
        assert row["task_id"] is not None, row
        task_id = row["task_id"]
        assert flow.worker_done(stack, task_id, timeout=RETRY_TIMEOUT), (
            f"task {task_id}'s first attempt never finished"
        )
        task = stack.probe.tasks(task_id=task_id)[0]
        assert (task["status"], task["attempt_count"]) == ("pending", 1), task
        assert stack.probe.notifications(
            email_people.host_id, dedup_key=f"private-note:{task_id}") == []

        # Rescripting clears the first attempt's exhausted request from
        # `unmatched`, which is the one this case meant to cause.
        stack.script([flow.route(nonce, [flow.email_answer(body)])])
        seen = flow.assert_outcome(stack, sent, _absent(
            email_people, sender, plus,
            bot=flow.BotRow(body=body, mail_state="sent"), replied=True,
            note=flow.Note(outcome="sent", without_you=True, remark=None),
        ), timeout=RETRY_TIMEOUT)
        assert seen.task["attempt_count"] == 1, seen.task
        rows = stack.probe.notifications(
            email_people.host_id, dedup_key=f"private-note:{task_id}")
        assert len(rows) == 1 and rows[0]["occurrences"] == 1, rows
