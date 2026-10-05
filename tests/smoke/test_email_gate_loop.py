"""The untrusted-sender gate's approve loop, answered by mail on the lean stack.

A stranger's mail at the host's plus-address is held, and the confirmation
request reaches the host by mail because `email_people` routes the host's
alerts to email. The host answers it by mail, as ISSUE-649 made possible: a
first-line `!confirm` from the host's own address, acted on only under a DMARC
pass our own MTA stamped (`transport/email/answers.py`,
`inbound._answer_refusal`). Each case reads the request out of the catch-all and
answers with the line it names (`email_flow.confirm_command_from`).

Case 5 of the spec's gate loop, approval on a thread from before the rework,
needs a `sent_emails` row with no room, which the product no longer writes; it
lives on the wire tier (`tests/testbed/test_email_intake.py`), where the
database belongs to the test.

Expected values cite the function they come from.
"""

from __future__ import annotations

import time

import pytest

from istota.notifications.resolvers import outbound_draft
from istota.notifications.resolvers.task_alert import flatten_body
from testbed.services import mail

from ..support import email_flow as flow

pytestmark = [pytest.mark.smoke, pytest.mark.profile("email")]


def _hold(stack, email_people, nonce: str, *, sender: flow.Correspondent,
          parent=None, with_references: bool = True, label: str = "gate"):
    """`sender` mails the host's plus-address and the gate holds it. Returns
    the mail, its held task and the request the host was mailed."""
    sent = flow.send(
        stack, sender, to=[mail.tagged(email_people.host_id)],
        subject=f"Re: {parent.subject}" if parent is not None else f"{label} {nonce}",
        text=f"a {label} question {nonce}", marker=nonce,
        reply_to_msg=parent, with_references=with_references,
    )
    task = flow.held_task(stack, sent)
    flow.assert_unknown_sender_prompt(task, sender.address)
    request = flow.request_mail(stack, sent, task)
    return sent, task, request


def _answered(stack, answer: flow.Sent, task: dict) -> dict:
    """The answer was filed as one and named the held task
    (`inbound.poll_emails`, the ISSUE-649 branch: `confirm_answer`)."""
    row = flow.filed_row(stack, answer)
    assert (row["routing_method"], row["task_id"]) == ("confirm_answer", task["id"]), row
    return row


def _ack(stack, answer: flow.Sent, task: dict) -> mail.ReceivedMessage:
    """The ack mailed back once the answer's transaction committed, under a
    subject that is not the request's (`answers.deliver_acks`)."""
    return flow.wait_for_reply(
        stack, after_uid=answer.outbox_uid, subject=f"Answered: task #{task['id']}",
    )


def _draft_push(stack, email_people, task_id: int, since: flow.Checkpoint):
    """The draft a held reply made, and what its notice pushes: the stored
    body as written, code spans included (`outbound_draft.delivery_body_for`),
    unlike the gate prompt, whose push goes through `flatten_body`. Equal to
    the row by construction, so the content is pinned by fragments the
    producer composes and by `assert_outcome`'s check that the sender's words
    are absent.
    """
    drafts = [
        d for d in stack.probe.drafts(email_people.host_id,
                                      id_above=since.mark.get("outbound_drafts"))
        if d.get("task_id") == task_id
    ]
    assert len(drafts) == 1, drafts
    key = outbound_draft.dedup_key(drafts[0]["id"])
    rows = stack.probe.notifications(email_people.host_id, dedup_key=key)
    assert len(rows) == 1, rows
    push = rows[0]["body"]
    for fragment in ("Nothing was sent.", f"`!drafts send {drafts[0]['id']}`"):
        assert fragment in push, (fragment, push)
    return key, push


