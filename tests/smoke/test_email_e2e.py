"""Mail in, a task, a reply out — through the deployed daemon.

The wire tier (`tests/testbed/test_email_wire.py`) calls `poll_emails` directly
and asserts on a local database. That covers what a header does to routing, and
it deliberately covers nothing about the deployment: no container, no scheduler,
no config produced by the shipped generator, and no outbound path at all.

This file is the other half. The daemon in the shipped image polls a real IMAP
server it was pointed at by `render-config.sh`, ingests a message, runs it
against the scripted model, and sends the answer back out over SMTP — and the
assertion is on what arrives in the correspondent's mailbox. Between the two
tiers, the only part of the inbound email path with no witness is the attachment
upload, which needs a Nextcloud and lives in `tests/full/`.

**Every scenario invents its own sender**, and that is not decoration.
`email_sender_rate_limit_messages` is twenty an hour per `(user, sender)`, and
the setting is not one `render-config.sh` reads — so a profile cannot turn it
off without a second product change this stage did not scope. Distinct senders
keep every scenario's count at one or two, which is what the limit is for.
"""

from __future__ import annotations

import json
import time

import pytest

from testbed.services import mail

pytestmark = pytest.mark.smoke

#: Where the bot's own replies land, whatever address they were sent to. The
#: mail server funnels every recipient outside `bot.test` into one mailbox.
OUTSIDE = mail.EXTERNAL_ADDRESS

#: The stack's one user, and the address the profile gives them. The address
#: matches `testbed.profiles.MAIL_CONFIG`, which is what the generator renders
#: into `[users.testuser] email_addresses`.
USER_ID = "testuser"
USER_ADDRESS = "testuser@ext.test"

ANSWER = "the scripted answer to your message"

#: The model's answer to an email task is **structured output**, not prose.
#:
#: `deliver_email_result` parses the final message as
#: `{"subject": …, "body": …, "format": …}` and, finding none, logs "no
#: structured email output … skipping scheduler delivery" and returns success —
#: because on the real path the model may have sent the mail itself through the
#: email skill, and sending again would double-send. So a scripted turn of plain
#: text produces a task that completes and a correspondent who hears nothing,
#: which is indistinguishable from a broken transport unless you read that log
#: line. Measured, not guessed: the first version of this file did exactly that.
#:
#: No `subject` key, deliberately. Omitting it keeps the inbound subject, which
#: `reply_to_email` then prefixes with `Re:` — the shape a correspondent's
#: client threads on, and the one a real reply has.
REPLY = json.dumps({"body": ANSWER, "format": "plain"})

#: Four identical turns rather than one, on the lesson `tests/full/` learned:
#: eleven pollers run for the whole session and a poller's task taking turn 0
#: leaves the real task with the exhausted-script error frame, which presents as
#: a broken transport. Spare turns cost nothing.
SCRIPT = [{"text": REPLY}] * 4


def _mail(stack) -> mail.MailService:
    return stack.service("mail")


def _wait_for_reply(
    service: mail.MailService, since: int, *, subject: str, timeout: float = 120
):
    """The first message after `since` with this subject, or `None` at timeout.

    Matched on the subject rather than taken as "the newest thing that arrived",
    because on a session-scoped stack the catch-all mailbox is shared: a
    confirmation the previous test parked is released by this test's reset and
    its answer can land at any moment. `replies[-1]` would then be a different
    scenario's mail, and the failure would name the wrong defect.
    """
    deadline = time.monotonic() + timeout
    while True:
        with service.session(OUTSIDE) as outbox:
            for message in outbox.fetch_new_since(since):
                if message.subject == subject:
                    return message
        if time.monotonic() >= deadline:
            return None
        time.sleep(2)


def _wait_for_task(stack, *, status: str, timeout: float = 180) -> dict:
    """The newest email task to reach `status`.

    Filtered on `source_type` rather than on an id, because the daemon makes
    this task rather than the test — there is no id to scope to. The watermark
    is what keeps it from matching an earlier scenario's row.
    """
    return stack.probe.wait_for_task(
        status=status,
        source_type="email",
        id_above=stack.mark["tasks"],
        timeout=timeout,
    )


