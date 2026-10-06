"""The outbound approval gate on a thread reply, on both lean email profiles.

A reply to an email thread goes through `transport.email.outbound`'s
`_hold_if_unapproved`, which asks `mail.outbound_policy.recipients_require_hold`
about every To and Cc address. Under the default `untrusted` floor (profile
`email`) a recipient that `Config.is_trusted_email_sender` does not clear holds
the whole message; under `all` (profile `email-hold-all`) only the user's own
addresses go out unapproved. A held reply is an `outbound_drafts` row, the
thread room's bot row carries the mailed body with `outgoing_mail.state` at
`held` and the draft's id, and a held reply always owes the host a note
(`private_replies.email_note_due`). The one release is the host's own
authenticated ask on the thread (`threads.host_asked`), which
`_send_thread_reply` passes as `approved` ahead of the policy.

A held draft is also answered by mail here (ISSUE-662): a first line
`!drafts send <id>` or `!drafts discard <id>` from the host's own address,
under a passing stamp, is an answer (`transport.email.answers.read_answer`)
filed `drafts_answer` with no task. A discard is applied in the poll's
transaction; a release runs after it commits (`answers.deliver_acks`), and
both are acked by mail. The id is required, even with one draft open, and a
forged answer is refused by its own rule as a forged `!confirm` is. Release
and discard from the web app, `/chat/drafts/{id}`, are in
`tests/full/test_email_rooms.py`.
"""

from __future__ import annotations

import pytest

from istota.mail.outbound_policy import HOLD_ALL_MODE, HOLD_UNTRUSTED
from testbed.services import mail

from ..support import email_flow as flow

pytestmark = pytest.mark.smoke

EMAIL = pytest.mark.profile("email")
HOLD_ALL = pytest.mark.profile("email-hold-all")


def _thread_with(stack, email_people, nonce: str, answer: str, cc_kind: str):
    """A trusted correspondent mails the host's plus-address with a second
    person, of `cc_kind`, on Cc. The sender is trusted by pattern, so the mail
    is not held at intake and mints a thread room with both as guests; the
    host is not on it, so the task is `host_absent` and a note is always due.
    The reply-all goes To the sender, Cc the other (`threads.reply_all`)."""
    stack.script([flow.route(nonce, [flow.email_answer(answer)])])
    sender = flow.person("trusted", nonce)
    other = flow.person(cc_kind, f"cc-{nonce}")
    plus = mail.tagged(email_people.host_id)
    sent = flow.send(
        stack, sender, to=[plus], cc=[other.address], subject=f"gate {nonce}",
        text=f"please confirm the delivery date {nonce}", marker=nonce,
    )
    return sent, sender, other, plus


def _expected(email_people, sender, other, plus, *, answer: str, held: bool,
              notices: flow.Notices) -> flow.Expected:
    return flow.Expected(
        processed=flow.Processed(
            routing_method="plus_address", user_id=email_people.host_id,
            host_asked=False, sender_check="verified",
        ),
        task=flow.TaskState(status="completed", host_absent=True),
        room="thread",
        participants=frozenset({(sender.address, "guest", None),
                                (other.address, "guest", None)}),
        transcript=flow.Transcript(
            incoming=flow.Incoming(to=(plus,), cc=(other.address,),
                                   sender_check="verified", trusted=True),
            bot=flow.BotRow(body=answer, mail_state="held" if held else "sent"),
        ),
        reply=None if held else flow.Reply(to=(sender.address,), cc=(other.address,)),
        note=flow.Note(outcome="held" if held else "sent", without_you=True,
                       remark=None),
        notices=notices,
    )


def _assert_sent_unheld(stack, email_people, nonce: str, cc_kind: str) -> None:
    answer = f"Delivery is on Tuesday {nonce}."
    sent, sender, other, plus = _thread_with(stack, email_people, nonce, answer, cc_kind)
    seen = flow.assert_outcome(stack, sent, _expected(
        email_people, sender, other, plus, answer=answer, held=False,
        notices=flow.Notices(
            rows=frozenset({("task_alert", "private-note:{task}")}),
            pushes=(flow.PRIVATE_NOTE_POINTER,), alert_mails=1,
        ),
    ))
    _assert_no_draft(stack, email_people, seen)


def _assert_no_draft(stack, email_people, seen: flow.Outcome) -> None:
    """No draft for this mail's task above the test's watermark."""
    assert seen.task is not None
    assert [d for d in stack.probe.drafts(
        email_people.host_id, id_above=stack.mark.get("outbound_drafts"),
    ) if d.get("task_id") == seen.task["id"]] == []