def _trusted_thread(stack, email_people, nonce: str) -> tuple[flow.Sent, str]:
    """A thread room minted by a trusted sender's mail, answered. Returns the
    mail and the room token."""
    trusted = flow.person("trusted", nonce)
    marker = f"t-{nonce}"
    sent = flow.send(
        stack, trusted, to=[mail.tagged(email_people.host_id)],
        subject=f"thread {nonce}", text=f"a trusted opener {nonce}", marker=marker,
    )
    task = stack.probe.wait_for_task(status="completed", timeout=flow.DEFAULT_TIMEOUT,
                                     task_id=flow.filed_row(stack, sent)["task_id"])
    assert task["status"] == "completed", task
    assert flow.worker_done(stack, task["id"])
    room = stack.probe.email_room(sent.message_id)
    assert room is not None, f"the trusted mail minted no thread room: {sent.message_id}"
    # The opener's note reaches the host by mail as well; wait for it, so it
    # cannot land above a later step's outbox mark and be counted there.
    deadline = time.monotonic() + flow.DEFAULT_TIMEOUT
    while not any(
        email_people.host_address in flow.recipients(m, "To")
        for m in flow.bot_mail_since(stack, sent.outbox_uid)
    ):
        assert time.monotonic() < deadline, "the opener's note never reached the host"
        time.sleep(flow.POLL_INTERVAL)
    return sent, room


class Issue650(AssertionError):
    """The behaviour #650 reports, raised only by the checks of it."""


def _trusted_rows(stack, user_id: str, address: str) -> list[dict]:
    return stack.probe.query(
        "SELECT sender_email FROM trusted_email_senders WHERE user_id = ? "
        "AND lower(sender_email) = lower(?)", [user_id, address],
    )


