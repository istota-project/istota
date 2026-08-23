"""The wire tier: a real mail server, no istota container, no compose project.

The narrowest tier in the tree, and the one with the least between the test and
the code. `poll_emails` is a plain function over a `Config`
(`transport/email/inbound.py:1279`) with no daemon behind it and no Nextcloud
dependency for attachment-free mail — so a test can call it directly against a
real IMAP server and a local SQLite database, and assert on both.

**Why it exists.** `imap_tools.MailBox` appears in the default suite exactly
once, inside a `patch`. Nothing had ever opened a socket to an IMAP or SMTP
server. Email routing precedence, thread matching, reply header construction and
the untrusted-sender gate have all shipped broken and been found in production
(`641ac5db`, ISSUE-245, ISSUE-234, ISSUE-227), and every one of them is a
property of what arrives on the wire rather than of what a fake handed back.

**What it costs.** One `docker run` of a small image, once per session, plus an
expunge of two mailboxes before each test. No istota image is built and nothing
is rendered, which is what separates this marker from `smoke`: it needs Docker
and nothing else.

**The TLS pairing, which is the one thing to get right before adding a test.**
istota picks its TLS mode by port number alone — 993 and 465 mean implicit TLS,
anything else means STARTTLS. The container publishes all four on ephemeral host
ports, and a published port is never 993. So the `Config` handed to istota names
the STARTTLS pair, where the branch it takes matches the endpoint it is talking
to, while this file's own driver uses the implicit pair. The deployed path — the
implicit branch against `mail:993` — is what `tests/smoke/test_email_e2e.py`
covers.
"""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from pathlib import Path

import pytest

from istota import db
from istota.config import Config, EmailConfig as AppEmailConfig, UserConfig
from istota.transport.email import inbound
from testbed.probe import Probe
from testbed.services import mail

from ..conftest import _require_no_xdist, require_docker

#: The one user every wire case routes to, and the address that is *theirs*.
#:
#: `@ext.test` rather than `@bot.test`: the second domain is the bot's own, and
#: a user address inside it would collapse into the bot mailbox on delivery.
USER_ID = "testuser"
USER_ADDRESS = "testuser@ext.test"

#: The plus-address the bot publishes for that user. Maddy rewrites every
#: recipient `*@bot.test` to the single bot mailbox, so this needs no account.
USER_TAG_ADDRESS = mail.tagged(USER_ID)


@pytest.fixture(scope="session")
def mail_server(pytestconfig, tmp_path_factory):
    """One Maddy container for the session, with its certificate trusted.

    Session-scoped because a container per test would be about a second each and
    buy nothing: `purge` empties both mailboxes in one round trip, which is a
    stronger reset than a fresh container gives the *database*, and the database
    is per-test regardless.

    `SSL_CERT_FILE` is set for the length of the session and restored after.
    That is the decision recorded in the spec: istota's own client always builds
    its context with `ssl.create_default_context()` and has no custom-CA or
    no-verify path, which is a property worth keeping — so the trust anchor is
    supplied from outside the application rather than by adding a knob inside
    it. `create_default_context` reads the variable at call time, so setting it
    here reaches every context any code under test builds afterwards.

    **`SSL_CERT_DIR` goes with it**, for the reason `testbed/compose/mail/
    mail.yml` records after measuring it in the container: `SSL_CERT_FILE`
    alone *replaces* the trust store rather than adding to it, so everything
    else in the same process loses every public CA. Nothing in this tier makes
    an outbound HTTPS request today, but the variable is process-wide and the
    session is not isolated — one `-m "testbed or smoke"` invocation, or a
    future case that fetches anything, and the symptom is `UnknownIssuer` from
    somewhere unrelated. The platform default keeps the rest of the store
    reachable by subject hash.
    """
    _require_no_xdist(pytestconfig)
    require_docker()
    cert_dir = tmp_path_factory.mktemp("mail-certs")
    try:
        running = mail.run_standalone(cert_dir=cert_dir)
    except mail.MailUnavailable as exc:
        pytest.skip(str(exc))

    added = {"SSL_CERT_FILE": str(running.server.ca_file)}
    capath = ssl.get_default_verify_paths().capath
    if capath:
        added["SSL_CERT_DIR"] = capath
    previous = {name: os.environ.get(name) for name in added}
    os.environ.update(added)
    try:
        yield running.server
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        running.close()


