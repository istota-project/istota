"""Email intake on threads, at the wire: who is asked, who is recorded, who is on it.

Everything `poll_emails` decides before a task runs, against a real IMAP server
and a database this file owns: the routing rung, whether the mail is held,
whether a thread room is minted, the room's participants and their kinds, the
epoch split, whether a task is created and whether it is `host_absent`. No task
ever runs here; the lean stack's email files take over from there.

The intake table being exercised is `threads.thread_addressed`: the host's own
mail is asked when the bot is in To or named, anyone else's when it names the
bot or the host is not on it, and everything else on a thread is recorded in
the room with no task. The recorded-only cases are the ones that tell that
rule apart from "every admitted mail is asked", which is what the stage's
negative control (`thread_addressed` always true) turns red.

The config is the shipped default, `[speech_gate] mode = "classifier"`, and
deliberately not pinned to `mention`: an email thread room is kept off the
classifier by its own rule (`room_policy.effective_speech_mode`), and the
recorded-only cases read the gate's rung to say that rule decided. A pin
would make them pass whether or not the rule held.

Each case opens its own thread with addresses carrying a per-test nonce, so
nothing depends on another case's rows, and asserts on the rows its own
Message-IDs resolve to rather than on table counts.
"""

from __future__ import annotations

import json
import logging

import pytest

from istota import confirmations, db
from istota.config import UserConfig
from istota.transport.email.private_room import email_conversation_token
from testbed.services import mail

from ..support.email_flow import gate_rung, new_nonce, stamp
from .conftest import USER_ADDRESS, USER_ID, USER_TAG_ADDRESS

pytestmark = pytest.mark.testbed

ALICE_ID = "alice"
ALICE_ADDRESS = "alice@ext.test"


def _send(
    wire,
    sender: str,
    *,
    to: list[str],
    cc: list[str] = (),
    bcc: list[str] = (),
    subject: str,
    body: str,
    references: str | None = None,
    in_reply_to: str | None = None,
    verdict: str | None = None,
) -> str:
    """One mail on the wire; its Message-ID. ``verdict`` writes a stamp for
    the sender's domain (`pass` or `fail`), None writes no header."""
    headers = {"Authentication-Results": stamp(sender, verdict)} if verdict else None
    return wire.send(
        from_addr=sender, to_addr=list(to), cc=list(cc), bcc=list(bcc),
        subject=subject, body=body, references=references,
        in_reply_to=in_reply_to, headers=headers,
    )


def _named(wire, text: str) -> str:
    """A body whose new text puts a question to the bot by name."""
    return f"{wire.config.bot_name}, {text}"


def _task(wire, task_id: int | None) -> dict | None:
    if task_id is None:
        return None
    rows = wire.probe.query("SELECT * FROM tasks WHERE id = ?", [task_id])
    return rows[0] if rows else None


def _present(wire, room: str) -> set[tuple[str, str, str | None]]:
    """``(address, kind, user_id)`` for each participant still on the thread."""
    return {
        (row["surface_ref"], row["kind"], row["user_id"])
        for row in wire.probe.participants(room)
        if row["left_at"] is None
    }


def _members(wire, room: str) -> list[str]:
    return [
        row["user_id"] for row in wire.probe.query(
            "SELECT user_id FROM room_members WHERE room_token = ? ORDER BY user_id",
            [room],
        )
    ]


def _rows_for_mail(wire, message_id: str) -> list[dict]:
    """Every transcript row, in any room, carrying this mail's card."""
    rows = wire.probe.query(
        "SELECT * FROM messages WHERE received_mail IS NOT NULL ORDER BY id",
    )
    found = []
    for row in rows:
        card = json.loads(row["received_mail"])
        if card.get("message_id") == message_id:
            row["received_mail"] = card
            found.append(row)
    return found


def _split_epochs(wire, room: str) -> list[dict]:
    """The room's epochs past the baseline: one per join that grew the audience."""
    return wire.probe.query(
        "SELECT epoch, reason, person FROM room_epochs "
        "WHERE room_token = ? AND epoch > 0 ORDER BY epoch",
        [room],
    )


