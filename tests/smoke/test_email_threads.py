"""Multi-party email threads on the lean stack, built mail by mail.

A thread with people besides the host and the bot is a room
(`transport/email/threads.py`). These cases mint one at the send and on
receipt, walk one thread through five mails from correspondents of every
standing, and pin the one release from the outbound gate: the host's own
authenticated ask (`threads.host_asked`, ISSUE-607). Every step asserts all
eight dimensions of `tests/support/email_flow.Expected`, reading above a
checkpoint taken before it.

Expected values cite the function they come from. Where the spec's wording and
the product differ, the docstring says so and the product is what is asserted.
"""

from __future__ import annotations

import time
from email.utils import parseaddr

import pytest

from testbed.services import mail

from ..support import email_flow as flow

pytestmark = [pytest.mark.smoke, pytest.mark.profile("email")]


#: Prints where a draft's Open action goes: the resolver's own
#: `_open_href`, read inside the container through the daemon's config.
_OPEN_HREF = (
    "import sys\n"
    "from pathlib import Path\n"
    "from istota import db\n"
    "from istota.config import load_config\n"
    "from istota.mail import drafts\n"
    "from istota.notifications.resolvers.outbound_draft import _open_href\n"
    "config = load_config(Path('/data/config/config.toml'))\n"
    "with db.get_db(config.db_path) as conn:\n"
    "    print(_open_href(conn, drafts.get(conn, int(sys.argv[1])), sys.argv[2]))\n"
)