class TestTheApproveLoop:
    def test_a_plain_yes_admits_the_sender_to_one_thread(self, stack, email_people):
        """`!confirm N` approves the held mail and admits its sender to that
        thread only (`confirmations.approve` with `trust_sender=False`).

        Approval mints the thread room and records the mail there, carrying
        the `received_mail` copied from the ledger (`threads._admit_approved_mail`,
        through `mail_card.stored_received_mail`), and recomputes
        `host_absent` (`intake_facts`): the host was not on the mail. Admission
        is not trust, so the outbound gate still holds the reply to the stranger
        under the `untrusted` floor (`transport/email/outbound.py`, through
        `recipients_require_hold`): a draft, the thread's bot row at `held`, and
        a `Reply waiting for your approval.` note in the bell.
        """
        nonce = flow.new_nonce()
        answer = f"the approved answer {nonce}"
        follow_up = f"the follow-up answer {nonce}"
        stranger = flow.person("stranger", nonce)
        plus = mail.tagged(email_people.host_id)
        stack.script([flow.route(nonce, [flow.email_answer(answer)]),
                      flow.route(f"f-{nonce}", [flow.email_answer(follow_up)])])
        sent, task, request = _hold(stack, email_people, nonce, sender=stranger)

        reply = flow.answer_by_mail(stack, request, "yes", nonce=nonce)
        _answered(stack, reply, task)
        assert "Confirmed." in _ack(stack, reply, task).body_text

        stack.probe.wait_for_task(status="completed", task_id=task["id"],
                                  timeout=flow.DEFAULT_TIMEOUT)
        assert flow.worker_done(stack, task["id"])
        draft_key, draft_push = _draft_push(
            stack, email_people, task["id"], flow.Checkpoint(stack.mark, 0),
        )
        seen = flow.assert_outcome(stack, sent, flow.Expected(
            processed=flow.Processed(
                routing_method="plus_address", user_id=email_people.host_id,
                host_asked=False, sender_check="verified",
            ),
            task=flow.TaskState(status="completed", host_absent=True),
            room="thread",
            participants=frozenset({(stranger.address, "guest", None)}),
            transcript=flow.Transcript(
                incoming=flow.Incoming(to=(plus,), cc=(), sender_check="verified",
                                       trusted=False),
                bot=flow.BotRow(body=answer, mail_state="held"),
            ),
            reply=None,
            note=flow.Note(outcome="held", without_you=True, remark=None),
            notices=flow.Notices(
                rows=frozenset({("confirmation", "task:{task}"),
                                ("outbound_draft", draft_key),
                                ("task_alert", "private-note:{task}")}),
                pushes=(flow.prompt_push(task), draft_push, flow.PRIVATE_NOTE_POINTER),
                # The request, the ack, the draft notice and the note.
                alert_mails=4,
            ),
        ))
        room = seen.room

        # The sender is on the thread now, so a reply there is not held.
        since = flow.checkpoint(stack)
        again = flow.send(
            stack, stranger, to=[plus], subject=f"Re: {sent.subject}",
            text=f"a follow-up {nonce}", marker=f"f-{nonce}", reply_to_msg=sent,
        )
        row = flow.filed_row(stack, again)
        stack.probe.wait_for_task(status="completed", task_id=row["task_id"],
                                  timeout=flow.DEFAULT_TIMEOUT)
        assert flow.worker_done(stack, row["task_id"])
        draft_key, draft_push = _draft_push(stack, email_people, row["task_id"], since)
        seen = flow.assert_outcome(stack, again, flow.Expected(
            processed=flow.Processed(
                routing_method="thread_room", user_id=email_people.host_id,
                host_asked=False, sender_check="verified",
            ),
            task=flow.TaskState(status="completed", host_absent=True),
            room="thread",
            participants=frozenset({(stranger.address, "guest", None)}),
            transcript=flow.Transcript(
                incoming=flow.Incoming(to=(plus,), cc=(), sender_check="verified",
                                       trusted=False),
                bot=flow.BotRow(body=follow_up, mail_state="held"),
            ),
            reply=None,
            note=flow.Note(outcome="held", without_you=True, remark=None),
            notices=flow.Notices(
                rows=frozenset({("outbound_draft", draft_key),
                                ("task_alert", "private-note:{task}")}),
                pushes=(draft_push, flow.PRIVATE_NOTE_POINTER),
                alert_mails=2,
            ),
        ), since=since)
        assert seen.room == room

        # A new thread from the same sender is held again: the yes was for T1.
        _hold(stack, email_people, f"n-{nonce}", sender=stranger, label="second")
        assert _trusted_rows(stack, email_people.host_id, stranger.address) == []

    def test_yes_trust_trusts_the_sender_everywhere(self, stack, email_people):
        """`!confirm N trust` approves and writes the sender into the runtime
        trust table (`confirmations.approve(trust_sender=True)`), which the
        outbound gate reads too, so the reply goes out unheld and a new thread
        from the sender is not held. The card keeps the ledger's verdict from
        intake, when the sender was not yet trusted (`mail_card.stored_received_mail`).
        """
        nonce = flow.new_nonce()
        answer = f"the trusted answer {nonce}"
        stranger = flow.person("stranger", nonce)
        plus = mail.tagged(email_people.host_id)
        stack.script([flow.route(nonce, [flow.email_answer(answer)]),
                      flow.route(f"n-{nonce}", [flow.email_answer(f"again {nonce}")])])
        sent, task, request = _hold(stack, email_people, nonce, sender=stranger)

        reply = flow.answer_by_mail(stack, request, "yes trust", nonce=nonce)
        _answered(stack, reply, task)
        assert f"Trusted {stranger.address}" in _ack(stack, reply, task).body_text

        flow.assert_outcome(stack, sent, flow.Expected(
            processed=flow.Processed(
                routing_method="plus_address", user_id=email_people.host_id,
                host_asked=False, sender_check="verified",
            ),
            task=flow.TaskState(status="completed", host_absent=True),
            room="thread",
            participants=frozenset({(stranger.address, "guest", None)}),
            transcript=flow.Transcript(
                incoming=flow.Incoming(to=(plus,), cc=(), sender_check="verified",
                                       trusted=False),
                bot=flow.BotRow(body=answer, mail_state="sent"),
            ),
            reply=flow.Reply(to=(stranger.address,), cc=()),
            note=flow.Note(outcome="sent", without_you=True, remark=None),
            notices=flow.Notices(
                rows=frozenset({("confirmation", "task:{task}"),
                                ("task_alert", "private-note:{task}")}),
                pushes=(flow.prompt_push(task), flow.PRIVATE_NOTE_POINTER),
                alert_mails=3,
            ),
        ))
        assert len(_trusted_rows(stack, email_people.host_id, stranger.address)) == 1

        fresh = flow.send(
            stack, stranger, to=[plus], subject=f"second {nonce}",
            text=f"a second thread {nonce}", marker=f"n-{nonce}",
        )
        row = flow.filed_row(stack, fresh)
        task = stack.probe.wait_for_task(status="completed", task_id=row["task_id"],
                                         timeout=flow.DEFAULT_TIMEOUT)
        assert task["status"] == "completed", task
        assert flow.wait_for_reply(stack, after_uid=fresh.outbox_uid,
                                   subject=f"Re: {fresh.subject}")

    def test_no_discards_the_mail(self, stack, email_people):
        """`!confirm N no` cancels the held task (`confirmations.decline`): no
        model run, no room, nothing to the sender, and the `confirmation` row
        resolved. No note: the task never reached `process_one_task`."""
        nonce = flow.new_nonce()
        stranger = flow.person("stranger", nonce)
        sent, task, request = _hold(stack, email_people, nonce, sender=stranger)

        reply = flow.answer_by_mail(stack, request, "no", nonce=nonce)
        _answered(stack, reply, task)
        assert "Task cancelled." in _ack(stack, reply, task).body_text

        flow.assert_outcome(stack, sent, flow.Expected(
            processed=flow.Processed(
                routing_method="plus_address", user_id=email_people.host_id,
                host_asked=False, sender_check="verified",
            ),
            task=flow.TaskState(status="cancelled", host_absent=False),
            room="none", participants=None,
            transcript=flow.Transcript(incoming=None, bot=None),
            reply=None, note=None,
            notices=flow.Notices(
                rows=frozenset({("confirmation", "task:{task}")}),
                pushes=(flow.prompt_push(task),),
                alert_mails=2,
            ),
        ))
        [row] = stack.probe.notifications(email_people.host_id,
                                          dedup_key=f"task:{task['id']}")
        assert row["state"] == "resolved", row

    def test_a_reply_to_the_request_answers_it(self, stack, email_people):
        """The request's other answer shape: a reply naming the request's
        Message-ID with a bare answer on its first line
        (`answers.read_answer`, through `confirmations.replies_to_request`)."""
        nonce = flow.new_nonce()
        sent, task, request = _hold(stack, email_people, nonce,
                                    sender=flow.person("stranger", nonce))
        reply = flow.send(
            stack, flow.person("host", nonce), to=[mail.BOT_ADDRESS],
            subject=f"Re: {request.subject}", text="no", marker=f"answer-{nonce}",
            reply_to_msg=request,
        )
        _answered(stack, reply, task)
        assert stack.probe.wait_for_task(
            status="cancelled", task_id=task["id"], timeout=flow.DEFAULT_TIMEOUT,
        )["status"] == "cancelled"


