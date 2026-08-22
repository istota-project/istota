"""Inbound email, at the wire.

Every case here puts a real message through a real SMTP submission, lets a real
IMAP server store it, and calls `poll_emails` against a local SQLite database.
Nothing is patched. That is the point: `imap_tools.MailBox` appears in the
default suite exactly once, inside a `patch`, and each of the four things this
file covers — routing precedence, thread matching, the untrusted-sender gate and
the DMARC canary — has shipped broken and been found in production, always
because of what a header actually looked like on the wire.

Three properties of the server that the cases lean on, all measured rather than
assumed:

- **An inbound `Authentication-Results` passes through verbatim.** Maddy neither
  strips one bearing its own authserv-id nor writes one of its own; it adds
  `Delivered-To`, `Return-Path` and `Received` and nothing else. So the header a
  case writes is the header istota reads, and the DMARC cases can inject by
  submission rather than by IMAP `APPEND`.
- **Every recipient at `bot.test` collapses into one mailbox**, so a plus-address
  needs no account, and every recipient outside it collapses into the catch-all,
  so a case can invent a correspondent per test.
- **Submission does not check that the authenticated account owns the `From`**,
  which is what makes a self-claiming sender reachable at all.
"""

from __future__ import annotations

import logging

import pytest

from istota import confirmations, db
from istota.config import UserConfig
from istota.email_support import compute_thread_id, normalize_subject
from istota.skills.email import (
    EmailConfig,
    _get_mailbox,
    _server_capabilities,
    _supports_uid_expunge,
    reply_to_email,
)
from testbed.services import mail

from .conftest import USER_ADDRESS, USER_ID, USER_TAG_ADDRESS

pytestmark = pytest.mark.testbed

STRANGER = "stranger@ext.test"


def _row_for(rows: list[dict], subject: str) -> dict:
    """The one processed row with this subject, or a failure naming what is there.

    Cases assert on a message they sent, and a bare `rows[0]` would silently
    read a different one the moment a case sends two.
    """
    matching = [row for row in rows if row["subject"] == subject]
    assert len(matching) == 1, (
        f"expected exactly one processed row for {subject!r}; the ledger holds "
        f"{[(row['subject'], row['routing_method']) for row in rows]}"
    )
    return matching[0]