def _assert_held(stack, email_people, nonce: str, cc_kind: str, reason: str):
    """The reply is held: one pending draft carrying the reply-all exactly,
    with the gate's reason, and the thread row's card names that draft, which
    is what `settle_draft_mail` later moves. Nothing reaches the wire."""
    answer = f"Delivery is on Tuesday {nonce}."
    sent, sender, other, plus = _thread_with(stack, email_people, nonce, answer, cc_kind)
    row = flow.filed_row(stack, sent)
    assert row["task_id"] is not None, row
    task = stack.probe.wait_for_task(status="completed", task_id=row["task_id"],
                                     timeout=flow.DEFAULT_TIMEOUT)
    assert task["status"] == "completed", task
    # `completed` commits before delivery holds the draft; the worker's line
    # comes after delivery and the note.
    assert flow.worker_done(stack, task["id"]), f"task {task['id']} never finished"
    since = flow.Checkpoint(mark=stack.mark, pushes=0)
    draft, draft_key, draft_push = flow.held_draft(stack, task["id"], since)
    assert (draft["status"], draft["hold_reason"]) == ("pending", reason), draft
    assert (draft["to_addrs"], draft["cc_addrs"]) == (
        [sender.address], [other.address],
    ), draft
    assert draft["in_reply_to"] == sent.message_id, draft

    seen = flow.assert_outcome(stack, sent, _expected(
        email_people, sender, other, plus, answer=answer, held=True,
        notices=flow.Notices(
            rows=frozenset({("outbound_draft", draft_key),
                            ("task_alert", "private-note:{task}")}),
            pushes=(draft_push, flow.PRIVATE_NOTE_POINTER),
            alert_mails=2,
        ),
    ))
    assert draft["room_token"] == seen.room, (draft, seen.room)
    bot_rows = [r for r in seen.rows
                if r["role"] == "assistant" and r.get("task_id") == task["id"]]
    assert [r["outgoing_mail"].get("draft_id") for r in bot_rows] == [draft["id"]], bot_rows
    return sent, sender, other, draft, draft_key, draft_push, seen


@EMAIL
class TestTheUntrustedFloor:
    def test_a_reply_all_to_trusted_people_goes_out(self, stack, email_people):
        """Both recipients match testuser's `*@trusted.test` pattern, so
        `recipients_require_hold` answers None and the reply goes out with no
        draft. The host was not on the mail, so the note is `Replied.`."""
        _assert_sent_unheld(stack, email_people, flow.new_nonce(), "trusted")

    def test_one_stranger_among_the_recipients_holds_the_reply(
        self, stack, email_people,
    ):
        """The Cc'd stranger is on no list, and one recipient the gate does
        not clear holds the whole message (`recipients_require_hold`, the
        `untrusted` branch: `HOLD_UNTRUSTED`). Being on the thread clears the
        inbound gate for them, never the outbound one."""
        _assert_held(stack, email_people, flow.new_nonce(), "stranger", HOLD_UNTRUSTED)