def _open_href(stack, draft_id: int, user_id: str) -> str:
    result = stack.exec(
        ["uv", "run", "python", "-c", _OPEN_HREF, str(draft_id), user_id], timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


def _named(text: str) -> str:
    """New text that asks the bot by name (`threads.asked_by_name`)."""
    return f"Istota, {text}"


def _completed(stack, sent: flow.Sent) -> dict:
    """The task `sent` made, once its worker has finished with it."""
    row = flow.filed_row(stack, sent)
    assert row["task_id"] is not None, row
    task = stack.probe.wait_for_task(status="completed", task_id=row["task_id"],
                                     timeout=flow.DEFAULT_TIMEOUT)
    assert task["status"] == "completed", task
    assert flow.worker_done(stack, task["id"])
    return task


def _split_epochs(stack, room: str) -> list[dict]:
    """The room's epochs past the baseline: one per join that grew the
    audience (`db.note_audience_join`, through `upsert_room_participant`)."""
    return stack.probe.query(
        "SELECT epoch, reason, person FROM room_epochs "
        "WHERE room_token = ? AND epoch > 0 ORDER BY epoch",
        [room],
    )


def _bot_address(message: mail.ReceivedMessage) -> str:
    return parseaddr(message.sender)[1].lower()


class TestMinting:
    def test_a_thread_the_bot_starts_is_minted_at_the_send(self, stack, email_people):
        """The host asks, from their private email room, for a mail to two
        correspondents; the room is minted when the send is recorded.

        `email send` records the send through the deferred file the scheduler
        replays (`scheduler_deferred`), which calls `threads.register_sent_thread`:
        anyone besides the host on it mints the room with the sent mail as its
        first row, an outgoing card tied to no task (`record_sent_mail`), and
        the recipients as its people. A reply from one of them finds that room
        and is answered with reply-all.

        Two trusted recipients rather than the spec's trusted and stranger: a
        stranger on `email send` is held by the outbound gate under the default
        `untrusted` floor (`skills/email._outbound_gate`, through
        `recipients_require_hold`), and a held draft mints nothing until it is
        released. A stranger already on a thread being admitted is the mixed
        thread's fourth step.
        """
        nonce = flow.new_nonce()
        first = flow.person("trusted", f"{nonce}a")
        second = flow.person("trusted", f"{nonce}b")
        subject = f"Planning {nonce}"
        body = f"Shall we meet on Thursday {nonce}?"
        command = (
            f"istota-skill email send --to {first.address} --cc {second.address} "
            f"--subject '{subject}' --body '{body}'"
        )
        answer = f"sent it {nonce}"
        replied = f"thursday works {nonce}"
        stack.script([
            flow.route(nonce, [
                {"tool_calls": [{"id": f"call-send-{nonce}", "name": "Bash",
                                 "arguments": {"command": command}}]},
                flow.email_answer(answer),
            ]),
            flow.route(f"r-{nonce}", [flow.email_answer(replied)]),
        ])
        ask = flow.send(
            stack, flow.person("host", nonce), to=[mail.BOT_ADDRESS],
            subject=f"send {nonce}", text=f"please write to my friends {nonce}",
            marker=nonce,
        )
        flow.assert_outcome(stack, ask, flow.Expected(
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
            reply=flow.Reply(to=(email_people.host_address,), cc=()),
            note=None,
            notices=flow.Notices(rows=frozenset(), pushes=(), alert_mails=0),
        ))

        sent_mail = flow.wait_for_reply(stack, after_uid=ask.outbox_uid, subject=subject)
        assert flow.recipients(sent_mail, "To") == (first.address,)
        assert flow.recipients(sent_mail, "Cc") == (second.address,)
        room = stack.probe.email_room(sent_mail.message_id)
        assert room is not None, (
            f"the send minted no thread room: {sent_mail.message_id}"
        )
        rows = stack.probe.room_messages(room)
        assert len(rows) == 1, rows
        card = rows[0]["outgoing_mail"]
        assert (rows[0]["role"], rows[0]["task_id"], rows[0]["body"].strip()) == (
            "assistant", None, body,
        ), rows[0]
        assert (card["to"], card["cc"], card["state"]) == (
            [first.address], [second.address], "sent",
        ), card
        assert {
            (p["surface_ref"], p["kind"], p["user_id"])
            for p in stack.probe.participants(room) if p.get("left_at") is None
        } == {(first.address, "guest", None), (second.address, "guest", None)}

        # A recipient's reply-all lands in that room and is answered to all.
        since = flow.checkpoint(stack)
        bot = _bot_address(sent_mail)
        reply = flow.send(
            stack, first, to=[bot], cc=[second.address], subject=f"Re: {subject}",
            text=f"that suits me {nonce}", marker=f"r-{nonce}", reply_to_msg=sent_mail,
        )
        seen = flow.assert_outcome(stack, reply, flow.Expected(
            processed=flow.Processed(
                routing_method="thread_room", user_id=email_people.host_id,
                host_asked=False, sender_check="verified",
            ),
            task=flow.TaskState(status="completed", host_absent=True),
            room="thread",
            participants=frozenset({(first.address, "guest", None),
                                    (second.address, "guest", None)}),
            transcript=flow.Transcript(
                incoming=flow.Incoming(to=(bot,), cc=(second.address,),
                                       sender_check="verified", trusted=True),
                bot=flow.BotRow(body=replied, mail_state="sent"),
            ),
            reply=flow.Reply(to=(first.address,), cc=(second.address,)),
            note=flow.Note(outcome="sent", without_you=True, remark=None),
            notices=flow.Notices(
                rows=frozenset({("task_alert", "private-note:{task}")}),
                pushes=(flow.PRIVATE_NOTE_POINTER,),
                alert_mails=1,
            ),
        ), since=since)
        assert seen.room == room

    def test_the_hosts_mail_to_a_correspondent_mints_on_receipt(self, stack, email_people):
        """The host writes to a trusted correspondent with the bot in To.

        The host's own address on the mail is the evidence that mints it
        (`threads.resolve_thread`); the host is its principal and the
        correspondent a guest. Asked, with the bot in To (`thread_addressed`),
        and `host_asked`, since the mail authenticated (`inbound.poll_emails`).
        Reply-all to the latest mail: the host in To, the correspondent in Cc.
        The host was on the mail and the reply went out, so no note
        (`private_replies.email_note_due`).
        """
        nonce = flow.new_nonce()
        answer = f"booked for two {nonce}"
        stack.script([flow.route(nonce, [flow.email_answer(answer)])])
        friend = flow.person("trusted", nonce)
        plus = mail.tagged(email_people.host_id)
        host = email_people.host_address
        sent = flow.send(
            stack, flow.person("host", nonce), to=[plus, friend.address],
            subject=f"Dinner {nonce}", text=f"please book us a table {nonce}",
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


class TestAMixedThread:
    def test_five_mails_from_everyone_on_one_thread(self, stack, email_people):
        """The host, alice, a trusted correspondent, and two strangers alice
        adds, on one thread.

        1. The host starts it to alice and the trusted correspondent, the bot
           in To: asked, `host_asked`, and reply-all goes out unheld to the
           host with alice and the correspondent in Cc, though alice is not
           trusted (`threads.host_asked`, ISSUE-607). No note.
        2. The trusted correspondent replies to all, naming the bot: asked,
           reply-all follows that mail (`threads.reply_all`: its sender in
           To, its other recipients in Cc). The spec expects it to go out;
           alice is on it and is not trusted for the host
           (`config.is_trusted_email_sender`), so the outbound gate holds it
           (`recipients_require_hold`) and a held reply owes a note even with
           the host on the mail (`email_note_due`).
        3. alice replies to all, adding two strangers, without naming the
           bot, the host in Cc: recorded only (`thread_addressed`), each
           newcomer a participant and an epoch split.
        4. A stranger now on the thread replies to all, naming the bot: not
           held at the gate, since they are present, and asked. The reply is
           held, a draft whose Open link is the draft's own room on lean
           (`outbound_draft._open_href`; no note room, `note_room_for_task`),
           and the note says so with no "without you" header.
        5. The other newcomer writes to the bot alone: `host_absent`, the
           scripted `NO_ACTION:` sends nothing and writes no bot row, and the
           note quotes their stored words and carries the reason as its remark
           (`email_note_remark`).
        """
        nonce = flow.new_nonce()
        host = flow.person("host", nonce)
        alice = flow.person("alice", nonce)
        trusted = flow.person("trusted", nonce)
        stranger = flow.person("stranger", f"{nonce}s")
        newcomer = flow.person("stranger", f"{nonce}m")
        plus = mail.tagged(email_people.host_id)
        subject = f"Offsite {nonce}"
        answers = {step: f"answer {step} {nonce}" for step in (1, 2, 4)}
        reason = f"nothing for the bot here {nonce}"
        stack.script([
            flow.route(f"1-{nonce}", [flow.email_answer(answers[1])]),
            flow.route(f"2-{nonce}", [flow.email_answer(answers[2])]),
            flow.route(f"4-{nonce}", [flow.email_answer(answers[4])]),
            flow.route(f"5-{nonce}", [{"text": f"NO_ACTION: {reason}"}]),
        ])
        before_split = {
            (host.address, "principal", email_people.host_id),
            (alice.address, "guest", email_people.alice_id),
            (trusted.address, "guest", None),
        }
        after_split = frozenset(before_split | {
            (stranger.address, "guest", None), (newcomer.address, "guest", None),
        })
        before_split = frozenset(before_split)

        # 1. The host starts it.
        one = flow.send(
            stack, host, to=[plus, alice.address, trusted.address], subject=subject,
            text=f"can you find a venue for the three of us {nonce}?",
            marker=f"1-{nonce}",
        )
        seen = flow.assert_outcome(stack, one, flow.Expected(
            processed=flow.Processed(
                routing_method="plus_address", user_id=email_people.host_id,
                host_asked=True, sender_check="verified",
            ),
            task=flow.TaskState(status="completed", host_absent=False),
            room="thread", participants=before_split,
            transcript=flow.Transcript(
                incoming=flow.Incoming(to=(plus, alice.address, trusted.address), cc=(),
                                       sender_check="verified", trusted=False),
                bot=flow.BotRow(body=answers[1], mail_state="sent"),
            ),
            reply=flow.Reply(to=(host.address,), cc=(alice.address, trusted.address)),
            note=None,
            notices=flow.Notices(rows=frozenset(), pushes=(), alert_mails=0),
        ))
        room = seen.room
        first_reply = seen.reply
        bot = _bot_address(first_reply)
        assert _split_epochs(stack, room) == []

        # 2. The trusted correspondent replies to all, naming the bot.
        since = flow.checkpoint(stack)
        two = flow.send(
            stack, trusted, to=[bot], cc=[host.address, alice.address],
            subject=f"Re: {subject}", text=_named(f"is Friday free {nonce}?"),
            marker=f"2-{nonce}", reply_to_msg=first_reply,
        )
        task = _completed(stack, two)
        draft, draft_key, draft_push = flow.held_draft(stack, task["id"], since)
        assert (draft["to_addrs"], draft["cc_addrs"]) == (
            [trusted.address], [host.address, alice.address],
        ), draft
        flow.assert_outcome(stack, two, flow.Expected(
            processed=flow.Processed(
                routing_method="thread_room", user_id=email_people.host_id,
                host_asked=False, sender_check="verified",
            ),
            task=flow.TaskState(status="completed", host_absent=False),
            room="thread", participants=before_split,
            transcript=flow.Transcript(
                incoming=flow.Incoming(to=(bot,), cc=(host.address, alice.address),
                                       sender_check="verified", trusted=True),
                bot=flow.BotRow(body=answers[2], mail_state="held"),
            ),
            reply=None,
            note=flow.Note(outcome="held", without_you=False, remark=None),
            notices=flow.Notices(
                rows=frozenset({("outbound_draft", draft_key),
                                ("task_alert", "private-note:{task}")}),
                pushes=(draft_push, flow.PRIVATE_NOTE_POINTER),
                alert_mails=2,
            ),
        ), since=since)

        # 3. alice replies to all and adds two strangers, not asking the bot.
        since = flow.checkpoint(stack)
        three = flow.send(
            stack, alice, to=[trusted.address],
            cc=[bot, host.address, stranger.address, newcomer.address],
            subject=f"Re: {subject}",
            text=f"looping in two colleagues who know the area {nonce}",
            marker=f"3-{nonce}", reply_to_msg=two,
        )
        flow.assert_outcome(stack, three, flow.Expected(
            processed=flow.Processed(
                routing_method="thread_room", user_id=email_people.host_id,
                host_asked=False, sender_check="verified",
            ),
            task=None, room="thread", participants=after_split,
            transcript=flow.Transcript(
                incoming=flow.Incoming(
                    to=(trusted.address,),
                    cc=(bot, host.address, stranger.address, newcomer.address),
                    sender_check="verified", trusted=False,
                ),
                bot=None,
            ),
            reply=None, note=None,
            notices=flow.Notices(rows=frozenset(), pushes=(), alert_mails=0),
        ), since=since)
        assert [(e["reason"], e["person"]) for e in _split_epochs(stack, room)] == [
            ("email_join", f"email:{stranger.address}"),
            ("email_join", f"email:{newcomer.address}"),
        ]

        # 4. A stranger now on the thread asks; the reply is held.
        since = flow.checkpoint(stack)
        four = flow.send(
            stack, stranger, to=[alice.address],
            cc=[trusted.address, bot, host.address, newcomer.address],
            subject=f"Re: {subject}", text=_named(f"what about the old mill {nonce}?"),
            marker=f"4-{nonce}", reply_to_msg=three,
        )
        task = _completed(stack, four)
        draft, draft_key, draft_push = flow.held_draft(stack, task["id"], since)
        assert (draft["to_addrs"], draft["cc_addrs"], draft["room_token"]) == (
            [stranger.address],
            [alice.address, trusted.address, host.address, newcomer.address],
            room,
        ), draft
        assert _open_href(stack, draft["id"], email_people.host_id) == (
            f"/chat/r/{room}/t/{task['id']}"
        )
        flow.assert_outcome(stack, four, flow.Expected(
            processed=flow.Processed(
                routing_method="thread_room", user_id=email_people.host_id,
                host_asked=False, sender_check="verified",
            ),
            task=flow.TaskState(status="completed", host_absent=False),
            room="thread", participants=after_split,
            transcript=flow.Transcript(
                incoming=flow.Incoming(
                    to=(alice.address,),
                    cc=(trusted.address, bot, host.address, newcomer.address),
                    sender_check="verified", trusted=False,
                ),
                bot=flow.BotRow(body=answers[4], mail_state="held"),
            ),
            reply=None,
            note=flow.Note(outcome="held", without_you=False, remark=None),
            notices=flow.Notices(
                rows=frozenset({("outbound_draft", draft_key),
                                ("task_alert", "private-note:{task}")}),
                pushes=(draft_push, flow.PRIVATE_NOTE_POINTER),
                alert_mails=2,
            ),
        ), since=since)

        # 5. The other newcomer writes to the bot alone; nothing is sent.
        since = flow.checkpoint(stack)
        five = flow.send(
            stack, newcomer, to=[bot], subject=f"Re: {subject}",
            text=f"just between us, I will be late {nonce}",
            marker=f"5-{nonce}", reply_to_msg=three,
        )
        seen = flow.assert_outcome(stack, five, flow.Expected(
            processed=flow.Processed(
                routing_method="thread_room", user_id=email_people.host_id,
                host_asked=False, sender_check="verified",
            ),
            task=flow.TaskState(status="completed", host_absent=True),
            room="thread", participants=after_split,
            transcript=flow.Transcript(
                incoming=flow.Incoming(to=(bot,), cc=(), sender_check="verified",
                                       trusted=False),
                bot=None,
            ),
            reply=None,
            note=flow.Note(outcome="none", without_you=True, remark=reason),
            notices=flow.Notices(
                rows=frozenset({("task_alert", "private-note:{task}")}),
                pushes=(flow.PRIVATE_NOTE_POINTER,),
                alert_mails=1,
            ),
        ), since=since)
        # Quoted from the stored turn, not from the model's text.
        assert five.text in seen.note_body
        assert seen.room == room
        # Nothing the bot said reached the thread after the first reply.
        assert [m.subject for m in flow.bot_mail_since(stack, two.outbox_uid)
                if newcomer.address in flow.recipients(m, "To")
                or stranger.address in flow.recipients(m, "To")] == []


class TestTheHostsOwnAsk:
    def test_the_host_asking_releases_the_reply_and_a_stranger_asking_does_not(
        self, stack, email_people,
    ):
        """The one release from the outbound gate is the host's own
        authenticated, addressed ask, answered to the people it was asked in
        front of (`threads.host_asked`, set by `inbound.poll_emails` only with
        `authserv_id` set and a DMARC pass). The stranger on the thread is
        not trusted, so the same thread asked by them is held.
        """
        nonce = flow.new_nonce()
        answer = f"the host's answer {nonce}"
        theirs = f"the stranger's answer {nonce}"
        stack.script([flow.route(nonce, [flow.email_answer(answer)]),
                      flow.route(f"s-{nonce}", [flow.email_answer(theirs)])])
        stranger = flow.person("stranger", nonce)
        plus = mail.tagged(email_people.host_id)
        host = email_people.host_address
        asked = flow.send(
            stack, flow.person("host", nonce), to=[plus, stranger.address],
            subject=f"Quote {nonce}", text=f"please send the quote to my contact {nonce}",
            marker=nonce,
        )
        participants = frozenset({(host, "principal", email_people.host_id),
                                  (stranger.address, "guest", None)})
        seen = flow.assert_outcome(stack, asked, flow.Expected(
            processed=flow.Processed(
                routing_method="plus_address", user_id=email_people.host_id,
                host_asked=True, sender_check="verified",
            ),
            task=flow.TaskState(status="completed", host_absent=False),
            room="thread", participants=participants,
            transcript=flow.Transcript(
                incoming=flow.Incoming(to=(plus, stranger.address), cc=(),
                                       sender_check="verified", trusted=False),
                bot=flow.BotRow(body=answer, mail_state="sent"),
            ),
            reply=flow.Reply(to=(host,), cc=(stranger.address,)),
            note=None,
            notices=flow.Notices(rows=frozenset(), pushes=(), alert_mails=0),
        ))
        assert stack.probe.drafts(
            email_people.host_id, id_above=stack.mark.get("outbound_drafts"),
        ) == []

        since = flow.checkpoint(stack)
        bot = _bot_address(seen.reply)
        theirs_sent = flow.send(
            stack, stranger, to=[bot], cc=[host], subject=f"Re: {asked.subject}",
            text=_named(f"can the quote include delivery {nonce}?"),
            marker=f"s-{nonce}", reply_to_msg=seen.reply,
        )
        task = _completed(stack, theirs_sent)
        draft, draft_key, draft_push = flow.held_draft(stack, task["id"], since)
        assert (draft["to_addrs"], draft["cc_addrs"]) == ([stranger.address], [host])
        flow.assert_outcome(stack, theirs_sent, flow.Expected(
            processed=flow.Processed(
                routing_method="thread_room", user_id=email_people.host_id,
                host_asked=False, sender_check="verified",
            ),
            task=flow.TaskState(status="completed", host_absent=False),
            room="thread", participants=participants,
            transcript=flow.Transcript(
                incoming=flow.Incoming(to=(bot,), cc=(host,), sender_check="verified",
                                       trusted=False),
                bot=flow.BotRow(body=theirs, mail_state="held"),
            ),
            reply=None,
            note=flow.Note(outcome="held", without_you=False, remark=None),
            notices=flow.Notices(
                rows=frozenset({("outbound_draft", draft_key),
                                ("task_alert", "private-note:{task}")}),
                pushes=(draft_push, flow.PRIVATE_NOTE_POINTER),
                alert_mails=2,
            ),
        ), since=since)


class TestRoomPost:
    def test_room_post_cannot_reach_a_thread_from_the_private_email_room(
        self, stack, email_people,
    ):
        """`room post` from the host's private email turn, aimed at a thread
        room, is refused and posts nothing.

        On lean the host has no web or Talk private room, so the only private
        room a task runs in is the private email room. `room post` asks first
        whether the turn is a relay origin (`private_replies.hold_room_post`,
        through `relay.relays.private_origin`), which takes web, Talk, SMS and
        WhatsApp only, so the refusal here is `unsupported_origin`, ahead of
        the `email_thread` refusal `_post_destination` makes for a web or Talk
        private room. That one needs a web room and is the full file's.
        """
        nonce = flow.new_nonce()
        opened = flow.route(f"t-{nonce}", [flow.email_answer(f"opened {nonce}")])
        stack.script([opened])
        friend = flow.person("trusted", nonce)
        opener = flow.send(
            stack, flow.person("host", nonce),
            to=[mail.tagged(email_people.host_id), friend.address],
            subject=f"Thread {nonce}", text=f"a thread to post into {nonce}",
            marker=f"t-{nonce}",
        )
        _completed(stack, opener)
        room = stack.probe.email_room(opener.message_id)
        assert room is not None

        call = f"call-post-{nonce}"
        posted = f"a post the thread must never see {nonce}"
        # The opener's route stays: a channel's memory extraction can quote
        # the opener while this test runs, and it is this test's marker.
        stack.script([opened, flow.route(nonce, [
            {"tool_calls": [{"id": call, "name": "Bash", "arguments": {"command": (
                f"istota-skill room post --request-key k-{nonce} --room {room} "
                f"'{posted}'"
            )}}]},
            flow.email_answer(f"could not post {nonce}"),
        ])])
        since = flow.checkpoint(stack)
        ask = flow.send(
            stack, flow.person("host", nonce), to=[mail.BOT_ADDRESS],
            subject=f"post {nonce}", text=f"post a line into my thread {nonce}",
            marker=nonce,
        )
        _completed(stack, ask)
        result = stack.endpoint.tool_results_by_id().get(call, "")
        assert "unsupported_origin" in result, result
        assert [r for r in stack.probe.room_messages(room, id_above=since.mark.get("messages"))
                if posted in (r.get("body") or "")] == []
        assert stack.probe.query(
            "SELECT id FROM whatsapp_skill_requests WHERE kind = 'room_post' "
            "AND service_body = ?", [posted],
        ) == []
        deadline = time.monotonic() + flow.NEGATIVE_SETTLE
        while time.monotonic() < deadline:
            assert not any(posted in m.body_text
                           for m in flow.bot_mail_since(stack, ask.outbox_uid))
            time.sleep(flow.POLL_INTERVAL)