class TestRoutingPrecedence:
    """Which rung claims a message, in the order `poll_emails` tries them."""

    def test_a_plus_address_beats_a_sender_match(self, wire):
        """Both rungs resolve, and the plus-address is the one that wins.

        The message is *from* the address configured for `testuser` and
        addressed to `bot+other@bot.test`, so rung 1 and rung 2 name different
        users. A test where only one rung matched would pass under either
        precedence.
        """
        wire.config.users["other"] = UserConfig(email_addresses=["other@ext.test"])
        wire.send(
            from_addr=USER_ADDRESS,
            to_addr="bot+other@bot.test",
            subject="precedence",
        )

        wire.poll()

        row = _row_for(wire.processed(), "precedence")
        assert row["routing_method"] == "plus_address"
        assert row["user_id"] == "other"

    def test_a_bare_address_routes_by_sender_match(self, wire):
        wire.send(from_addr=USER_ADDRESS, to_addr=mail.BOT_ADDRESS, subject="mine")

        wire.poll()

        row = _row_for(wire.processed(), "mine")
        assert row["routing_method"] == "sender_match"
        assert row["user_id"] == USER_ID

    def test_a_reply_matches_the_thread_by_in_reply_to(self, wire):
        """`In-Reply-To` alone, with no `References`, is enough.

        This is the shape `641ac5db` fixed: `match_thread` read only
        `References`, on a docstring claim that it "subsumes In-Reply-To". They
        are separate sender-written headers and one can arrive unusable while
        the other is exact — which is how a real reply on a long thread was
        filed as `discarded`.
        """
        with db.get_db(wire.config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id=USER_ID,
                message_id="<sent-1@bot.test>",
                to_addr=STRANGER,
                subject="Quarterly",
            )
        wire.send(
            from_addr=STRANGER,
            to_addr=mail.BOT_ADDRESS,
            subject="Re: Quarterly",
            in_reply_to="<sent-1@bot.test>",
        )

        wire.poll()

        row = _row_for(wire.processed(), "Re: Quarterly")
        assert row["routing_method"] == "thread_match"
        assert row["user_id"] == USER_ID

    def test_a_thread_match_recovers_the_origin_target(self, wire):
        """The reply goes back where the conversation started, not to email.

        The `sent_emails` row is the only record of where a thread came from, so
        losing it turns a Talk conversation that happens to have gone out by
        email into an email-only one.
        """
        with db.get_db(wire.config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id=USER_ID,
                message_id="<sent-2@bot.test>",
                to_addr=STRANGER,
                subject="Origin",
                conversation_token="roomtoken123",
                origin_target="room:roomtoken123",
            )
        wire.send(
            from_addr=STRANGER,
            to_addr=mail.BOT_ADDRESS,
            subject="Re: Origin",
            in_reply_to="<sent-2@bot.test>",
        )

        wire.poll()

        task = wire.tasks()[-1]
        assert task["conversation_token"] == "roomtoken123"
        assert "room:roomtoken123" in (task["output_target"] or "")

    def test_a_thread_row_belonging_to_another_user_is_dropped(self, wire):
        """Identity wins over payload.

        The message plus-addresses `testuser`, and the thread it names belongs to
        `other`. The mail still becomes `testuser`'s task — it was addressed to
        them — but it must not inherit the other user's conversation, which is
        what a sender able to guess a `Message-ID` would be reaching for.
        """
        wire.config.users["other"] = UserConfig(email_addresses=["other@ext.test"])
        with db.get_db(wire.config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="other",
                message_id="<sent-3@bot.test>",
                to_addr=STRANGER,
                subject="Theirs",
                conversation_token="othersroom",
                origin_target="room:othersroom",
            )
        wire.send(
            from_addr=STRANGER,
            to_addr=USER_TAG_ADDRESS,
            subject="Re: Theirs",
            in_reply_to="<sent-3@bot.test>",
        )

        wire.poll()

        row = _row_for(wire.processed(), "Re: Theirs")
        assert row["routing_method"] == "plus_address"
        assert row["user_id"] == USER_ID
        task = wire.tasks()[-1]
        assert task["conversation_token"] != "othersroom"
        assert "othersroom" not in (task["output_target"] or "")

    def test_unroutable_mail_is_filed_rather_than_dropped(self, wire):
        """No rung resolves: a `discarded` row, no task, and the mail stays put.

        Filed rather than deleted, because a ledger row is the only way to tell
        "we decided not to act on this" from "we never saw it".
        """
        wire.send(from_addr=STRANGER, to_addr=mail.BOT_ADDRESS, subject="nobody")

        created = wire.poll()

        row = _row_for(wire.processed(), "nobody")
        assert row["routing_method"] == "discarded"
        assert row["user_id"] is None
        assert row["task_id"] is None
        assert created == []
        with wire.inbox() as session:
            assert len(session.uids()) == 1

    def test_the_bots_own_mail_is_skipped_without_an_owner(self, wire):
        """A loop-breaker, and the one filing that records no routing method."""
        wire.send(
            from_addr=mail.BOT_ADDRESS,
            to_addr=USER_TAG_ADDRESS,
            subject="my own words",
            auth=(mail.BOT_ADDRESS, mail.MAIL_PASSWORD),
        )

        created = wire.poll()

        row = _row_for(wire.processed(), "my own words")
        assert row["routing_method"] is None
        assert row["task_id"] is None
        assert created == []