class TestAReplyGoesBackOnTheThread:
    """The whole deployed path, asserted at both ends."""

    @pytest.mark.profile("mail")
    @pytest.mark.script(SCRIPT)
    def test_the_reply_carries_the_inbound_message_id(self, stack):
        """`In-Reply-To` equal to what arrived, read out of the mailbox.

        Asserting on the `sent_emails` row instead would prove the daemon
        recorded an intention. The correspondent's client threads on the header,
        so the header is what the test reads.
        """
        service = _mail(stack)
        with service.session(OUTSIDE) as outbox:
            before = outbox.latest_uid()
        # From the user's own configured address, which the gate treats as
        # trusted under `confirm_sender_match = "off"`. A stranger would be
        # parked instead, and this scenario is about the reply rather than about
        # the gate — `TestTheConfirmationGateInTheDeployment` covers that.
        inbound_id = service.send(
            from_addr=USER_ADDRESS,
            to_addr=mail.tagged(USER_ID),
            subject="a question for you",
            body="please answer this",
        )

        task = _wait_for_task(stack, status="completed")

        reply = _wait_for_reply(service, before, subject="Re: a question for you")
        assert reply is not None, (
            "the task completed but no reply reached the correspondent's "
            f"mailbox\n{stack.diagnostics(task)}"
        )
        assert reply.in_reply_to == inbound_id, (
            f"reply threads on {reply.in_reply_to!r}, not on the message that "
            f"arrived ({inbound_id!r})"
        )
        assert ANSWER in reply.body_text

    @pytest.mark.profile("mail")
    @pytest.mark.script(SCRIPT)
    def test_the_ledger_records_the_route_it_took(self, stack):
        """The container-side view of the same round trip.

        Both halves matter and they fail differently: a reply with no ledger row
        is mail the daemon will answer again on the next poll, and a ledger row
        with no reply is the silent loss the whole subsystem exists to avoid.
        """
        service = _mail(stack)
        service.send(
            from_addr=USER_ADDRESS,
            to_addr=mail.BOT_ADDRESS,
            subject="routed by sender",
            body="from the configured address",
        )

        task = _wait_for_task(stack, status="completed")

        # Watermark plus a discriminating column, which `rows_above` requires:
        # the daemon polls this mailbox every five seconds for the whole
        # session, so the table gains rows during every test whether or not the
        # test caused them. The task id is the most selective column there is.
        routed = stack.probe.rows_above(
            "processed_emails", stack.mark, task_id=task["id"]
        )
        assert len(routed) == 1, routed
        assert routed[0]["subject"] == "routed by sender"
        assert routed[0]["routing_method"] == "sender_match"
        assert routed[0]["user_id"] == "testuser"