class _Thread:
    """A thread room the host opened with one stranger, by mail."""

    def __init__(self, wire, nonce: str):
        self.nonce = nonce
        self.stranger = f"s-{nonce}@stranger.test"
        self.subject = f"Dinner {nonce}"
        self.root = _send(
            wire, USER_ADDRESS, to=[USER_TAG_ADDRESS, self.stranger],
            subject=self.subject, body=_named(wire, "can you find a table for two?"),
            verdict="pass",
        )
        wire.poll()
        self.room = wire.probe.email_room(self.root)
        assert self.room is not None, wire.probe.processed(self.root)

    def reply(self, wire, sender: str, *, to, cc=(), body: str, **kwargs) -> str:
        return _send(
            wire, sender, to=to, cc=cc, subject=f"Re: {self.subject}", body=body,
            references=self.root, in_reply_to=self.root, **kwargs,
        )


@pytest.fixture
def thread(wire) -> _Thread:
    return _Thread(wire, new_nonce())


class TestTheHostsOwnMail:
    """`thread_addressed` for the host: the bot in To, or named."""

    def test_the_bot_in_to_is_asked(self, wire, thread):
        """The thread opener: host to the bot and a stranger, which mints."""
        row = wire.probe.processed(thread.root)
        assert row["routing_method"] == "plus_address"
        assert row["user_id"] == USER_ID
        assert row["thread_id"] == thread.room
        task = _task(wire, row["task_id"])
        assert task is not None
        assert task["status"] == "pending"
        assert task["host_absent"] == 0
        assert task["conversation_token"] == thread.room
        assert _present(wire, thread.room) == {
            (USER_ADDRESS, "principal", USER_ID),
            (thread.stranger, "guest", None),
        }

    def test_the_bot_in_cc_unnamed_is_recorded_only(self, wire):
        """The host writes to a stranger and copies the bot without asking it."""
        nonce = new_nonce()
        stranger = f"s-{nonce}@stranger.test"
        mid = _send(
            wire, USER_ADDRESS, to=[stranger], cc=[USER_TAG_ADDRESS],
            subject=f"Plans {nonce}", body="Shall we meet on Friday?",
            verdict="pass",
        )

        created = wire.poll()

        row = wire.probe.processed(mid)
        assert row["routing_method"] == "plus_address"
        assert row["task_id"] is None
        assert created == []
        room = wire.probe.email_room(mid)
        assert room is not None, "the host's thread is a room whether or not it asks"
        assert _present(wire, room) == {
            (USER_ADDRESS, "principal", USER_ID),
            (stranger, "guest", None),
        }
        recorded = _rows_for_mail(wire, mid)
        assert [(r["room_token"], r["task_id"]) for r in recorded] == [(room, None)]
        assert gate_rung(wire.probe, recorded[0]["id"]) == ["mode_mention"]

    def test_the_bot_in_cc_named_is_asked(self, wire):
        nonce = new_nonce()
        stranger = f"s-{nonce}@stranger.test"
        mid = _send(
            wire, USER_ADDRESS, to=[stranger], cc=[USER_TAG_ADDRESS],
            subject=f"Plans {nonce}", body=_named(wire, "what is free on Friday?"),
            verdict="pass",
        )

        created = wire.poll()

        row = wire.probe.processed(mid)
        assert row["task_id"] is not None
        assert created == [row["task_id"]]
        assert _task(wire, row["task_id"])["host_absent"] == 0
        room = wire.probe.email_room(mid)
        assert room is not None
        assert _present(wire, room) == {
            (USER_ADDRESS, "principal", USER_ID),
            (stranger, "guest", None),
        }

    def test_the_bot_in_to_unnamed_is_asked(self, wire):
        """The bot in To is enough for the host's own mail, without a name.
        The `thread` fixture's opener also names the bot, so it cannot tell
        the two arms of the rule apart; this case can."""
        nonce = new_nonce()
        stranger = f"s-{nonce}@stranger.test"
        mid = _send(
            wire, USER_ADDRESS, to=[USER_TAG_ADDRESS, stranger],
            subject=f"Venue {nonce}", body="Please find us a venue for Saturday.",
            verdict="pass",
        )

        created = wire.poll()

        row = wire.probe.processed(mid)
        assert row["routing_method"] == "plus_address"
        assert created == [row["task_id"]]
        task = _task(wire, row["task_id"])
        assert task["status"] == "pending"
        assert task["host_absent"] == 0
        room = wire.probe.email_room(mid)
        assert room is not None
        assert _present(wire, room) == {
            (USER_ADDRESS, "principal", USER_ID),
            (stranger, "guest", None),
        }


    @pytest.mark.parametrize(("authserv_id", "asked"), [("mail", 1), ("", 0)])
    def test_host_asked_needs_our_own_authserv_id(self, wire, authserv_id, asked):
        """The host's addressed, passing mail is `host_asked`, the one release
        from the outbound hold, only with `authserv_id` set: without it the
        verdict is read off the topmost header, which the sender writes
        (`inbound.poll_emails`, ISSUE-607). The lean stack always sets the id,
        so this is the one place the condition can be seen to matter; under
        the wire's `confirm_sender_match = off` the unscoped pass still runs
        the task."""
        wire.config.email.authserv_id = authserv_id
        nonce = new_nonce()
        mid = _send(
            wire, USER_ADDRESS, to=[USER_TAG_ADDRESS, f"s-{nonce}@stranger.test"],
            subject=f"Quote {nonce}", body="Please send the quote.", verdict="pass",
        )

        created = wire.poll()

        row = wire.probe.processed(mid)
        assert created == [row["task_id"]]
        assert wire.probe.email_room(mid) is not None
        assert row["host_asked"] == asked


