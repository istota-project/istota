"""A connected mail account, read and sent from through the deployed daemon.

The default suite proves each piece of a connected account in isolation: the
store, the private credential channel, the skill's account resolution, the
verified connection. Nothing there runs them together, and the seam that
matters is between them: the skill proxy spawning the email CLI with
`ISTOTA_CRED_FD`, the CLI asking the daemon for one account's password, and
`mail.connect` reaching a real IMAP and SMTP server with it.

The mail server has a third mailbox, `me@personal.test`, which nothing polls.
The account is created the way an operator would, through the shipped
`istota mail-account` CLI with the password on stdin, and the scenario puts one
message in that mailbox and a different one in the bot's.

**Both halves of the read are required.** A build that ignored `--account`
would pass "`--account personal` returns the personal message" if the bot box
happened to hold it too, and would pass "a bare list returns the bot box's
message" always. Only the pair, each with its negative, says the flag chose
the mailbox. Recorded against such a build (`_account_arg` returning `None`):
both cases go red. The read case fails on the personal half, whose list came
back from the bot box (`account` is `bot`, holding the bot box's message), and
the send case on its envelope, which names no account because the mail went
out through the bot box.

The send goes to the stack user's own address rather than to the bot box, so
the outbound gate sends rather than holds under the default `untrusted` floor,
and so the message lands in the catch-all, which nothing polls. A send through
the bot box would carry the bot's address; one through the account carries the
account's, and its sent copy lands in the account's own `Sent` folder, which
only the account's IMAP login can write.
"""

from __future__ import annotations

import json
import time
from email.utils import parseaddr

import pytest

from testbed.services import mail
from testbed.stack import CONTAINER_CONFIG, StackError

from ..support import email_flow

pytestmark = pytest.mark.smoke

USER_ID = "testuser"
#: `testbed.profiles.MAIL_CONFIG` gives the user this address, so a send to it
#: clears the outbound gate as the user's own.
USER_ADDRESS = "testuser@ext.test"
SLUG = "personal"
SENT_FOLDER = "Sent"

#: Mail the poller ingests from a stranger is parked rather than run, so the
#: bot-box message takes no turn of this file's script.
PROFILE = pytest.mark.profile("mail")