class TestAThreadThatAlreadyExists:
    def test_in_reply_to_alone_finds_the_thread_after_a_hold(self, stack, email_people):
        """A stranger's reply naming its parent in `In-Reply-To` alone, held
        and approved, lands in the existing thread's room.

        Approval rebuilds the mail from its ledger row, which keeps
        `In-Reply-To` since `f927eae1` (`processed_emails.in_reply_to`, read by
        `threads._admit_approved_mail`); before it the rebuilt mail named no
        parent and approval minted a second room or none.
        """
        nonce = flow.new_nonce()
        answer = f"the thread answer {nonce}"
        stack.script([flow.route(f"t-{nonce}", [flow.email_answer(f"opened {nonce}")]),
                      flow.route(nonce, [flow.email_answer(answer)])])
        opener, room = _trusted_thread(stack, email_people, nonce)
        trusted = opener.sender

        since = flow.checkpoint(stack)
        stranger = flow.person("stranger", nonce)
        sent, task, request = _hold(stack, email_people, nonce, sender=stranger,
                                    parent=opener, with_references=False)
        assert sent.references is None
        reply = flow.answer_by_mail(stack, request, "yes", nonce=nonce)
        _answered(stack, reply, task)

        stack.probe.wait_for_task(status="completed", task_id=task["id"],
                                  timeout=flow.DEFAULT_TIMEOUT)
        assert flow.worker_done(stack, task["id"])
        # The room first: it is what this case is about.
        [ran] = stack.probe.tasks(task_id=task["id"])
        assert stack.probe._canonical_room(ran["conversation_token"] or "") == room, (
            ran["conversation_token"], room,
        )
        assert stack.probe.email_room(sent.message_id) == room
        draft_key, draft_push = _draft_push(stack, email_people, task["id"], since)
        plus = mail.tagged(email_people.host_id)
        seen = flow.assert_outcome(stack, sent, flow.Expected(
            processed=flow.Processed(
                routing_method="thread_room", user_id=email_people.host_id,
                host_asked=False, sender_check="verified",
            ),
            task=flow.TaskState(status="completed", host_absent=True),
            room="thread",
            participants=frozenset({(trusted, "guest", None),
                                    (stranger.address, "guest", None)}),
            transcript=flow.Transcript(
                incoming=flow.Incoming(to=(plus,), cc=(), sender_check="verified",
                                       trusted=False),
                bot=flow.BotRow(body=answer, mail_state="held"),
            ),
            reply=None,
            note=flow.Note(outcome="held", without_you=True, remark=None),
            notices=flow.Notices(
                rows=frozenset({("confirmation", "task:{task}"),
                                ("outbound_draft", draft_key),
                                ("task_alert", "private-note:{task}")}),
                pushes=(flow.prompt_push(task), draft_push, flow.PRIVATE_NOTE_POINTER),
                alert_mails=4,
            ),
        ), since=since)
        assert seen.room == room, (seen.room, room)

    @pytest.mark.xfail(strict=True, raises=Issue650, reason="#650")
    def test_approving_on_a_vetoed_thread_records_nothing(self, stack, email_people):
        """A held reply on a thread then switched off, approved by mail.

        Today `confirmations.approve` has no veto check: `_admit_approved_mail`
        adds the sender as a participant before `record_inbound` refuses the
        vetoed room, the task runs with no conversation token, so
        `task_room_vetoed` cannot stop it, and it ends `failed` on delivery.
        A vetoed room records no participant (multiplayer D12); #650 asks for a
        refusal or a dropped task with no model run.

        The xfail covers only `Issue650`, raised by the two checks of that
        behaviour. A setup step that fails is a plain failure, so the strict
        xfail cannot pass for the wrong reason.
        """
        nonce = flow.new_nonce()
        stack.script([flow.route(f"t-{nonce}", [flow.email_answer(f"opened {nonce}")]),
                      flow.route(nonce, [flow.email_answer(f"vetoed {nonce}")])])
        opener, room = _trusted_thread(stack, email_people, nonce)
        stranger = flow.person("stranger", nonce)
        sent, task, request = _hold(stack, email_people, nonce, sender=stranger,
                                    parent=opener)

        # From the opener: an `off` is heard only from a present participant
        # (`inbound.poll_emails`, the veto branch, `threads.is_present`), and
        # the host is none, since the opener mailed the plus-address alone.
        # The veto's ledger row carries no Message-ID or subject, so the room's
        # policy is what is waited on.
        flow.send(
            stack, flow.Correspondent(opener.sender, None, flow.stamp(opener.sender)),
            to=[mail.tagged(email_people.host_id)], subject=f"Re: {opener.subject}",
            text="!istota off", marker=f"off-{nonce}", reply_to_msg=opener,
        )
        deadline = time.monotonic() + flow.DEFAULT_TIMEOUT
        while not stack.probe.query(
            "SELECT 1 FROM room_policy WHERE room_token = ? AND vetoed_at IS NOT NULL",
            [room],
        ):
            assert time.monotonic() < deadline, f"room {room} was not switched off"
            time.sleep(flow.POLL_INTERVAL)

        reply = flow.answer_by_mail(stack, request, "yes", nonce=nonce)
        _answered(stack, reply, task)
        # Settle whichever way #650 is fixed: a run ends in the worker's line,
        # a refusal leaves the task parked or cancelled.
        if not flow.worker_done(stack, task["id"], timeout=60):
            [after] = stack.probe.tasks(task_id=task["id"])
            assert after["status"] in ("pending_confirmation", "cancelled"), after
        [after] = stack.probe.tasks(task_id=task["id"])
        present = {p["surface_ref"] for p in stack.probe.participants(room)
                   if p.get("left_at") is None}
        if stranger.address in present:
            raise Issue650(f"the vetoed room gained a participant: {present}")
        if after["status"] == "failed":
            raise Issue650(f"the approved task ran and failed: {after}")


