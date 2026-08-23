"""An attachment arriving by mail, and Nextcloud serving the bytes back.

The one part of the inbound email path the lean shape cannot reach. Everything
else — routing, threading, the gate, the reply headers — is covered without a
Nextcloud by `tests/testbed/test_email_wire.py` and
`tests/smoke/test_email_e2e.py`, and both are cheaper.

**What "uploaded to Nextcloud" turns out to mean on this deployment**, because
the spec's phrasing assumed a WebDAV write and there is not one.
`upload_file_to_inbox_v2` branches on `config.use_mount`, and
`render-config.sh` writes `nextcloud_mount_path` as the literal `/mnt/shared` on
every profile — so the write is an ordinary `shutil.copy2` onto a Docker volume.
Nextcloud reaches the same bytes through the `files_external` *local* mount
`provision-nc.sh` creates for the bot, which is why the WebDAV path carries a
`Shared Files/` prefix that the on-disk path does not. Stage 5 measured that
round trip for `storage.py`; this file is the same shape one subsystem over, and
the assertion is the end-to-end one: bytes a stranger put in an email are
readable through the deployment's own file surface.

The second class here is smaller and is about this stage's product change. Two
`ISTOTA_EMAIL_*` variables were read by the generator and passed by nothing, so
the only place the fix is visible is the config the container rendered for
itself — which is exactly what the full shape exists to witness.
"""

from __future__ import annotations

import json
import time

import pytest

from testbed.services import mail
from testbed.services.nextcloud import BOT_MOUNT_POINT

pytestmark = pytest.mark.full

USER_ID = "testuser"
USER_ADDRESS = "testuser@ext.test"

ANSWER = "the scripted answer to your message"

#: Structured output, for the reason `tests/smoke/test_email_e2e.py` records:
#: `deliver_email_result` reads the model's final message as JSON and skips
#: delivery entirely when it is prose, on the assumption the model sent the mail
#: itself. Four turns, because eleven pollers share this stack.
SCRIPT = [{"text": json.dumps({"body": ANSWER, "format": "plain"})}] * 4

ATTACHMENT_BODY = b"the bytes that arrived by email\n"

#: A clean verdict from the authserv-id this profile configured, and a failing
#: one.
#:
#: The `full` profile runs `confirm_sender_match = "verify"`, so a sender
#: claiming the user's own address is proof only when the receiving MTA says so.
#: Every scenario here writes `From: testuser@ext.test`, which is that claim, so
#: without a passing stamp the mail is held for confirmation and never
#: completes. Maddy passes an inbound `Authentication-Results` through verbatim,
#: which is what makes writing one from a test mean anything.
PASSING_STAMP = {
    "Authentication-Results": "mail; spf=pass smtp.mailfrom=ext.test; "
    "dkim=pass; dmarc=pass header.from=ext.test"
}
FAILING_STAMP = {"Authentication-Results": "mail; dmarc=fail header.from=ext.test"}


def _wait_for_email_task(stack, *, status: str = "completed", timeout: float = 300):
    """The newest email task above this test's watermark.

    `source_type` alone matches every earlier scenario's row on a session-scoped
    stack, and the daemon makes this task rather than the test, so there is no
    id to scope to. The watermark is the other half.
    """
    return stack.probe.wait_for_task(
        status=status,
        source_type="email",
        id_above=stack.mark["tasks"],
        timeout=timeout,
    )


def _wait_for_dav(
    nextcloud, path: str, name: str, *, timeout: float = 60
) -> list[str]:
    """Poll a WebDAV collection until it holds `name`, and answer the listing.

    Nextcloud serves a file written underneath it by the next PROPFIND of the
    parent, with no rescan — measured in Stage 5. The poll is for the write
    itself: the task is terminal before the daemon has necessarily finished
    every transaction around it.

    **The exit condition is the file, not "the collection is non-empty."** That
    directory is not cleared between tests and `/mnt/shared` survives a session
    under `ISTOTA_TESTBED_KEEP`, so an earlier scenario's file makes a
    non-empty check return on the first poll — turning the wait into a no-op
    and the assertion into a race that happens to pass on the ordering.
    """
    deadline = time.monotonic() + timeout
    listing: list[str] = []
    while True:
        listing = nextcloud.files(path, user=nextcloud.bot_user)
        if any(entry.endswith(name) for entry in listing):
            return listing
        if time.monotonic() >= deadline:
            return listing
        time.sleep(2)