class TestRepliesThreadByMessageId:
    """`641ac5db`, at the wire.

    A reply on a long thread was filed as `discarded` although its `In-Reply-To`
    named our sent message exactly. Two independent defects: identifier headers
    were read raw, so a `References` chain that arrived as RFC 2047 encoded-words
    split into junk; and `match_thread` read `References` alone. Both are
    properties of what a sender put on the wire, which is why they are here and
    not in the default suite.
    """

    @pytest.fixture
    def sent(self, wire) -> str:
        with db.get_db(wire.config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id=USER_ID,
                message_id="<ours@bot.test>",
                to_addr=STRANGER,
                subject="Long thread",
            )
        return "<ours@bot.test>"

    def test_an_encoded_word_references_chain_still_finds_our_id(self, wire, sent):
        """`References` as encoded-words, and no `In-Reply-To` to fall back on.

        Q-encoding writes a space as `_`, so splitting the raw header on
        whitespace returns one unusable token rather than three ids. The header
        below decodes to `<a@x.test> <ours@bot.test> <b@y.test>`.
        """
        wire.send(
            from_addr=STRANGER,
            to_addr=mail.BOT_ADDRESS,
            subject="Re: Long thread",
            references=(
                "=?utf-8?Q?=3Ca=40x=2Etest=3E_=3Cours=40bot=2Etest=3E"
                "_=3Cb=40y=2Etest=3E?="
            ),
        )

        wire.poll()

        row = _row_for(wire.processed(), "Re: Long thread")
        assert row["routing_method"] == "thread_match", (
            "the References chain arrived as encoded-words and its ids were not "
            f"recovered; the ledger stored references={row['references']!r}"
        )
        assert row["user_id"] == USER_ID

    def test_an_id_folded_across_two_encoded_words_rejoins(self, wire, sent):
        """The shape that was actually reported.

        RFC 2047 discards the linear whitespace *between* adjacent
        encoded-words, so a fold inside an id has to rejoin by concatenation —
        `=?…?Q?=3Cou?= =?…?Q?rs=40bot=2Etest=3E?=` is one id, not two. Joining
        the decoded chunks with a separator, which is the obvious fix, breaks
        exactly this case.
        """
        wire.send(
            from_addr=STRANGER,
            to_addr=mail.BOT_ADDRESS,
            subject="Re: Long thread folded",
            references="=?utf-8?Q?=3Cou?= =?utf-8?Q?rs=40bot=2Etest=3E?=",
        )

        wire.poll()

        row = _row_for(wire.processed(), "Re: Long thread folded")
        assert row["routing_method"] == "thread_match"

    def test_two_ids_glued_at_a_fold_boundary_still_separate(self, wire, sent):
        """The other half of the same rule.

        A fold *at* an id boundary glues into `<a@x><ours@bot>`, which no
        whitespace split can separate. The angle brackets are the grammar, so
        both shapes come out of one rule.
        """
        wire.send(
            from_addr=STRANGER,
            to_addr=mail.BOT_ADDRESS,
            subject="Re: Long thread glued",
            references="=?utf-8?Q?=3Ca=40x=2Etest=3E?= =?utf-8?Q?=3Cours=40bot=2Etest=3E?=",
        )

        wire.poll()

        row = _row_for(wire.processed(), "Re: Long thread glued")
        assert row["routing_method"] == "thread_match"


class TestThreadingOnTheWire:
    """What the ledger records about a conversation, read back off the server."""

    def test_the_identifier_headers_are_stored_as_the_sender_wrote_them(self, wire):
        message_id = wire.send(
            from_addr=USER_ADDRESS,
            to_addr=USER_TAG_ADDRESS,
            subject="ids",
            in_reply_to="<earlier@x.test>",
            references="<first@x.test> <earlier@x.test>",
        )

        wire.poll()

        row = _row_for(wire.processed(), "ids")
        assert row["message_id"] == message_id
        assert row["references"] == "<first@x.test> <earlier@x.test>"

    def test_an_rfc_2047_subject_threads_by_its_decoded_text(self, wire):
        """The subject arrives encoded and the thread id is computed from the
        text, not from the encoded-word — so a reply written in one client and
        answered in another lands on one thread."""
        subject = "Re: café plány"
        wire.send(from_addr=USER_ADDRESS, to_addr=USER_TAG_ADDRESS, subject=subject)

        wire.poll()

        row = _row_for(wire.processed(), subject)
        assert row["thread_id"] == compute_thread_id(
            subject, [USER_ADDRESS, mail.BOT_ADDRESS]
        )
        assert normalize_subject(subject) == "café plány"

    def test_a_three_hop_thread_keeps_one_thread_id(self, wire):
        """`Re:` prefixes accumulate and the thread does not fork."""
        subjects = ["Budget", "Re: Budget", "Re: Re: Budget"]
        for subject in subjects:
            wire.send(
                from_addr=USER_ADDRESS, to_addr=USER_TAG_ADDRESS, subject=subject
            )

        wire.poll()

        rows = wire.processed()
        thread_ids = {_row_for(rows, subject)["thread_id"] for subject in subjects}
        assert len(thread_ids) == 1, thread_ids