class TestACorrespondentsMail:
    """`thread_addressed` for anyone else: named, or the host not on it."""

    def test_named_by_a_stranger_on_the_thread_is_asked(self, wire, thread):
        mid = thread.reply(
            wire, thread.stranger, to=[USER_ADDRESS], cc=[mail.BOT_ADDRESS],
            body=_named(wire, "does seven work for you too?"),
        )

        created = wire.poll()

        row = wire.probe.processed(mid)
        assert row["routing_method"] == "thread_room"
        assert row["user_id"] == USER_ID
        assert row["thread_id"] == thread.room
        assert created == [row["task_id"]]
        task = _task(wire, row["task_id"])
        assert task["status"] == "pending", "a stranger on the thread is not held"
        assert task["host_absent"] == 0
        assert _present(wire, thread.room) == {
            (USER_ADDRESS, "principal", USER_ID),
            (thread.stranger, "guest", None),
        }

    def test_to_the_bot_alone_is_asked_with_the_host_absent(self, wire, thread):
        mid = thread.reply(
            wire, thread.stranger, to=[mail.BOT_ADDRESS],
            body="Is the table still booked for seven?",
        )

        created = wire.poll()

        row = wire.probe.processed(mid)
        assert row["routing_method"] == "thread_room"
        assert row["thread_id"] == thread.room
        assert created == [row["task_id"]]
        task = _task(wire, row["task_id"])
        assert task["user_id"] == USER_ID
        assert task["host_absent"] == 1
        assert _present(wire, thread.room) == {
            (USER_ADDRESS, "principal", USER_ID),
            (thread.stranger, "guest", None),
        }

    def test_a_reply_all_with_the_host_on_it_is_recorded_only(self, wire, thread):
        """The bot in To says nothing for a correspondent: every reply-all on
        a thread the bot is on has it there."""
        mid = thread.reply(
            wire, thread.stranger, to=[mail.BOT_ADDRESS], cc=[USER_ADDRESS],
            body="Seven works, see you there.",
        )

        created = wire.poll()

        row = wire.probe.processed(mid)
        assert row["routing_method"] == "thread_room"
        assert row["thread_id"] == thread.room
        assert row["task_id"] is None
        assert created == []
        recorded = _rows_for_mail(wire, mid)
        assert [(r["room_token"], r["task_id"]) for r in recorded] == [
            (thread.room, None),
        ]
        # Decided by the email room's own rule, on a deployment whose default
        # is the classifier: the room is kept on `mention` and no model is asked.
        assert gate_rung(wire.probe, recorded[0]["id"]) == ["mode_mention"]


