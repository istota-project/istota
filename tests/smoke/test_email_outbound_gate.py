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

Releasing or discarding a draft is not driven here. Nothing on the lean shape
can do it: a mail is an answer only when its first line is a `!confirm`
command or a reply to a mailed request (`transport.email.answers.read_answer`),
so a mailed `!drafts send <id>` is ordinary mail, and the `!drafts` command and
the `/chat/drafts/{id}` routes need Talk or the web app. Release and discard,
with the thread row's state following the draft (`db.settle_draft_mail`), are
the full file's (`tests/full/test_email_rooms.py`).
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
    flow.assert_outcome(stack, sent, _expected(
        email_people, sender, other, plus, answer=answer, held=False,
        notices=flow.Notices(
            rows=frozenset({("task_alert", "private-note:{task}")}),
            pushes=(flow.PRIVATE_NOTE_POINTER,), alert_mails=1,
        ),
    ))
    assert stack.probe.drafts(
        email_people.host_id, id_above=stack.mark.get("outbound_drafts"),
    ) == []


def _assert_held(stack, email_people, nonce: str, cc_kind: str, reason: str) -> None:
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
        flow.assert_outcome(stack, sent, flow.Expected(
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
        assert stack.probe.drafts(
            email_people.host_id, id_above=stack.mark.get("outbound_drafts"),
        ) == []

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
        flow.assert_outcome(stack, sent, flow.Expected(
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
        assert stack.probe.drafts(
            email_people.host_id, id_above=stack.mark.get("outbound_drafts"),
        ) == []