class TestTheConfirmationGate:
    """Who gets through without asking, and what happens to everyone else."""

    def test_an_unknown_sender_parks_the_task_and_answers_nothing(self, wire):
        wire.send(from_addr=STRANGER, to_addr=USER_TAG_ADDRESS, subject="from outside")

        wire.poll()

        task = wire.tasks()[-1]
        assert task["status"] == "pending_confirmation"
        assert task["confirmation_prompt"]
        with wire.outbox() as session:
            assert session.uids() == [], "a held message must produce no reply"

    def test_an_undeliverable_prompt_says_so(self, wire, caplog):
        """ISSUE-245: a prompt nobody can read is silent mail loss.

        The user has no alerts channel and this tier has no Talk, so the
        `alert` purpose falls back to a Talk destination that cannot be
        delivered. The task is still parked — and will be cancelled unanswered
        — so the warning is the only signal that anything is owed.
        """
        with caplog.at_level(logging.WARNING, logger="istota.transport.email.inbound"):
            wire.send(from_addr=STRANGER, to_addr=USER_TAG_ADDRESS, subject="unheard")
            wire.poll()

        assert any(
            "could not be delivered" in record.message for record in caplog.records
        ), [record.message for record in caplog.records]

    def test_a_prompt_reaches_a_surface_that_can_take_it(self, wire):
        """Routed to email, the held task's prompt arrives as mail.

        The other half of the pair above, and the reason it is worth a wire
        test: the prompt is not sent inline but accumulated and flushed after
        the per-message transactions close, so "the gate parked it" and "the
        user was asked" are two different claims.
        """
        wire.config.users[USER_ID].routing = {"alert": "email"}
        wire.send(from_addr=STRANGER, to_addr=USER_TAG_ADDRESS, subject="please ask")

        wire.poll()

        task = wire.tasks()[-1]
        with wire.outbox() as session:
            delivered = session.fetch_new_since(0)
        assert len(delivered) == 1, [message.subject for message in delivered]
        assert f"!confirm {task['id']}" in delivered[0].body_text

    def test_a_trusted_sender_skips_the_gate(self, wire):
        wire.config.users[USER_ID].trusted_email_senders = ["*@ext.test"]
        wire.send(from_addr=STRANGER, to_addr=USER_TAG_ADDRESS, subject="known")

        wire.poll()

        task = wire.tasks()[-1]
        assert task["status"] == "pending"
        assert task["confirmation_prompt"] is None

    def test_a_quiet_sender_produces_no_task_at_all(self, wire):
        """Filed and left in the mailbox, never gated and never queued."""
        wire.config.users[USER_ID].quiet_email_senders = ["noise@ext.test"]
        wire.send(
            from_addr="noise@ext.test", to_addr=USER_TAG_ADDRESS, subject="rustle"
        )

        created = wire.poll()

        row = _row_for(wire.processed(), "rustle")
        assert row["routing_method"] == "quiet"
        assert row["task_id"] is None
        assert created == []

    def test_apply_answer_releases_a_parked_task_and_trusts_the_sender(self, wire):
        wire.send(from_addr=STRANGER, to_addr=USER_TAG_ADDRESS, subject="may i")
        wire.poll()
        parked = wire.tasks()[-1]

        with db.get_db(wire.config.db_path) as conn:
            task = db.get_task(conn, parked["id"])
            acknowledgement = confirmations.apply_answer(
                conn,
                task,
                confirmations.Answer(approve=True, trust_sender=True),
                config=wire.config,
            )

        assert STRANGER in acknowledgement
        released = wire.tasks()[-1]
        assert released["status"] == "pending"
        trusted = wire.probe.query("SELECT sender_email FROM trusted_email_senders")
        assert [row["sender_email"] for row in trusted] == [STRANGER]