@dataclass
class Wire:
    """One test's view of the server and the database in front of it."""

    server: mail.MailServer
    config: Config
    probe: Probe

    def send(self, **kwargs) -> str:
        """Put a message into the bot's mailbox. Returns its `Message-ID`.

        Defaults to the plus-address rung (`bot+testuser@bot.test`), because
        that is what most cases want as their *background*; a case about a
        different rung overrides it.
        """
        kwargs.setdefault("to_addr", USER_TAG_ADDRESS)
        kwargs.setdefault("subject", "a subject")
        kwargs.setdefault("body", "a body")
        return mail.send(self.server, **kwargs)

    def poll(self) -> list[int]:
        """`poll_emails` against the real server. Returns created task ids.

        Only ingested tasks: a message filed as `discarded`, `quiet`,
        `throttled` or `read_error` produces no entry here, so an empty list is
        not the same as "nothing was polled". Every case that cares reads
        `processed()` as well.
        """
        return inbound.poll_emails(self.config)

    def processed(self) -> list[dict]:
        """Every `processed_emails` row, oldest first."""
        return self.probe.query(
            "SELECT * FROM processed_emails ORDER BY id",
        )

    def tasks(self) -> list[dict]:
        return self.probe.query("SELECT * FROM tasks ORDER BY id")

    def inbox(self) -> mail.ImapSession:
        """The bot's mailbox — what istota polls."""
        return mail.ImapSession(self.server, account=mail.BOT_ADDRESS)

    def outbox(self) -> mail.ImapSession:
        """The catch-all mailbox — where everything istota sends lands.

        Whatever the `To:` was: Maddy funnels every recipient outside `bot.test`
        into this one account, which is what lets a case invent a correspondent
        address without creating one.
        """
        return mail.ImapSession(self.server, account=mail.EXTERNAL_ADDRESS)


@pytest.fixture
def wire(mail_server, tmp_path) -> Wire:
    """A clean mailbox pair, a fresh database, and a `Config` pointed at both.

    Reset *before* the test rather than after, on the tier's standing rule: a
    failed case's state is still there to inspect, and the next case is still
    clean.

    Three in-process caches are cleared too. `poll_emails` deduplicates DMARC
    alerts for 24 hours, collapses confirmation prompts within a window, and
    counts failed reads per message across polls — all module globals, so
    without this the second case to touch any of them is reading the first's
    state and the failure looks like flake.
    """
    for account in mail.ACCOUNTS:
        with mail.ImapSession(mail_server, account=account) as session:
            session.purge()
    inbound._reset_dmarc_alert_dedup()
    inbound._reset_volume_state()
    inbound._reset_message_failures()

    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Wire(
        server=mail_server,
        config=make_wire_config(mail_server, tmp_path, db_path),
        probe=Probe(db_path=str(db_path), local=db_path),
    )


def make_wire_config(
    server: mail.MailServer, tmp_path: Path, db_path: Path, **overrides
) -> Config:
    """A `Config` istota will really poll this server with.

    `authserv_id` is `mail`, the server's own hostname, so a case can write an
    `Authentication-Results: mail; …` header and have the canary read it as the
    receiving MTA's verdict — and write any other id to have it discarded.
    Measured rather than assumed: Maddy passes an inbound
    `Authentication-Results` through verbatim, adding none of its own.

    The per-sender rate limit is off (`0`). This file sends around twenty
    messages, most of them from one address, against a shipped default of twenty
    an hour — so the twenty-first would be filed `throttled` and produce no task,
    which reads exactly like a routing defect. The one case that is *about* the
    limit turns it back on.
    """
    config = Config()
    config.db_path = db_path
    config.temp_dir = tmp_path / "temp"
    config.temp_dir.mkdir(exist_ok=True)
    config.skills_dir = tmp_path / "skills"
    config.skills_dir.mkdir(exist_ok=True)
    config.email = AppEmailConfig(
        enabled=True,
        imap_host=server.host,
        # The STARTTLS pair, deliberately: see the module docstring.
        imap_port=server.imap_starttls_port,
        imap_user=mail.BOT_ADDRESS,
        imap_password=mail.MAIL_PASSWORD,
        smtp_host=server.host,
        smtp_port=server.smtp_starttls_port,
        bot_email=mail.BOT_ADDRESS,
        poll_folder="INBOX",
        authserv_id="mail",
    )
    config.users = {USER_ID: UserConfig(email_addresses=[USER_ADDRESS])}
    config.scheduler.email_sender_rate_limit_messages = 0
    for key, value in overrides.items():
        setattr(config, key, value)
    return config