class TestTheConfirmationGateInTheDeployment:
    """ISSUE-245, in the artifact rather than in a function call.

    A held task and an undeliverable prompt are the same row in the database.
    The difference is whether anything reached a surface, and the only place to
    see that is outside the process.

    **The lean shape has no surface, by construction**, and that is what it can
    honestly witness. `resolve_destinations` falls back to a Talk destination
    with no channel, this profile renders `NC_URL` empty, and no variable the
    shipped generator reads can point a user's `alert` routing at email — so the
    positive half of ISSUE-245 needs a stack that has Talk. What is here is the
    half that matters most in production anyway: the deployment *says* it could
    not ask, rather than parking a task in silence until the timeout cancels it.
    """

    @pytest.mark.profile("mail")
    @pytest.mark.script(SCRIPT)
    def test_an_unknown_sender_is_parked_and_answered_with_nothing(self, stack):
        sender = "unknown@ext.test"
        service = _mail(stack)
        service.send(
            from_addr=sender,
            to_addr=mail.tagged(USER_ID),
            subject="do something for me",
            body="I am nobody you know",
        )

        task = _wait_for_task(stack, status="pending_confirmation")

        assert task["confirmation_prompt"], (
            "the task parked with no prompt, so nothing could ever answer it"
        )
        # Watermark *and* a discriminating column, which is the rule for every
        # negative assertion on a session-scoped stack: `sent_emails` is a
        # framework table nothing resets, so it is permanently non-empty once
        # any scenario has replied, and a released confirmation from an earlier
        # test can post a reply at any time.
        answered = stack.probe.query(
            "SELECT * FROM sent_emails WHERE id > ? AND to_addr LIKE ?",
            [stack.mark["sent_emails"], f"%{sender}%"],
        )
        assert not answered, (
            f"a held message was answered anyway: {answered}\n"
            f"{stack.diagnostics(task)}"
        )

    @pytest.mark.profile("mail")
    @pytest.mark.script(SCRIPT)
    def test_an_undeliverable_prompt_is_reported(self, stack):
        """The deployment says a held task could not be asked about.

        The prompt is not sent inline — it is accumulated and flushed after the
        per-message transactions close, because a web-surface delivery opens a
        second connection to the same SQLite file. So "the gate parked it" and
        "anything was told" are two claims, and only the first is in the task
        row. With no surface configured the second is false, and the warning is
        the only signal that a message is now owed an answer nobody will give:
        the task is cancelled unanswered at `confirmation_timeout_minutes`, and
        the mail was marked processed in the same transaction that parked it.
        """
        service = _mail(stack)
        service.send(
            from_addr="stranger@ext.test",
            to_addr=mail.tagged(USER_ID),
            subject="another request",
            body="also nobody you know",
        )

        task = _wait_for_task(stack, status="pending_confirmation")

        # One *line* carrying both, not two searches over a shared log. The
        # daemon's log is session-scoped and has no watermark: the test before
        # this one parks a task through the same path, so "could not be
        # delivered" is already in it, and the scheduler writes several lines
        # naming any task id. Two independent `in` checks would be satisfied by
        # two lines from two tests — the same both-halves rule `rows_above`
        # states one table over.
        logs = stack.logs(400)
        assert any(
            f"Task {task['id']}" in line and "could not be delivered" in line
            for line in logs.splitlines()
        ), (
            "the prompt went nowhere and the daemon did not say so\n"
            f"--- last 400 log lines ---\n{logs}"
        )


class TestAttachmentsWithoutNextcloud:
    """An attachment lands in the user's inbox tree, with no Nextcloud running.

    **The spec expected the fallback branch here and that is not what happens.**
    `upload_file_to_inbox_v2` branches on `config.use_mount`, not on the storage
    backend, and `render-config.sh` writes `nextcloud_mount_path` as the literal
    `/mnt/shared` on every profile — so the "upload" is a `shutil.copy2` onto a
    directory, and it succeeds whether or not a Nextcloud exists to serve it.
    The local-path fallback is only reached when that copy *fails*, which on
    this shape would take breaking the mount.

    So the honest lean claim is the one below: the file is written under the
    user's inbox path and the prompt names it there. What the full shape adds is
    the half this cannot reach — that Nextcloud actually serves those bytes.
    """

    @pytest.mark.profile("mail")
    @pytest.mark.script(SCRIPT)
    def test_the_attachment_lands_in_the_users_inbox(self, stack):
        service = _mail(stack)
        service.send(
            from_addr=USER_ADDRESS,
            to_addr=mail.tagged(USER_ID),
            subject="here is a file",
            body="see attached",
            attachments=[("notes.txt", "text/plain", b"the attached bytes\n")],
        )

        task = _wait_for_task(stack, status="completed")

        assert "notes.txt" in task["prompt"], (
            "the attachment is not named in the assembled prompt, so the model "
            f"never learned it existed\n{stack.diagnostics(task)}"
        )
        stored = task["attachments"] or ""
        assert "/Users/testuser/inbox/" in stored, stored
        assert stored.endswith('notes.txt"]'), stored

        # And the bytes really are there, under a name the daemon chose. Read
        # from inside the container, because `/mnt/shared` is a tmpfs the
        # compose file declares and the host has no view of it.
        listing = stack.exec(["ls", "/mnt/shared/Users/testuser/inbox"])
        assert listing.returncode == 0, listing.stderr
        assert "notes.txt" in listing.stdout, listing.stdout