class TestTheThreadsPeople:

    def test_a_newcomer_is_a_participant_and_splits_the_epoch(self, wire, thread):
        newcomer = f"n-{thread.nonce}@stranger.test"
        assert _split_epochs(wire, thread.room) == [], "the opener's people are the baseline"

        mid = thread.reply(
            wire, thread.stranger, to=[mail.BOT_ADDRESS, USER_ADDRESS],
            cc=[newcomer], body="Adding my colleague.",
        )
        wire.poll()

        assert wire.probe.processed(mid)["thread_id"] == thread.room
        assert (newcomer, "guest", None) in _present(wire, thread.room)
        assert _split_epochs(wire, thread.room) == [
            {"epoch": 1, "reason": "email_join", "person": f"email:{newcomer}"},
        ]

    def test_the_people_are_the_union_of_admitted_mail_only(self, wire, thread):
        """From, To and Cc across every admitted mail. The bot's addresses
        (the plus-address and the bare one were both on the thread), a Bcc
        recipient, and the people on a held mail are none of them.

        The Bcc half pins the wire shape rather than a product filter: a Bcc
        is an envelope recipient only, so the bot's copy carries no header
        naming it, and the check below reads that copy to show it."""
        nonce = thread.nonce
        copied = f"c-{nonce}@stranger.test"
        blind = f"b-{nonce}@stranger.test"
        outsider = f"x-{nonce}@stranger.test"
        outsiders_cc = f"y-{nonce}@stranger.test"

        admitted = thread.reply(
            wire, thread.stranger, to=[mail.BOT_ADDRESS], cc=[USER_ADDRESS, copied],
            bcc=[blind], body="Copying a friend.",
        )
        held = thread.reply(
            wire, outsider, to=[mail.BOT_ADDRESS], cc=[outsiders_cc],
            body=_named(wire, "can I join?"),
        )
        wire.poll()

        assert wire.probe.processed(admitted)["thread_id"] == thread.room
        held_row = wire.probe.processed(held)
        assert held_row["thread_id"] is None
        assert _task(wire, held_row["task_id"])["status"] == "pending_confirmation"
        assert _present(wire, thread.room) == {
            (USER_ADDRESS, "principal", USER_ID),
            (thread.stranger, "guest", None),
            (copied, "guest", None),
        }
        everyone = {row["surface_ref"] for row in wire.probe.participants(thread.room)}
        assert not any(address.endswith("@bot.test") for address in everyone)
        assert blind not in everyone
        with wire.inbox() as session:
            raw = [m for m in session.fetch_new_since(0) if m.message_id == admitted]
        assert len(raw) == 1
        assert blind not in str(raw[0].headers)

    def test_a_bcc_reaches_the_envelope_and_no_header(self, wire):
        """What makes the Bcc half above mean anything: `mail.send` puts a Bcc
        on the envelope (the only non-bot recipient, so the catch-all copy is
        its), and the bot's copy names it nowhere."""
        blind = f"b-{new_nonce()}@stranger.test"
        mid = _send(
            wire, f"s-{new_nonce()}@stranger.test", to=[mail.BOT_ADDRESS],
            bcc=[blind], subject="blind", body="hello",
        )
        with wire.outbox() as session:
            assert [m.message_id for m in session.fetch_new_since(0)] == [mid]
        with wire.inbox() as session:
            (copy,) = session.fetch_new_since(0)
        assert blind not in str(copy.headers)

    def test_another_istota_user_is_a_correspondent_not_a_member(self, wire):
        """alice is on testuser's thread as a guest with her user id, and the
        thread's only member is its host (ISSUE-606). Her reply speaks as the
        host, since `speaking_user` needs membership."""
        wire.config.users[ALICE_ID] = UserConfig(email_addresses=[ALICE_ADDRESS])
        nonce = new_nonce()
        subject = f"Book club {nonce}"
        root = _send(
            wire, USER_ADDRESS, to=[USER_TAG_ADDRESS, ALICE_ADDRESS],
            subject=subject, body=_named(wire, "pick a date for us?"), verdict="pass",
        )
        wire.poll()
        room = wire.probe.email_room(root)
        assert room is not None

        reply = _send(
            wire, ALICE_ADDRESS, to=[mail.BOT_ADDRESS], cc=[USER_ADDRESS],
            subject=f"Re: {subject}", body=_named(wire, "Thursday suits me."),
            references=root, in_reply_to=root,
        )
        wire.poll()

        assert _present(wire, room) == {
            (USER_ADDRESS, "principal", USER_ID),
            (ALICE_ADDRESS, "guest", ALICE_ID),
        }
        assert _members(wire, room) == [USER_ID]
        row = wire.probe.processed(reply)
        assert row["routing_method"] == "thread_room"
        assert row["user_id"] == USER_ID
        assert _task(wire, row["task_id"])["user_id"] == USER_ID