class TestTheDmarcCanary:
    """A sender claiming to be the user, and what the receiving MTA said about it.

    Every case here writes its own `Authentication-Results`, which is legitimate
    precisely because Maddy passes an inbound one through untouched — the
    measurement that settled this stage's open question. The canary only reads a
    header whose authserv-id it was told to trust, and the wire suite's config
    names `mail`, the server's own hostname.

    Alerts are read out of the catch-all mailbox rather than out of a log,
    because the property is that the user is *told*.
    """

    @pytest.fixture(autouse=True)
    def alerts_by_email(self, wire):
        wire.config.users[USER_ID].routing = {"alert": "email"}

    def test_a_failing_verdict_raises_one_alert_per_sender_per_poll(self, wire):
        """Two messages, one alert. The dedup key is (user, sender, verdict).

        Without it a flood of forged mail is a flood of alerts, which is how a
        canary gets muted.
        """
        for index in (1, 2):
            wire.send(
                from_addr=USER_ADDRESS,
                to_addr=USER_TAG_ADDRESS,
                subject=f"self {index}",
                headers={
                    "Authentication-Results": "mail; dmarc=fail header.from=ext.test"
                },
            )

        wire.poll()

        with wire.outbox() as session:
            alerts = session.fetch_new_since(0)
        assert len(alerts) == 1, [message.subject for message in alerts]
        assert "dmarc=fail" in alerts[0].body_text

    def test_it_watches_the_sender_match_route_too(self, wire):
        """Not only the plus-address one.

        Watching `sender_match` alone would miss `From: <user>` with
        `Cc: bot+<user>@…`; watching `plus_address` alone misses the plain
        `From: <user>` to the bare bot address, which is this case.
        """
        wire.send(
            from_addr=USER_ADDRESS,
            to_addr=mail.BOT_ADDRESS,
            subject="bare route",
            headers={"Authentication-Results": "mail; dmarc=fail header.from=ext.test"},
        )

        wire.poll()

        row = _row_for(wire.processed(), "bare route")
        assert row["routing_method"] == "sender_match"
        with wire.outbox() as session:
            assert len(session.fetch_new_since(0)) == 1

    def test_a_verdict_stamped_by_anyone_else_does_not_count_as_a_pass(self, wire):
        """A forged `dmarc=pass` from an authserv-id we never named is discarded.

        Both headers are on the wire, the forged one topmost. With `authserv_id`
        set, only ours is read — so the fail stands and the alert still goes.
        This is the case the setting exists for: with it blank the topmost
        header wins, and on a mailbox with nothing upstream stamping, the
        topmost header is the sender's.
        """
        wire.send(
            from_addr=USER_ADDRESS,
            to_addr=USER_TAG_ADDRESS,
            subject="forged pass",
            headers=[
                (
                    "Authentication-Results",
                    "attacker.example; dmarc=pass header.from=ext.test",
                ),
                ("Authentication-Results", "mail; dmarc=fail header.from=ext.test"),
            ],
        )

        wire.poll()

        with wire.outbox() as session:
            alerts = session.fetch_new_since(0)
        assert len(alerts) == 1
        assert "dmarc=fail" in alerts[0].body_text

    def test_a_clean_pass_raises_nothing(self, wire):
        wire.send(
            from_addr=USER_ADDRESS,
            to_addr=USER_TAG_ADDRESS,
            subject="honest",
            headers={
                "Authentication-Results": "mail; spf=pass smtp.mailfrom=ext.test; "
                "dkim=pass; dmarc=pass header.from=ext.test"
            },
        )

        wire.poll()

        with wire.outbox() as session:
            assert session.uids() == []

    def test_mail_carrying_no_stamp_of_ours_alerts_on_its_own(self, wire):
        """`unstamped`: the MTA we named said nothing about this message.

        The case the setting is *for*. A blank `authserv_id` reads the topmost
        header and trusts that "topmost" means "ours", which stops holding the
        moment the MTA stops stamping — and the sender's own header is then the
        topmost one. Name the id and a message arriving with no stamp of yours
        raises the alarm rather than passing quietly.

        Reachable at all because Maddy writes no `Authentication-Results` of its
        own; the message really arrives with none.
        """
        wire.send(from_addr=USER_ADDRESS, to_addr=USER_TAG_ADDRESS, subject="unstamped")

        wire.poll()

        with wire.outbox() as session:
            alerts = session.fetch_new_since(0)
        assert len(alerts) == 1
        assert "no Authentication-Results header from mail" in alerts[0].body_text

    def test_an_absent_header_is_silent_when_no_authserv_id_is_named(self, wire):
        """`unevaluated`: nothing was asked, so nothing is claimed.

        The default shipped configuration. An install that has not named its
        MTA's authserv-id cannot distinguish "no verdict" from "no stamping",
        and alerting on every unstamped message there would be an alert on every
        message — which is how a canary gets switched off.
        """
        wire.config.email.authserv_id = ""
        wire.send(from_addr=USER_ADDRESS, to_addr=USER_TAG_ADDRESS, subject="quiet")

        wire.poll()

        with wire.outbox() as session:
            assert session.uids() == []