class TestAForgedAnswer:
    def test_a_forged_answer_changes_nothing(self, stack, email_people):
        """`!confirm N` from the host's address under a DMARC fail.

        Refused by its own rule, whatever `confirm_sender_match` says
        (`inbound._answer_refusal`): filed `answer_refused:fail` with no task,
        not held, so it raises no second request, and it leaves the held task
        parked. One `email-answer-refused:<reason>` notice is raised and pushed
        on the alert route, and it never quotes the mail
        (`answers.write_refusal_notice`, `answers.refusal_text`). No ack is
        mailed, and the refused answer runs no DMARC canary (ISSUE-649).
        """
        nonce = flow.new_nonce()
        stranger = flow.person("stranger", nonce)
        sent, task, request = _hold(stack, email_people, nonce, sender=stranger)
        # The request's push is in; read everything after it.
        ntfy = stack.service("ntfy")
        deadline = time.monotonic() + flow.DEFAULT_TIMEOUT
        while not ntfy.pushes() and time.monotonic() < deadline:
            time.sleep(flow.POLL_INTERVAL)
        time.sleep(flow.POST_COUNT_SETTLE)
        since = flow.checkpoint(stack)
        # The canary's row is keyed per user and verdict, so on a session
        # stack an earlier test may have left it open; a canary run would then
        # bump it in place, below any watermark. Its counter is what moves.
        canary_before = self._canary(stack, email_people)

        forged = flow.answer_by_mail(stack, request, "yes", nonce=nonce,
                                     sender=flow.person("spoofed_host", nonce))
        row = flow.filed_row(stack, forged)
        assert (row["routing_method"], row["task_id"], row["user_id"]) == (
            "answer_refused:fail", None, email_people.host_id,
        ), row

        deadline = time.monotonic() + flow.DEFAULT_TIMEOUT
        while len(ntfy.pushes()) <= since.pushes and time.monotonic() < deadline:
            time.sleep(flow.POLL_INTERVAL)
        time.sleep(flow.NEGATIVE_SETTLE)

        # The refusal row is keyed by reason alone, so this case can raise it
        # once per session stack; a second run would bump it and push nothing.
        notices = stack.probe.notifications(
            email_people.host_id, id_above=since.mark.get("notifications"))
        assert [(n["source"], n["dedup_key"]) for n in notices] == [
            ("task_alert", "email-answer-refused:fail"),
        ], notices
        push = flatten_body(notices[0]["body"])
        for fragment in ("was not acted on", "result: fail", "Nothing was approved"):
            assert fragment in push, (fragment, push)
        pushes = [c.body.decode("utf-8", "replace") for c in ntfy.pushes()[since.pushes:]]
        assert pushes == [push], pushes
        assert self._canary(stack, email_people) == canary_before

        # Nothing else moved: the held task is still parked, no task was
        # created for the answer, and the bot mailed only the notice.
        [parked] = stack.probe.tasks(task_id=task["id"])
        assert parked["status"] == "pending_confirmation", parked
        assert stack.probe.rows_above("tasks", since.mark,
                                      user_id=email_people.host_id) == []
        mailed = flow.bot_mail_since(stack, forged.outbox_uid)
        assert [m.subject for m in mailed] == ["An answer by email was ignored"], [
            (m.subject, m.recipients) for m in mailed
        ]
        for message in mailed:
            assert forged.text not in message.body_text
        assert flow.reply_to(stack, sent) is None

    @staticmethod
    def _canary(stack, email_people) -> list[tuple]:
        return [
            (n["id"], n["occurrences"], n["updated_at"])
            for n in stack.probe.notifications(email_people.host_id,
                                               dedup_key="dmarc:fail")
        ]