class TestMinting:

    def test_a_root_shaped_like_a_private_email_ref_mints_nothing(self, wire, caplog):
        """A sender chooses the root. One spelled like the host's private
        email room's ref would otherwise bind their own room as a thread with
        a stranger on it (`threads._mint`)."""
        nonce = new_nonce()
        private_ref = email_conversation_token(USER_ID)
        with caplog.at_level(logging.WARNING, logger="istota.transport.email.threads"):
            mid = _send(
                wire, USER_ADDRESS, to=[USER_TAG_ADDRESS, f"s-{nonce}@stranger.test"],
                subject=f"Shaped {nonce}", body=_named(wire, "hello?"),
                references=private_ref, verdict="pass",
            )
            wire.poll()

        row = wire.probe.processed(mid)
        assert row["routing_method"] == "plus_address"
        assert row["thread_id"] is None
        assert wire.probe.email_room(mid) is None
        assert wire.probe.query(
            "SELECT room_token FROM room_bindings WHERE surface = 'email' "
            "AND surface_ref = ?", [private_ref],
        ) == []
        assert wire.probe.query("SELECT token FROM rooms WHERE origin = 'email'") == []
        assert any("Refusing a thread root" in r.message for r in caplog.records)

    def test_approving_a_reply_on_a_pre_rework_thread_mints_its_room(self, wire):
        """In-Reply-To survives a hold (`f927eae1`), on a thread the bot sent
        before rooms were minted at send: a `sent_emails` row and no room.

        A stranger the bot never wrote to replies naming the parent in
        In-Reply-To alone, and is held. Approval rebuilds the mail from
        `processed_emails`, which has to carry In-Reply-To for `match_thread`
        to find the sent mail; without it the approved reply mints nothing.
        Lives here rather than on the lean stack because the precondition is a
        database row the current product never writes.
        """
        nonce = new_nonce()
        parent = f"<pre-{nonce}@bot.test>"
        with db.get_db(wire.config.db_path) as conn:
            db.record_sent_email(
                conn, user_id=USER_ID, message_id=parent,
                to_addr=f"known-{nonce}@stranger.test", subject=f"Quote {nonce}",
            )
        outsider = f"x-{nonce}@stranger.test"
        mid = _send(
            wire, outsider, to=[mail.BOT_ADDRESS], subject=f"Re: Quote {nonce}",
            body="Forwarded to me, can you send the quote again?",
            in_reply_to=parent,
        )

        wire.poll()

        row = wire.probe.processed(mid)
        assert row["routing_method"] == "thread_room"
        assert row["user_id"] == USER_ID
        assert row["in_reply_to"] == parent
        assert row["thread_id"] is None
        assert wire.probe.query("SELECT token FROM rooms WHERE origin = 'email'") == []
        held = _task(wire, row["task_id"])
        assert held["status"] == "pending_confirmation"

        with db.get_db(wire.config.db_path) as conn:
            confirmations.approve(
                conn, db.get_task(conn, held["id"]), config=wire.config,
            )

        released = _task(wire, held["id"])
        assert released["status"] == "pending"
        room = wire.probe.email_room(parent)
        assert room is not None, "approval found no thread: In-Reply-To was lost"
        assert released["conversation_token"] == room
        assert released["host_absent"] == 1
        assert wire.probe.email_room(mid) == room
        assert (outsider, "guest", None) in _present(wire, room)
        first = wire.probe.room_messages(room)[0]
        assert first["role"] == "assistant"
        assert first["outgoing_mail"]["to"] == [f"known-{nonce}@stranger.test"]