class TestOutboundReplyHeaders:
    """What `reply_to_email` puts on the wire, read back off it.

    Asserting on the returned `Message-ID` alone would prove nothing about the
    headers, and those are what a correspondent's client threads on.
    """

    def test_a_reply_carries_in_reply_to_and_an_accumulated_references(self, wire):
        email_config = EmailConfig(
            imap_host=wire.server.host,
            imap_port=wire.server.imap_starttls_port,
            imap_user=mail.BOT_ADDRESS,
            imap_password=mail.MAIL_PASSWORD,
            smtp_host=wire.server.host,
            smtp_port=wire.server.smtp_starttls_port,
        )

        new_id = reply_to_email(
            to_addr=STRANGER,
            subject="Quarterly",
            body="answered",
            config=email_config,
            from_addr=mail.BOT_ADDRESS,
            in_reply_to="<theirs@x.test>",
            references="<first@x.test> <theirs@x.test>",
        )

        with wire.outbox() as session:
            delivered = session.fetch_new_since(0)
        assert len(delivered) == 1
        reply = delivered[0]
        assert reply.message_id == new_id
        assert reply.in_reply_to == "<theirs@x.test>"
        assert reply.references == "<first@x.test> <theirs@x.test>"
        assert reply.subject == "Re: Quarterly"