@HOLD_ALL
class TestTheAllFloor:
    def test_a_reply_all_to_trusted_people_is_held(self, stack, email_people):
        """The same all-trusted reply-all the `untrusted` floor sends. Under
        `all` only the user's own addresses go out unapproved
        (`recipients_require_hold`, the `all` branch: `HOLD_ALL_MODE`)."""
        _assert_held(stack, email_people, flow.new_nonce(), "trusted", HOLD_ALL_MODE)

    def test_the_hosts_private_room_reply_goes_out(self, stack, email_people):
        """The host mails the bot alone: the answer is a reply to the host's
        own address in the private email room, which the `all` branch of
        `recipients_require_hold` sends (`mail.support.own_addresses`). No
        thread, so no note."""
        nonce = flow.new_nonce()
        answer = f"the private answer {nonce}"
        stack.script([flow.route(nonce, [flow.email_answer(answer)])])
        sent = flow.send(
            stack, flow.person("host", nonce), to=[mail.BOT_ADDRESS],
            subject=f"own address {nonce}", text=f"a question of my own {nonce}",
            marker=nonce,
        )
        host = email_people.host_address
        seen = flow.assert_outcome(stack, sent, flow.Expected(
            processed=flow.Processed(
                routing_method="sender_match", user_id=email_people.host_id,
                host_asked=False, sender_check="verified",
            ),
            task=flow.TaskState(status="completed", host_absent=False),
            room="private",
            participants=frozenset({
                (email_people.host_id, "principal", email_people.host_id),
            }),
            transcript=flow.Transcript(
                incoming=flow.Incoming(to=(mail.BOT_ADDRESS,), cc=(),
                                       sender_check="verified", trusted=False),
                bot=flow.BotRow(body=answer, mail_state="sent"),
            ),
            reply=flow.Reply(to=(host,), cc=()),
            note=None,
            notices=flow.Notices(rows=frozenset(), pushes=(), alert_mails=0),
        ))
        _assert_no_draft(stack, email_people, seen)

    def test_the_hosts_own_ask_on_a_thread_goes_out(self, stack, email_people):
        """The host asks on a thread with a trusted correspondent, stamped,
        bot in To: `host_asked`, and `_send_thread_reply` passes it as
        `approved` before any policy is consulted (ISSUE-607), so even `all`
        does not hold it. The host was on the mail and it went out: no note."""
        nonce = flow.new_nonce()
        answer = f"the host's answer {nonce}"
        stack.script([flow.route(nonce, [flow.email_answer(answer)])])
        friend = flow.person("trusted", nonce)
        plus = mail.tagged(email_people.host_id)
        host = email_people.host_address
        sent = flow.send(
            stack, flow.person("host", nonce), to=[plus, friend.address],
            subject=f"ask {nonce}", text=f"please send my friend the plan {nonce}",
            marker=nonce,
        )
        seen = flow.assert_outcome(stack, sent, flow.Expected(
            processed=flow.Processed(
                routing_method="plus_address", user_id=email_people.host_id,
                host_asked=True, sender_check="verified",
            ),
            task=flow.TaskState(status="completed", host_absent=False),
            room="thread",
            participants=frozenset({(host, "principal", email_people.host_id),
                                    (friend.address, "guest", None)}),
            transcript=flow.Transcript(
                incoming=flow.Incoming(to=(plus, friend.address), cc=(),
                                       sender_check="verified", trusted=False),
                bot=flow.BotRow(body=answer, mail_state="sent"),
            ),
            reply=flow.Reply(to=(host,), cc=(friend.address,)),
            note=None,
            notices=flow.Notices(rows=frozenset(), pushes=(), alert_mails=0),
        ))
        _assert_no_draft(stack, email_people, seen)


# -- answering a held draft by mail (ISSUE-662) ---------------------------------


def _answer(stack, nonce: str, text: str, *, sender: str = "host") -> flow.Sent:
    """A mail to the bot's bare address whose first line is `text`."""
    return flow.send(
        stack, flow.person(sender, nonce), to=[mail.BOT_ADDRESS],
        subject=f"drafts {nonce}-{flow.new_nonce()}", text=text,
        marker=f"answer-{nonce}",
    )


def _ack(stack, answer: flow.Sent, subject: str) -> str:
    message = flow.wait_for_reply(stack, after_uid=answer.outbox_uid, subject=subject)
    assert flow.HOST_ADDRESS in flow.recipients(message, "To"), message.headers
    return message.body_text


def _draft(stack, email_people, draft_id: int) -> dict:
    [row] = [d for d in stack.probe.drafts(email_people.host_id) if d["id"] == draft_id]
    return row


def _card_state(stack, room: str, task_id: int) -> str | None:
    [row] = [r for r in stack.probe.room_messages(room)
             if r["role"] == "assistant" and r.get("task_id") == task_id]
    return (row.get("outgoing_mail") or {}).get("state")