class TestTheMailCard:
    """`processed_emails.mail_meta` for every mail the poller files, and the
    same card as `messages.received_mail` once the mail is admitted.

    `verify`, so the stamp is what the gate decides on and `sender_check` can
    be held to that decision: a pass runs, a fail is held.
    """

    @pytest.fixture(autouse=True)
    def verify(self, wire):
        wire.config.email.confirm_sender_match = "verify"

    def _card(self, wire, message_id: str) -> dict:
        row = wire.probe.processed(message_id)
        assert row is not None and row["mail_meta"], row
        return row["mail_meta"]

    def test_an_admitted_mail_carries_the_card_into_its_room(self, wire):
        nonce = new_nonce()
        stranger = f"s-{nonce}@stranger.test"
        copied = f"c-{nonce}@stranger.test"
        blind = f"b-{nonce}@stranger.test"
        mid = _send(
            wire, USER_ADDRESS, to=[USER_TAG_ADDRESS, stranger], cc=[copied],
            bcc=[blind], subject=f"Card {nonce}", body=_named(wire, "book it?"),
            verdict="pass",
        )

        wire.poll()

        card = self._card(wire, mid)
        assert [p["address"] for p in card["to"]] == [USER_TAG_ADDRESS, stranger]
        assert [p["address"] for p in card["cc"]] == [copied]
        assert blind not in json.dumps(card)
        assert card["sender_check"] == "verified"
        assert card["trusted"] is False
        assert card["message_id"] == mid
        task = _task(wire, wire.probe.processed(mid)["task_id"])
        assert task["status"] == "pending", "a verified self-claim runs"
        room = wire.probe.email_room(mid)
        assert [(r["room_token"], r["received_mail"]) for r in _rows_for_mail(wire, mid)] == [
            (room, card),
        ]

    def test_a_recorded_only_mail_carries_the_card_too(self, wire, thread):
        mid = thread.reply(
            wire, thread.stranger, to=[mail.BOT_ADDRESS], cc=[USER_ADDRESS],
            body="Noted, thanks.",
        )

        wire.poll()

        card = self._card(wire, mid)
        assert wire.probe.processed(mid)["task_id"] is None
        assert [p["address"] for p in card["to"]] == [mail.BOT_ADDRESS]
        assert [p["address"] for p in card["cc"]] == [USER_ADDRESS]
        assert card["sender_check"] == "none"
        assert [(r["room_token"], r["received_mail"]) for r in _rows_for_mail(wire, mid)] == [
            (thread.room, card),
        ]

    @pytest.mark.parametrize(
        ("who", "verdict", "check"),
        [("host", "fail", "failed"), ("stranger", None, "none")],
    )
    def test_a_held_mail_has_a_card_and_no_row_until_approved(
        self, wire, who, verdict, check,
    ):
        nonce = new_nonce()
        stranger = f"s-{nonce}@stranger.test"
        sender = USER_ADDRESS if who == "host" else stranger
        to = [USER_TAG_ADDRESS, stranger] if who == "host" else [USER_TAG_ADDRESS]
        mid = _send(
            wire, sender, to=to, subject=f"Held {nonce}",
            body=_named(wire, "please look at this"), verdict=verdict,
        )

        wire.poll()

        card = self._card(wire, mid)
        assert card["sender_check"] == check
        assert [p["address"] for p in card["to"]] == to
        assert card["cc"] == []
        task_id = wire.probe.processed(mid)["task_id"]
        assert _task(wire, task_id)["status"] == "pending_confirmation"
        assert _rows_for_mail(wire, mid) == [], "a held mail is in no transcript"
        assert wire.probe.email_room(mid) is None

        with db.get_db(wire.config.db_path) as conn:
            confirmations.approve(conn, db.get_task(conn, task_id), config=wire.config)

        room = wire.probe.email_room(mid)
        assert room is not None
        assert [(r["room_token"], r["received_mail"]) for r in _rows_for_mail(wire, mid)] == [
            (room, card),
        ]