def _istota(stack, *argv: str, stdin: str | None = None) -> str:
    """The shipped `istota` CLI in the daemon container, optionally fed stdin.

    `Stack.exec` has no stdin, so a password goes through `printf` inside the
    container's shell, as an operator would pipe it.
    """
    command = (
        f'printf %s "$STDIN_VALUE" | uv run istota -c {CONTAINER_CONFIG} "$@"'
    )
    result = stack.exec(
        ["env", f"STDIN_VALUE={stdin or ''}", "sh", "-c", command, "istota", *argv],
        timeout=120,
    )
    if result.returncode != 0:
        raise StackError(
            f"`istota {' '.join(argv)}` exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    return result.stdout


def _connect(stack, *, send: bool) -> None:
    """Connect the personal mailbox as the user's account, in the wanted state.

    Every call states the whole account, so the two cases do not depend on
    the order they run in on a session-scoped stack.
    """
    fields = [
        "--label", "Personal",
        "--imap-host", mail.SERVICE_NAME,
        "--imap-port", str(mail.IMAP_TLS_PORT),
        "--imap-security", "tls",
        "--imap-username", mail.PERSONAL_ADDRESS,
        "--default-folder", "INBOX",
        "--enabled",
    ]
    if send:
        fields += [
            "--permission", "read_send",
            "--smtp-host", mail.SERVICE_NAME,
            "--smtp-port", str(mail.SMTP_TLS_PORT),
            "--smtp-security", "tls",
            "--smtp-same-login",
            "--from-address", mail.PERSONAL_ADDRESS,
            "--from-name", "Personal",
            "--sent-folder", SENT_FOLDER,
        ]
    else:
        fields += ["--permission", "read", "--no-smtp"]
    _istota(stack, "mail-account", "ensure", USER_ID, SLUG, *fields)
    _istota(stack, "mail-account", "set-password", USER_ID, SLUG, "--imap",
            stdin=mail.MAIL_PASSWORD)


def _deliver(service: mail.MailService, *, to: str, mailbox: str,
             subject: str, sender: str) -> None:
    """Send one message and wait until it is readable in `mailbox`."""
    with service.session(mailbox) as box:
        before = box.latest_uid()
    service.send(from_addr=sender, to_addr=to, subject=subject,
                 body=f"the body of {subject}")
    with service.session(mailbox) as box:
        arrived = box.wait_for_new(before, timeout=30)
    assert any(m.subject == subject for m in arrived), (
        f"{subject!r} never reached {mailbox}: {[m.subject for m in arrived]}"
    )


def _bash(call_id: str, command: str) -> dict:
    return {"tool_calls": [{"id": call_id, "name": "Bash",
                            "arguments": {"command": command}}]}


def _envelope(stack, call_id: str) -> dict:
    """The JSON envelope one scripted call printed, or a readable failure."""
    output = stack.endpoint.tool_results_by_id().get(call_id)
    if output is None:
        raise AssertionError(
            f"call {call_id} never returned to the model\n{stack.tool_output()}"
        )
    start = output.find("{")
    try:
        envelope, _ = json.JSONDecoder().raw_decode(output[start:])
    except (ValueError, IndexError):
        raise AssertionError(f"call {call_id} printed no JSON envelope:\n{output}")
    return envelope


def _subjects(envelope: dict) -> set[str]:
    return {item.get("subject", "") for item in envelope.get("emails") or []}


def _run(stack, prompt: str) -> dict:
    task_id = stack.submit(prompt, user_id=USER_ID)
    return stack.probe.wait_for_task(status="completed", task_id=task_id,
                                     timeout=180)


@PROFILE
class TestReadingAConnectedAccount:
    def test_account_reads_its_own_mailbox_and_bare_reads_the_bot_box(self, stack):
        nonce = email_flow.new_nonce()
        personal_subject = f"personal mailbox {nonce}"
        bot_subject = f"bot mailbox {nonce}"
        service = stack.service("mail")

        _connect(stack, send=False)
        _deliver(service, to=mail.PERSONAL_ADDRESS, mailbox=mail.PERSONAL_ADDRESS,
                 subject=personal_subject, sender=f"friend-{nonce}@ext.test")
        # Tagged for the user, from a stranger: the poller parks it for
        # testuser, so the bot box's `all` scope still shows it to them.
        _deliver(service, to=mail.tagged(USER_ID), mailbox=mail.BOT_ADDRESS,
                 subject=bot_subject, sender=f"stranger-{nonce}@ext.test")

        account_call = f"call-account-{nonce}"
        bare_call = f"call-bare-{nonce}"
        stack.script([
            _bash(account_call,
                  f"istota-skill email list --account {SLUG} --limit 50"),
            _bash(bare_call, "istota-skill email list --limit 50"),
            {"text": "I read both mailboxes"},
        ])
        task = _run(stack, "check my email in both mailboxes")

        account = _envelope(stack, account_call)
        assert account.get("status") == "ok", (
            f"{account}\n{stack.diagnostics(task)}"
        )
        assert account.get("account") == SLUG, account
        assert personal_subject in _subjects(account), (
            f"--account {SLUG} did not return the personal mailbox's message: "
            f"{sorted(_subjects(account))}"
        )
        assert bot_subject not in _subjects(account), (
            f"--account {SLUG} returned the bot box's message, so the flag did "
            f"not choose the mailbox: {sorted(_subjects(account))}"
        )

        bare = _envelope(stack, bare_call)
        assert bare.get("status") == "ok", bare
        assert bare.get("account") == "bot", bare
        assert bot_subject in _subjects(bare), sorted(_subjects(bare))
        assert personal_subject not in _subjects(bare), (
            f"a bare list returned the personal mailbox's message: "
            f"{sorted(_subjects(bare))}"
        )


@PROFILE
class TestSendingFromAConnectedAccount:
    def test_a_send_goes_out_from_the_account_and_records_no_thread(self, stack):
        nonce = email_flow.new_nonce()
        subject = f"from my own address {nonce}"
        service = stack.service("mail")

        _connect(stack, send=True)
        with service.session(mail.EXTERNAL_ADDRESS) as outside:
            outside_before = outside.latest_uid()
        with service.session(mail.PERSONAL_ADDRESS) as personal:
            sent_before = _latest_uid_or_zero(personal, SENT_FOLDER)

        call = f"call-send-{nonce}"
        stack.script([
            _bash(call, (
                f"istota-skill email send --account {SLUG} --to {USER_ADDRESS} "
                f"--subject '{subject}' --body 'sent through my own server {nonce}'"
            )),
            {"text": "sent it"},
        ])
        task = _run(stack, "send an email from my personal mail account")

        envelope = _envelope(stack, call)
        # `ok` is a direct send; a held one would answer `held`.
        assert envelope.get("status") == "ok", (
            f"{envelope}\n{stack.diagnostics(task)}"
        )
        assert envelope.get("account") == SLUG, envelope

        arrived = _wait_for_subject(service, mail.EXTERNAL_ADDRESS,
                                    outside_before, subject)
        assert arrived is not None, (
            f"the send never reached {USER_ADDRESS}\n{stack.diagnostics(task)}"
        )
        sender = parseaddr(arrived.sender)[1].lower()
        assert sender == mail.PERSONAL_ADDRESS, (
            f"the message went out From {sender!r}, not the account's address; "
            "a send through the bot box carries the bot's"
        )

        # The sent copy is written over the account's own IMAP login.
        assert envelope.get("sent_copy") == "saved", envelope
        copied = _wait_for_subject(service, mail.PERSONAL_ADDRESS, sent_before,
                                   subject, folder=SENT_FOLDER)
        assert copied is not None, (
            f"no copy in the account's {SENT_FOLDER} folder"
        )

        # No thread is recorded for an account send: its replies land in the
        # user's own mailbox, which nothing polls.
        recorded = stack.probe.rows_above("sent_emails", stack.mark,
                                          task_id=task["id"])
        assert recorded == [], recorded


def _latest_uid_or_zero(session: mail.ImapSession, folder: str) -> int:
    try:
        return session.latest_uid(folder)
    except mail.MailUnavailable:
        return 0


def _wait_for_subject(service: mail.MailService, mailbox: str, after: int,
                      subject: str, *, folder: str | None = None,
                      timeout: float = 60):
    deadline = time.monotonic() + timeout
    while True:
        with service.session(mailbox) as box:
            try:
                found = box.fetch_new_since(after, folder=folder)
            except mail.MailUnavailable:
                found = []
        for message in found:
            if message.subject == subject:
                return message
        if time.monotonic() >= deadline:
            return None
        time.sleep(2)