@EMAIL
class TestADraftAnsweredByMail:
    def test_send_by_mail_releases_the_draft(self, stack, email_people):
        """`!drafts send <id>` from the host, stamped: filed `drafts_answer`
        with no task, the reply-all goes out threaded on the mail it answers,
        `sent_emails` records it, the draft is `sent`, the thread row's card
        follows (`db.settle_draft_mail`), the notice resolves by `email`, and
        the ack names what was sent.

        The notice, which can itself arrive as a mail, names the verbs and
        carries no caveat: `outbound_draft.delivery_body_for` adds "Answers
        are not accepted by email" only where an answer by mail cannot be
        authenticated, and every lean email profile sets `authserv_id`.
        `answers.drafts_answer_places`, which names email outright, words the
        stale-draft reminder only, which no case here waits long enough for.
        """
        nonce = flow.new_nonce()
        sent, sender, other, draft, key, push, seen = _assert_held(
            stack, email_people, nonce, "stranger", HOLD_UNTRUSTED)
        assert f"`!drafts send {draft['id']}`" in push, push
        assert "not accepted by email" not in push, push
        since = flow.checkpoint(stack)

        answer = _answer(stack, nonce, f"!drafts send {draft['id']}")
        row = flow.filed_row(stack, answer)
        assert (row["routing_method"], row["task_id"], row["user_id"]) == (
            "drafts_answer", None, email_people.host_id,
        ), row
        ack = _ack(stack, answer, f"Answered: draft #{draft['id']}")
        assert f"Sent #{draft['id']} to" in ack, ack

        released = flow._wait(lambda: flow.reply_to(stack, sent),
                              timeout=flow.DEFAULT_TIMEOUT)
        assert released is not None, "the released draft never reached the wire"
        assert (flow.recipients(released, "To"), flow.recipients(released, "Cc")) == (
            (sender.address,), (other.address,),
        ), released.headers
        settled = _draft(stack, email_people, draft["id"])
        assert settled["status"] == "sent", settled
        recorded = stack.probe.query(
            "SELECT * FROM sent_emails WHERE message_id = ?",
            [settled["sent_message_id"]],
        )
        assert [(r["user_id"], r["task_id"], r["in_reply_to"]) for r in recorded] == [
            (email_people.host_id, seen.task["id"], sent.message_id),
        ], recorded
        assert _card_state(stack, seen.room, seen.task["id"]) == "sent"
        [notice] = stack.probe.notifications(email_people.host_id, dedup_key=key)
        assert (notice["state"], notice["resolved_by"]) == ("resolved", "email"), notice
        assert stack.probe.rows_above("tasks", since.mark, source_type="email",
                                      user_id=email_people.host_id) == []

    def test_discard_by_mail_after_a_forgery_and_a_missing_id(
        self, stack, email_people,
    ):
        """Three answers to one draft.

        1. `!drafts send <id>` under a DMARC fail: refused by its own rule
           (`inbound._answer_refusal`), filed `answer_refused:fail`, no task,
           and the draft still pending. The refusal raises the draft kind's
           notice (`answers.write_refusal_notice`), keyed by reason, so on a
           session stack it may already be open; its row is what is read.
        2. `!drafts send` with no id, authentic: filed `drafts_answer`, the
           ack asks for the id, and nothing is released, though a draft is
           open (`answers.apply_drafts_answer`).
        3. `!drafts discard <id>`, authentic: applied in the poll's
           transaction; nothing reaches the wire, the draft and the thread
           row's card are `discarded`, and the notice resolves by `email`.
        """
        nonce = flow.new_nonce()
        sent, _sender, _other, draft, key, _push, seen = _assert_held(
            stack, email_people, nonce, "stranger", HOLD_UNTRUSTED)
        since = flow.checkpoint(stack)

        forged = _answer(stack, nonce, f"!drafts send {draft['id']}",
                         sender="spoofed_host")
        row = flow.filed_row(stack, forged)
        assert (row["routing_method"], row["task_id"], row["user_id"]) == (
            "answer_refused:fail", None, email_people.host_id,
        ), row
        refusals = stack.probe.notifications(
            email_people.host_id, dedup_key="email-draft-answer-refused:fail")
        assert [n["state"] for n in refusals] == ["open"], refusals
        assert "Nothing was sent or discarded" in refusals[0]["body"], refusals
        assert _draft(stack, email_people, draft["id"])["status"] == "pending"

        bare = _answer(stack, nonce, "!drafts send")
        row = flow.filed_row(stack, bare)
        assert (row["routing_method"], row["task_id"]) == ("drafts_answer", None), row
        ack = _ack(stack, bare, "Your answer by email")
        assert "By email, name the draft" in ack and "Nothing changed" in ack, ack
        assert _draft(stack, email_people, draft["id"])["status"] == "pending"

        discard = _answer(stack, nonce, f"!drafts discard {draft['id']}")
        row = flow.filed_row(stack, discard)
        assert (row["routing_method"], row["task_id"]) == ("drafts_answer", None), row
        ack = _ack(stack, discard, f"Answered: draft #{draft['id']}")
        assert f"Discarded #{draft['id']}" in ack, ack

        assert _draft(stack, email_people, draft["id"])["status"] == "discarded"
        assert _card_state(stack, seen.room, seen.task["id"]) == "discarded"
        [notice] = stack.probe.notifications(email_people.host_id, dedup_key=key)
        assert (notice["state"], notice["resolved_by"]) == ("resolved", "email"), notice
        assert flow._wait(lambda: flow.reply_to(stack, sent),
                          timeout=flow.NEGATIVE_SETTLE) is None
        assert stack.probe.rows_above("tasks", since.mark, source_type="email",
                                      user_id=email_people.host_id) == []