class TestTheVeto:
    """`!<bot> off|on` as a mail's first line on a thread (multiplayer D8)."""

    def _command(self, wire, word: str) -> str:
        return f"!{wire.config.bot_name.lower()} {word}"

    def _poll_one(self, wire) -> dict:
        """Poll, and the one ledger row it filed. A veto's row carries neither
        Message-ID nor subject, so it is found as the row the poll added."""
        filed = len(wire.processed())
        assert wire.poll() == []
        rows = wire.processed()
        assert len(rows) == filed + 1, [r["routing_method"] for r in rows[filed:]]
        assert rows[-1]["mail_meta"] is None, "a veto's ledger row keeps nothing of it"
        return rows[-1]

    def test_off_records_nothing_and_on_needs_a_passing_stamp(self, wire, thread):
        before = wire.probe.watermark()
        people = _present(wire, thread.room)

        thread.reply(
            wire, thread.stranger, to=[mail.BOT_ADDRESS], cc=[USER_ADDRESS],
            body=self._command(wire, "off"),
        )
        assert self._poll_one(wire)["routing_method"] == "room_veto"
        assert self._is_off(wire, thread.room)
        # The switch-off notice is the one row the veto writes.
        notices = wire.probe.room_messages(thread.room, id_above=before["messages"])
        assert [(r["role"], r["received_mail"]) for r in notices] == [("system", None)]
        after_off = wire.probe.watermark()

        thread.reply(
            wire, thread.stranger, to=[mail.BOT_ADDRESS],
            body=_named(wire, "are you there?"),
        )
        assert self._poll_one(wire)["routing_method"] == "room_off"

        thread.reply(
            wire, thread.stranger, to=[mail.BOT_ADDRESS], body=self._command(wire, "on"),
        )
        assert self._poll_one(wire)["routing_method"] == "room_off"
        assert self._agreed(wire, thread.room, thread.stranger) is None

        thread.reply(
            wire, thread.stranger, to=[mail.BOT_ADDRESS], body=self._command(wire, "on"),
            verdict="pass",
        )
        assert self._poll_one(wire)["routing_method"] == "room_veto"
        assert self._agreed(wire, thread.room, thread.stranger) is not None
        assert self._is_off(wire, thread.room)

        # Not even the host's authenticated `on` by mail switches it back:
        # email calls `apply(..., authenticated=False)`, so it is never a
        # member's ask, and a member does that from the web view.
        thread.reply(
            wire, USER_ADDRESS, to=[mail.BOT_ADDRESS], body=self._command(wire, "on"),
            verdict="pass",
        )
        assert self._poll_one(wire)["routing_method"] == "room_veto"
        assert self._is_off(wire, thread.room)

        assert [
            r for r in wire.probe.room_messages(thread.room, id_above=after_off["messages"])
            if r["role"] != "system"
        ] == [], "nothing but veto notices is written while the room is off"
        assert wire.probe.rows_above("tasks", before, user_id=USER_ID) == []
        assert _present(wire, thread.room) == people

    def _is_off(self, wire, room: str) -> bool:
        rows = wire.probe.query(
            "SELECT vetoed_at FROM room_policy WHERE room_token = ?", [room],
        )
        return bool(rows) and rows[0]["vetoed_at"] is not None

    def _agreed(self, wire, room: str, address: str):
        rows = wire.probe.query(
            "SELECT agreed_at FROM room_vetoes WHERE room_token = ? AND person = ?",
            [room, f"email:{address}"],
        )
        assert len(rows) == 1, rows
        return rows[0]["agreed_at"]