class TestWireBehaviourAMockCannotReach:
    """Four properties of a real server, none of them assertable against a fake."""

    def test_a_first_poll_starts_one_batch_back_from_the_top(self, wire):
        """Sixty waiting messages, a batch of fifty, and the ten oldest are left.

        Deliberate, and the reason is worth pinning at the wire: a fresh install
        pointed at an existing mailbox that walked from UID 1 would *answer*
        every message in it — a reply to each original sender, or a confirmation
        prompt apiece. So a ledger with no rows resumes one batch below the
        newest UID rather than at the beginning, and the second poll finds
        nothing because there is nothing left above the cursor.

        The arithmetic only holds against a server that assigns UIDs the way RFC
        3501 says. A fake mailbox can be written to agree with whatever the code
        does; that is what makes this case worth its thirty seconds.
        """
        # The per-user cap defaults to sixty an hour, which this backlog sits
        # exactly on. Off, because the case is about the batch, and a `throttled`
        # row at message sixty would read as a lost message.
        wire.config.scheduler.email_rate_limit_messages = 0
        for index in range(60):
            wire.send(from_addr=USER_ADDRESS, subject=f"backlog {index:02d}")

        first = wire.poll()
        second = wire.poll()

        assert len(first) == 50
        assert second == [], "the first poll drained everything above the cursor"
        subjects = {row["subject"] for row in wire.processed()}
        assert subjects == {f"backlog {index:02d}" for index in range(10, 60)}

    def test_the_cursor_then_carries_a_later_batch(self, wire):
        """Once the ledger exists, every later message is picked up.

        The other half of the case above, and the one that would catch a cursor
        that never advanced: a poll that drained a batch and left the cursor
        where it was would re-walk the same messages forever, and the ledger
        would make that look like "nothing to do".
        """
        wire.config.scheduler.email_rate_limit_messages = 0
        for index in range(3):
            wire.send(from_addr=USER_ADDRESS, subject=f"first wave {index}")
        assert len(wire.poll()) == 3

        for index in range(4):
            wire.send(from_addr=USER_ADDRESS, subject=f"second wave {index}")

        assert len(wire.poll()) == 4
        assert wire.poll() == []

    def test_uid_expunge_is_negotiated_against_what_the_server_advertises(self, wire):
        """The greeting cache is not the capability list, and this proves it.

        imaplib caches the pre-auth greeting's capabilities and never refreshes
        them, so a client that trusted the cache would read a *different* set
        than the server offers an authenticated session. Maddy is a real example
        of both halves: the post-auth set is strictly larger, and it still does
        not include `UIDPLUS` — so the delete path correctly falls back to a
        folder-wide `EXPUNGE`.
        """
        email_config = EmailConfig(
            imap_host=wire.server.host,
            imap_port=wire.server.imap_starttls_port,
            imap_user=mail.BOT_ADDRESS,
            imap_password=mail.MAIL_PASSWORD,
            smtp_host=wire.server.host,
            smtp_port=wire.server.smtp_starttls_port,
        )
        with _get_mailbox(email_config) as mailbox:
            mailbox.login(email_config.imap_user, email_config.imap_password)
            mailbox.folder.set("INBOX")
            greeting = {
                capability.upper() for capability in mailbox.client.capabilities
            }
            live = {capability.upper() for capability in _server_capabilities(mailbox)}
            supported = _supports_uid_expunge(mailbox, email_config)

        assert live > greeting, (
            "the post-auth capability list should be strictly larger than the "
            f"greeting cache; greeting={sorted(greeting)} live={sorted(live)}"
        )
        assert supported is False
        assert "UIDPLUS" not in live

    def test_a_message_with_no_text_plain_part_still_becomes_a_task(self, wire):
        """HTML-only mail is real and common. The body reaches the prompt as the
        raw markup rather than as nothing, which is worth pinning: an empty
        prompt would run the model against a message it cannot see."""
        wire.send(
            from_addr=USER_ADDRESS,
            to_addr=USER_TAG_ADDRESS,
            subject="html only",
            body="<p>the whole message</p>",
            body_subtype="html",
        )

        created = wire.poll()

        assert len(created) == 1
        task = wire.tasks()[-1]
        assert "the whole message" in task["prompt"]

    @pytest.mark.parametrize("cte", ["quoted-printable", "8bit"])
    def test_a_non_ascii_body_arrives_decoded(self, wire, cte):
        """Two transfer encodings, one assertion.

        Nothing in this repo decodes a body — `imap_tools` does, and until now
        nothing had ever handed it bytes off a wire. Quoted-printable is what a
        mostly-ASCII message with a few accents gets; 8-bit is what a server
        advertising `8BITMIME` accepts unencoded.
        """
        body = "příloha: café na náměstí"
        wire.send(
            from_addr=USER_ADDRESS,
            to_addr=USER_TAG_ADDRESS,
            subject=f"encoded {cte}",
            body=body,
            cte=cte,
        )

        wire.poll()

        task = wire.tasks()[-1]
        assert body in task["prompt"]


class TestTheRateLimitIsRealRatherThanConfigured:
    """The one case that turns the per-sender budget back on.

    Every other case in this file switches it off, because twenty messages an
    hour from one address is under what the suite sends and the twenty-first
    would be filed `throttled` — producing no task, which reads exactly like a
    routing defect. So the limiter needs one case of its own, or the suite's
    own workaround would be the only evidence it exists.
    """

    def test_over_budget_mail_is_filed_and_left_in_the_mailbox(self, wire):
        wire.config.scheduler.email_sender_rate_limit_messages = 2
        for index in range(3):
            wire.send(from_addr=USER_ADDRESS, subject=f"loud {index}")

        created = wire.poll()

        assert len(created) == 2
        throttled = [
            row for row in wire.processed() if row["routing_method"] == "throttled"
        ]
        assert [row["subject"] for row in throttled] == ["loud 2"]
        assert throttled[0]["user_id"] == USER_ID
        assert throttled[0]["task_id"] is None
        with wire.inbox() as session:
            assert len(session.uids()) == 3, "throttled mail is filed, never deleted"