class TestAnAttachmentReachesTheUsersInboxTree:
    @pytest.mark.profile("full")
    @pytest.mark.script(SCRIPT)
    def test_the_bytes_are_readable_through_nextcloud(self, stack):
        service: mail.MailService = stack.service("mail")
        nextcloud = stack.service("nextcloud")
        name = f"quarterly-{stack.mark['tasks']}.txt"

        service.send(
            from_addr=USER_ADDRESS,
            to_addr=mail.tagged(USER_ID),
            subject="the quarterly numbers",
            body="attached, as promised",
            headers=PASSING_STAMP,
            attachments=[(name, "text/plain", ATTACHMENT_BODY)],
        )
        task = _wait_for_email_task(stack)

        # `Shared Files/` is the bot's `files_external` mount point, so this is
        # the same directory as `/mnt/shared/Users/testuser/inbox` seen from
        # Nextcloud's side. The on-disk path and the DAV path differ by exactly
        # that prefix.
        inbox = f"{BOT_MOUNT_POINT}/Users/{USER_ID}/inbox"
        listing = _wait_for_dav(nextcloud, inbox, name)
        stored = [entry for entry in listing if entry.endswith(name)]
        assert stored, (
            f"nothing named {name!r} under {inbox!r}; the directory holds "
            f"{listing}\n{stack.diagnostics(task)}"
        )
        assert nextcloud.read_file(stored[0], user=nextcloud.bot_user) == (
            ATTACHMENT_BODY
        )

    @pytest.mark.profile("full")
    @pytest.mark.script(SCRIPT)
    def test_the_prompt_names_the_stored_path_rather_than_a_temp_one(self, stack):
        """What the model is told about the file.

        The prompt carries the path `upload_file_to_inbox_v2` returned, and the
        fallback branch carries the temp directory the download wrote to
        instead. They are easy to tell apart and mean different things: the
        second says the file exists only inside this task's scratch space and
        will be gone when it ends.
        """
        service: mail.MailService = stack.service("mail")
        name = f"invoice-{stack.mark['tasks']}.txt"

        service.send(
            from_addr=USER_ADDRESS,
            to_addr=mail.tagged(USER_ID),
            subject="one invoice",
            body="attached",
            headers=PASSING_STAMP,
            attachments=[(name, "application/pdf", b"%PDF-1.4 not really\n")],
        )
        task = _wait_for_email_task(stack)

        assert name in (task["prompt"] or ""), (
            f"the attachment is not named in the assembled prompt\n"
            f"{stack.diagnostics(task)}"
        )
        stored = task["attachments"] or ""
        assert f"/Users/{USER_ID}/inbox/" in stored, stored
        assert "/attachments_" not in stored, (
            "the upload fell back to the task's temp directory, so the file is "
            f"not in the user's storage at all: {stored}"
        )


class TestTheEmailSettingsCompose:
    """The two variables this stage added to `docker-compose.yml`.

    Read out of the config the *container* rendered for itself, which is the
    only place the fix is visible: on the lean shape the generator runs on the
    host and every variable it reads is reachable, so the gap this closes does
    not exist there. `render-config.sh` read both and compose passed neither, so
    an operator setting either in `docker/.env` silently got the default.

    **Both values are ones the shell default is not**, and that is the point of
    choosing them. `authserv_id` defaults to empty and `confirm_sender_match` to
    `off`, so a profile asking for `off` would render the same line with the
    compose hunk reverted and this class would pass against the defect it exists
    to catch. The profile asks for `verify`, which nothing else can produce.
    """

    @pytest.mark.profile("full")
    def test_the_authserv_id_and_gate_mode_reach_the_rendered_config(self, stack):
        rendered = stack.exec(["cat", "/data/config/config.toml"])
        assert rendered.returncode == 0, rendered.stderr

        assert 'authserv_id = "mail"' in rendered.stdout, (
            "the container rendered an empty authserv_id, so "
            "ISTOTA_EMAIL_AUTHSERV_ID did not reach the generator"
        )
        assert 'confirm_sender_match = "verify"' in rendered.stdout, (
            "the container rendered the shell default, so "
            "ISTOTA_EMAIL_CONFIRM_SENDER_MATCH did not reach the generator"
        )


class TestTheSelfClaimGateOnTheDeployedShape:
    """`verify`, doing the thing it exists for, in the artifact.

    The behavioural half of the class above, and the stronger of the two: it
    cannot pass with the compose hunk reverted for a reason that has nothing to
    do with reading a file. Under the shell default (`off`) a `From:` naming the
    user's own address is proof on its own, so both messages below would run.
    Under `verify` the verdict decides, and the pair differs only in the header
    the receiving MTA wrote.

    Neither claim is reachable on the lean shape: `verify` refuses to start
    without an `authserv_id`, and that is the other variable compose was
    dropping.
    """

    @pytest.mark.profile("full")
    @pytest.mark.script(SCRIPT)
    def test_a_self_claim_the_mta_did_not_authenticate_is_held(self, stack):
        service: mail.MailService = stack.service("mail")
        service.send(
            from_addr=USER_ADDRESS,
            to_addr=mail.tagged(USER_ID),
            subject="unauthenticated self-claim",
            body="I am you, honestly",
            headers=FAILING_STAMP,
        )

        task = _wait_for_email_task(stack, status="pending_confirmation")

        assert task["status"] == "pending_confirmation", (
            "a self-claiming message with a dmarc=fail stamp ran without being "
            f"asked about; the gate is not in `verify`\n{stack.diagnostics(task)}"
        )
        assert task["confirmation_prompt"]

    @pytest.mark.profile("full")
    @pytest.mark.script(SCRIPT)
    def test_a_self_claim_the_mta_authenticated_runs(self, stack):
        """The other half of the pair, and the reason the first one means
        something: `verify` that held everything would be indistinguishable from
        `gate`, and the failing case above would pass under it."""
        service: mail.MailService = stack.service("mail")
        service.send(
            from_addr=USER_ADDRESS,
            to_addr=mail.tagged(USER_ID),
            subject="authenticated self-claim",
            body="I am you, and the MTA agrees",
            headers=PASSING_STAMP,
        )

        task = _wait_for_email_task(stack, status="completed")

        assert task["status"] == "completed", (
            "a self-claiming message with a clean dmarc=pass stamp was held; "
            f"`verify` is behaving like `gate`\n{stack.diagnostics(task)}"
        )

    @pytest.mark.profile("full")
    def test_the_daemon_is_pointed_at_the_mail_container(self, stack):
        """The rest of the mail service's `config_env`, on the shape where a
        variable can be read by the generator and dropped by compose."""
        rendered = stack.exec(["cat", "/data/config/config.toml"])

        for expected in (
            'imap_host = "mail"',
            "imap_port = 993",
            'smtp_host = "mail"',
            "smtp_port = 465",
            f'bot_email = "{mail.BOT_ADDRESS}"',
        ):
            assert expected in rendered.stdout, expected
