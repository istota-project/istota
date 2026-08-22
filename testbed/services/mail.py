"""A real mail server, and the SMTP/IMAP driver that talks to it.

Maddy, not a stub. The distinction the package draws between a *service* and a
*stub* is at its sharpest here: `imap_tools.MailBox` appears in istota's suite
exactly once, inside a `patch`, and nothing has ever opened a socket to an IMAP
or SMTP server. Email routing precedence, thread matching, reply header
construction and the untrusted-sender gate have all shipped broken and been
found in production. A stub of IMAP would be a second implementation of the
thing that keeps being wrong.

Maddy rather than GreenMail or Mailpit, settled the expensive way by
istota-redteam: GreenMail's IMAP rejects the parenthesized `UID SEARCH` that
imap-tools always sends, and Mailpit has no IMAP server at all. Maddy serves
implicit TLS on 993/465 and STARTTLS on 143/587 from one small container, which
is what lets istota's port-based TLS branch run unmodified.

**Two shapes, because it is used two ways.**

`run_standalone` is a bare `docker run` for the in-process wire suite, which has
no compose project and no istota container at all — it calls `poll_emails` and
the email skill directly against the real server, over a local SQLite DB.

`serve` is the `Service` conformer for a profile on either stack shape. It
starts nothing: the profile's mail overlay (`testbed/compose/mail/mail.yml`) runs
the container inside the compose project, where the daemon reaches it as `mail`
on the standard ports. `serve` materializes the certificate the overlay binds,
returns the `ISTOTA_EMAIL_*` variables that point the shipped generator at it,
and — once the stack is up — binds a driver to the published host ports so a
scenario can put mail in and read replies out.

**All four ports are published, and that is not redundancy.** istota picks its
TLS mode by port number alone: 993 and 465 mean implicit TLS, anything else
means STARTTLS. A host-published port is ephemeral by design, so a wire test
configuring istota with one of those would exercise the STARTTLS branch whatever
the container port behind it was. Publishing both pairs lets the wire suite
point istota's own config at the STARTTLS pair — where the branch it takes
matches what it is talking to — while this module's driver always uses the
implicit pair, which is what the deployed profiles use.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import imaplib
import logging
import os
import smtplib
import socket
import ssl
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

from .. import certs

logger = logging.getLogger("testbed.services.mail")

#: Pinned by digest. istota-redteam runs `:latest`, which is fine for a manually
#: driven campaign and not for a suite that has to give the same answer next
#: month. The digest is the multi-arch manifest list, so it resolves on arm64
#: and amd64 alike. `ISTOTA_TESTBED_MAIL_IMAGE` overrides it — for trying a new
#: Maddy before pinning it, not for routine use.
MAIL_IMAGE = (
    "foxcpp/maddy@sha256:"
    "de42151adff6388edb5e4ee88f60334fa1ab85e309485193ecb1c2db20203315"
)
MAIL_IMAGE_ENV = "ISTOTA_TESTBED_MAIL_IMAGE"

#: The mailbox istota polls, and the one the outside world reads.
#:
#: `.test` is reserved by RFC 6761, so neither can ever resolve — a message that
#: escaped this rig would go nowhere rather than somewhere.
BOT_ADDRESS = "bot@bot.test"
EXTERNAL_ADDRESS = "catchall@ext.test"
ACCOUNTS = (BOT_ADDRESS, EXTERNAL_ADDRESS)

#: Both accounts share it. Not a secret in any sense that matters: the server
#: has two mailboxes, serves a domain that cannot resolve, and publishes its
#: ports on loopback only. A generated value would have to be threaded into the
#: rendered config, the overlay and every wire test, buying nothing.
MAIL_PASSWORD = "maddy-testbed"  # private-data-ok

#: Container-side ports. Maddy's own defaults; the config file binds all four.
IMAP_TLS_PORT = 993
SMTP_TLS_PORT = 465
IMAP_STARTTLS_PORT = 143
SMTP_STARTTLS_PORT = 587
CONTAINER_PORTS = (
    IMAP_TLS_PORT,
    SMTP_TLS_PORT,
    IMAP_STARTTLS_PORT,
    SMTP_STARTTLS_PORT,
)

#: The compose service name, which is also the name in the certificate's SAN.
SERVICE_NAME = "mail"

#: Where `maddy.conf` and `accounts.conf` live in this checkout.
CONF_DIR = Path(__file__).resolve().parent.parent / "compose" / "mail"

#: The overlay a profile names to get a mail container.
OVERLAY = CONF_DIR / "mail.yml"

LOOPBACK = "127.0.0.1"

READY_TIMEOUT = 60.0
IMAP_TIMEOUT = 30.0


class MailUnavailable(RuntimeError):
    """The mail server could not be started or reached.

    Environmental, never a code defect: no Docker daemon, a pull that failed, a
    digest that no longer exists. The fixtures translate it to `pytest.skip`,
    on the tier's standing rule that a run must never report green because
    something never came up.
    """


def mail_image() -> str:
    """The image to run, honouring the override."""
    return os.environ.get(MAIL_IMAGE_ENV) or MAIL_IMAGE


# -- the driver -------------------------------------------------------------


@dataclass(frozen=True)
class MailServer:
    """Where a *host* process reaches the mail server.

    Never the container-side address: inside the compose network the server is
    `mail` on the standard ports, and that pairing is `MailService.config_env`'s
    to state. This is the other end — a published port on loopback, whichever
    one Docker picked.
    """

    host: str
    imap_port: int
    """Maps to container 993. Implicit TLS."""
    smtp_port: int
    """Maps to container 465. Implicit TLS."""
    imap_starttls_port: int = 0
    """Maps to container 143. What a wire test hands istota's own config."""
    smtp_starttls_port: int = 0
    """Maps to container 587. Same."""
    ca_file: Path | None = None
    """The self-signed certificate, which is its own trust anchor."""

    def context(self) -> ssl.SSLContext:
        """A verifying context that trusts this server and nothing else extra.

        Verifying, deliberately. istota's own client always verifies and has no
        no-verify path, which is a property worth keeping; a driver that skipped
        verification would be able to pass against a certificate the daemon
        would refuse.
        """
        return ssl.create_default_context(
            cafile=str(self.ca_file) if self.ca_file else None
        )


@dataclass(frozen=True)
class ReceivedMessage:
    """One message read back off the wire.

    Every header here is **wire text**: `message_id`, `in_reply_to`,
    `references`, `sender`, `recipients` and `headers` are read through a
    `compat32` parse, so an RFC 2047 encoded-word arrives encoded. The wire suite
    exists because a header in an unexpected encoding is how a reply was once
    lost (`641ac5db`), and a driver that quietly decoded would report the
    repaired value.

    `subject` and `body_text` are the exception and are decoded, because a test
    reading a subject wants the text and no defect in this area has ever been
    about the subject's transfer encoding.
    """

    uid: int
    message_id: str
    in_reply_to: str | None
    references: str | None
    sender: str
    recipients: list[str]
    subject: str
    body_text: str
    headers: dict[str, str] = field(default_factory=dict)


def send(
    server: MailServer,
    *,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    from_name: str | None = None,
    reply_to: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    headers: dict[str, str] | list[tuple[str, str]] | None = None,
    attachments: list[tuple[str, str, bytes]] | None = None,
    auth: tuple[str, str] = (EXTERNAL_ADDRESS, MAIL_PASSWORD),
    message_id: str | None = None,
    body_subtype: str = "plain",
    cte: str | None = None,
) -> str:
    """Send one message through submission. Returns its `Message-ID`.

    **`headers`, `in_reply_to` and `references` go onto the wire verbatim**, and
    that is not a nicety — it is what makes half the wire suite mean anything.
    `EmailMessage` under `policy.SMTP` parses an assigned header value through
    the header registry, and an unregistered name like `References` or
    `Authentication-Results` lands on `UnstructuredHeader`, which *decodes* RFC
    2047 encoded-words on assignment and re-folds on serialization. So
    `message["References"] = "=?utf-8?Q?=3Ca=40x=3E?="` puts `<a@x>` on the
    wire, and a test aiming at the encoded-word bug (`641ac5db`) would send
    plain text and pass against the code it was written to catch. Measured, not
    reasoned: the first version of this function did exactly that, and the
    negative control is what found it.

    These three are therefore prepended to the *serialized bytes* and the
    message goes out through `sendmail` rather than `send_message`. `headers`
    accepts a list of pairs as well as a dict, because two
    `Authentication-Results` headers is a case and their order is what the
    canary reads.

    `attachments` is `(filename, content_type, payload)`. A `content_type` of
    `"application/octet-stream"` is the safe default; the string is split on `/`
    and handed to `add_attachment`.

    Implicit TLS on `smtp_port`, always. The submission endpoint requires
    authentication and does not check that the authenticated account owns the
    `From`, so one credential can send as any invented address — which is how a
    routing case gets a distinct sender per test without creating an account.
    """
    message = EmailMessage(policy=email.policy.SMTP)
    message["From"] = email.utils.formataddr((from_name or "", from_addr))
    message["To"] = to_addr
    message["Subject"] = subject
    message["Date"] = email.utils.formatdate(localtime=True)
    message["Message-ID"] = message_id or email.utils.make_msgid(
        domain=from_addr.rsplit("@", 1)[-1]
    )
    if reply_to:
        message["Reply-To"] = reply_to
    # `body_subtype` and `cte` are what let a case put a *shape* on the wire
    # rather than a string: an HTML-only message, a quoted-printable body, an
    # 8-bit one. Python picks base64 for non-ASCII text unless told otherwise,
    # so leaving `cte` alone would make two of those three unreachable.
    if cte is None:
        message.set_content(body, subtype=body_subtype)
    else:
        message.set_content(body, subtype=body_subtype, cte=cte)
    for filename, content_type, payload in attachments or []:
        maintype, _, subtype = content_type.partition("/")
        message.add_attachment(
            payload,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=filename,
        )

    # Prepended, so an `Authentication-Results` a case writes is the topmost one
    # — which is what RFC 8601 says the most recent hop's is, and the assumption
    # a blank `authserv_id` rests on.
    verbatim: list[tuple[str, str]] = []
    if headers:
        verbatim += (
            list(headers.items()) if isinstance(headers, dict) else list(headers)
        )
    if in_reply_to:
        verbatim.append(("In-Reply-To", in_reply_to))
    if references:
        verbatim.append(("References", references))
    raw = b"".join(
        f"{name}: {value}\r\n".encode() for name, value in verbatim
    ) + message.as_bytes()

    # `sendmail` with bytes, not `send_message`, because the header block above
    # is already serialized. `send_message` is also what negotiates
    # `BODY=8BITMIME`, so that is done by hand — without it an 8-bit body is
    # refused by a conforming server, and the 8-bit body is one of the cases.
    options = [] if raw.isascii() else ["BODY=8BITMIME"]
    with smtplib.SMTP_SSL(
        server.host, server.smtp_port, context=server.context(), timeout=IMAP_TIMEOUT
    ) as client:
        client.login(*auth)
        client.sendmail(from_addr, [to_addr], raw, mail_options=options)
    return message["Message-ID"]


class ImapSession:
    """A logged-in IMAP connection over implicit TLS, as a context manager.

    Every read is `BODY.PEEK`, so driving the mailbox never marks a message
    `\\Seen`. That is not tidiness: `poll_emails` fetches with `mark_seen=False`
    and a test that flipped the flag underneath it would be changing the state
    the code under test reads.
    """

    def __init__(
        self,
        server: MailServer,
        *,
        account: str = BOT_ADDRESS,
        password: str = MAIL_PASSWORD,
        folder: str = "INBOX",
    ) -> None:
        self.server = server
        self.account = account
        self.password = password
        self.folder = folder
        self._conn: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "ImapSession":
        self._conn = imaplib.IMAP4_SSL(
            self.server.host,
            self.server.imap_port,
            ssl_context=self.server.context(),
            timeout=IMAP_TIMEOUT,
        )
        self._conn.login(self.account, self.password)
        self._conn.select(self.folder)
        return self

    def __exit__(self, *exc) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        for step in (conn.close, conn.logout):
            try:
                step()
            except Exception:  # pragma: no cover - teardown is best effort
                logger.debug("closing an IMAP session raised", exc_info=True)

    def _connection(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            raise RuntimeError("ImapSession is not entered")
        return self._conn

    def _select(self, folder: str) -> imaplib.IMAP4_SSL:
        conn = self._connection()
        if folder != self.folder:
            conn.select(folder)
            self.folder = folder
        return conn

    def uids(self, folder: str = "INBOX") -> list[int]:
        """Every UID in the folder, ascending."""
        conn = self._select(folder)
        typ, data = conn.uid("search", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return []
        return sorted(int(raw) for raw in data[0].split())

    def latest_uid(self, folder: str = "INBOX") -> int:
        """The highest UID present, or 0 for an empty folder.

        0 rather than `None`, because every caller uses it as "everything after
        this" and `None` would make each of them write the same guard.
        """
        found = self.uids(folder)
        return found[-1] if found else 0

    def fetch_new_since(self, uid: int, folder: str = "INBOX") -> list[ReceivedMessage]:
        """Every message with a UID strictly greater than `uid`."""
        conn = self._select(folder)
        return [self._fetch(conn, found) for found in self.uids(folder) if found > uid]

    def wait_for_new(
        self,
        uid: int,
        *,
        timeout: float,
        poll_interval: float = 1.0,
        folder: str = "INBOX",
    ) -> list[ReceivedMessage]:
        """Block until something arrives after `uid`, or return empty at timeout.

        One second between polls, against istota-redteam's five. A local server
        has no reason to be polled that slowly, and at five the wait would
        dominate a suite whose assertions are otherwise milliseconds.
        """
        deadline = time.monotonic() + timeout
        while True:
            found = self.fetch_new_since(uid, folder=folder)
            if found:
                return found
            if time.monotonic() >= deadline:
                return []
            time.sleep(poll_interval)

    def purge(self, folder: str = "INBOX") -> int:
        """Mark every message `\\Deleted` and expunge. Returns the count.

        What makes the wire suite order-independent without recreating the
        container per test. Expunging does not rewind UIDs — the next message
        still gets a higher one — so a poll cursor stored against this mailbox
        stays valid across a purge, which is exactly the property a session
        holding both a mailbox and a database needs.
        """
        conn = self._select(folder)
        present = self.uids(folder)
        if not present:
            return 0
        conn.uid("store", ",".join(str(uid) for uid in present), "+FLAGS", "(\\Deleted)")
        conn.expunge()
        return len(present)

    def _fetch(self, conn: imaplib.IMAP4_SSL, uid: int) -> ReceivedMessage:
        """One message, parsed twice, and the second parse is the point.

        `policy.default` is what can decode a body — `get_content()` needs it —
        and it also decodes every RFC 2047 encoded-word in a header on the way
        past. That is the wrong read for a driver whose job is to say what
        arrived: this suite exists because an identifier header arriving as
        encoded-words was once mishandled, and a reader that silently decoded
        would report the repaired value.

        So headers come from a `compat32` parse, which hands back the wire text,
        and the body from a `default` one.
        """
        typ, data = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError(f"could not fetch uid {uid} from {self.account}")
        payload = data[0][1]
        parsed = email.message_from_bytes(payload, policy=email.policy.default)
        raw = email.message_from_bytes(payload, policy=email.policy.compat32)
        # First occurrence wins, which for a repeated header is the topmost —
        # RFC 8601's most recent hop, and the one every reader of an
        # `Authentication-Results` means. A later duplicate is dropped rather
        # than overwriting it.
        headers: dict[str, str] = {}
        for name, value in raw.items():
            headers.setdefault(name, value)
        return ReceivedMessage(
            uid=uid,
            message_id=headers.get("Message-ID", ""),
            in_reply_to=headers.get("In-Reply-To"),
            references=headers.get("References"),
            sender=headers.get("From", ""),
            recipients=[
                address.strip()
                for header in ("To", "Cc")
                for address in headers.get(header, "").split(",")
                if address.strip()
            ],
            subject=str(parsed.get("Subject", "")),
            body_text=_text_body(parsed),
            headers=headers,
        )


def _optional(value) -> str | None:
    return None if value is None else str(value)


def _text_body(message) -> str:
    """The best available text body, preferring `text/plain`.

    Falls back to `text/html` rather than to the empty string, because "no
    `text/plain` part" is one of the wire cases and a driver that returned
    nothing there would make the test unable to tell that case from a lost
    message.
    """
    if not message.is_multipart():
        if message.get_content_maintype() != "text":
            return ""
        return message.get_content()
    for wanted in ("text/plain", "text/html"):
        for part in message.walk():
            if part.get_content_type() == wanted:
                return part.get_content()
    return ""


# -- the container ----------------------------------------------------------


def _free_port() -> int:
    """Bind `:0`, read the port back, release it.

    A published port is chosen here rather than left to Docker so the caller can
    say which one it got before the container exists — and picked this way
    rather than from a fixed pair, so a developer's own red-team stack on
    10993/10465 does not collide.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


#: The container's entrypoint, shared with the compose overlay.
#:
#: A file rather than an inline string in each, because a compose `entrypoint:`
#: has to write `$$` for every `$` — so an inline copy would differ from this
#: one character by character, which is the shape that drifts. The addresses and
#: the password live in the script; a default-suite test asserts they still
#: agree with the constants above.
ENTRYPOINT = CONF_DIR / "entrypoint.sh"


@dataclass
class StandaloneMail:
    """A Maddy container this process started, and how to reach and stop it.

    `run_standalone` returns this rather than the bare `MailServer` the spec
    sketched: something has to hold the container id, and a caller that has an
    address but no handle cannot stop what it started.
    """

    server: MailServer
    container: str
    _closed: bool = False

    def close(self) -> None:
        """Remove the container. Idempotent."""
        if self._closed:
            return
        self._closed = True
        subprocess.run(
            ["docker", "rm", "--force", "--volumes", self.container],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def __enter__(self) -> "StandaloneMail":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def run_standalone(
    *,
    cert_dir: Path,
    host_imap_port: int = 0,
    host_smtp_port: int = 0,
    host_imap_starttls_port: int = 0,
    host_smtp_starttls_port: int = 0,
    timeout: float = READY_TIMEOUT,
) -> StandaloneMail:
    """Just the container, for the in-process wire suite. No compose.

    The wire suite has no istota container and no compose project: it calls
    `poll_emails` and the email skill directly, against this server and a local
    SQLite database. So this is a plain `docker run` with the four ports
    published on loopback, and the caller gets an address rather than a stack.

    A port of 0 means one is chosen by binding `:0` and releasing it.
    """
    crt, key = certs.generate_self_signed(cert_dir)
    ports = {
        IMAP_TLS_PORT: host_imap_port or _free_port(),
        SMTP_TLS_PORT: host_smtp_port or _free_port(),
        IMAP_STARTTLS_PORT: host_imap_starttls_port or _free_port(),
        SMTP_STARTTLS_PORT: host_smtp_starttls_port or _free_port(),
    }
    name = f"istota-testbed-mail-{uuid.uuid4().hex[:8]}"
    argv = [
        "docker",
        "run",
        "--detach",
        "--name",
        name,
        "--hostname",
        SERVICE_NAME,
        "--env",
        f"MADDY_HOSTNAME={SERVICE_NAME}",
        # State — both SQLite files — on tmpfs, so nothing outlives the
        # container and a leaked one cannot seed the next run's mailboxes.
        "--tmpfs",
        "/data",
        "--volume",
        f"{CONF_DIR / 'maddy.conf'}:/data/maddy.conf:ro",
        "--volume",
        f"{CONF_DIR / 'accounts.conf'}:/data/accounts.conf:ro",
        "--volume",
        f"{ENTRYPOINT}:/data/entrypoint.sh:ro",
        "--volume",
        f"{crt}:/data/tls/fullchain.pem:ro",
        "--volume",
        f"{key}:/data/tls/privkey.pem:ro",
    ]
    for container_port, host_port in ports.items():
        argv += ["--publish", f"{LOOPBACK}:{host_port}:{container_port}"]
    argv += ["--entrypoint", "/bin/sh", mail_image(), "/data/entrypoint.sh"]

    result = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise MailUnavailable(
            f"could not start {mail_image()}: docker run exited "
            f"{result.returncode}\n{result.stderr.strip()}"
        )

    server = MailServer(
        host=LOOPBACK,
        imap_port=ports[IMAP_TLS_PORT],
        smtp_port=ports[SMTP_TLS_PORT],
        imap_starttls_port=ports[IMAP_STARTTLS_PORT],
        smtp_starttls_port=ports[SMTP_STARTTLS_PORT],
        ca_file=crt,
    )
    standalone = StandaloneMail(server=server, container=name)
    try:
        wait_reachable(server, timeout=timeout, logs=lambda: _container_logs(name))
    except BaseException:
        standalone.close()
        raise
    return standalone


def _container_logs(name: str) -> str:
    result = subprocess.run(
        ["docker", "logs", "--tail", "40", name],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return (result.stdout or "") + (result.stderr or "")


def wait_reachable(
    server: MailServer, *, timeout: float = READY_TIMEOUT, logs=None
) -> None:
    """Poll until both mailboxes accept a login, or raise with the server log.

    Both, not one. Account creation runs before the server starts, so a partial
    failure leaves an IMAP endpoint that answers and one mailbox that does not
    exist — which surfaces at the first test as an authentication error against
    whichever account that test happened to use.
    """
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        try:
            for account in ACCOUNTS:
                with ImapSession(server, account=account):
                    pass
            return
        except Exception as exc:  # noqa: BLE001 - the message is the point
            last = f"{type(exc).__name__}: {exc}"
        if time.monotonic() >= deadline:
            tail = f"\n--- container log ---\n{logs()}" if logs else ""
            raise MailUnavailable(
                f"the mail server did not accept a login within {timeout:.0f}s; "
                f"last error was {last}{tail}"
            )
        time.sleep(0.5)


# -- the `Service` conformer ------------------------------------------------


def serve(state_dir: Path) -> "MailService":
    """The `Service` conformer, for a profile on either shape.

    Starts nothing. The profile names `testbed/compose/mail/mail.yml`, and
    compose runs the container inside the project where the daemon reaches it
    as `mail` on the standard ports — which is what keeps the port-based TLS
    branch under test. What this does is materialize the certificate the overlay
    binds and hand back the variables that point the shipped generator at the
    server.

    Deliberately not `run_standalone`. A container the pytest process started
    would be reachable from the daemon only as `host.docker.internal` on an
    ephemeral port, and istota reads 993/465 as "implicit TLS" and anything else
    as STARTTLS — so the deployed path would silently exercise the branch the
    deployment does not use.
    """
    crt, key = certs.generate_self_signed(state_dir / "certs")
    return MailService(cert_file=crt, key_file=key)


@dataclass
class MailService:
    """`Service` over the mail container the profile's overlay runs."""

    cert_file: Path
    key_file: Path
    name: str = SERVICE_NAME
    _stack = None
    _server: MailServer | None = None

    @property
    def container_url(self) -> str:
        """`mail` — the compose service name, and the name in the cert's SAN.

        A bare hostname rather than a URL, and it is the one member of the
        protocol that does not fit this service cleanly: mail is two protocols
        on four ports and has no single address. `config_env()` is what actually
        wires the daemon up; this exists so `diagnostics` and the fixture need
        no special case.
        """
        return SERVICE_NAME

    def config_env(self) -> dict[str, str]:
        """The `ISTOTA_EMAIL_*` variables the shipped generator reads.

        Every one of these is read by `docker/istota/render-config.sh` *and*
        passed through by `docker/docker-compose.yml`, which is the two-file
        rule. Two of them — `ISTOTA_EMAIL_AUTHSERV_ID` and
        `ISTOTA_EMAIL_CONFIRM_SENDER_MATCH` — were read by the generator and not
        passed by compose until this service needed them; adding them was a
        reviewed product change, not a fixture working around the gap.

        There is no `ISTOTA_EMAIL_SMTP_USER` or `..._PASSWORD`, and there does
        not need to be: `Config.effective_smtp_user` and
        `effective_smtp_password` fall back to the IMAP credentials, and this
        rig uses one bot account for both. That is the kind of thing a "no
        product change" claim rests on, so it is stated rather than assumed.

        `authserv_id` is `mail`, which is the server's own hostname. The DMARC
        canary only reads a verdict from a header stamped by an authserv-id it
        was told to trust, so a scenario that wants a verdict seen writes
        `Authentication-Results: mail; …` — and one that wants it ignored writes
        any other id.
        """
        return {
            "ISTOTA_EMAIL_ENABLED": "true",
            "ISTOTA_EMAIL_IMAP_HOST": SERVICE_NAME,
            "ISTOTA_EMAIL_IMAP_PORT": str(IMAP_TLS_PORT),
            "ISTOTA_EMAIL_IMAP_USER": BOT_ADDRESS,
            "ISTOTA_EMAIL_IMAP_PASSWORD": MAIL_PASSWORD,
            "ISTOTA_EMAIL_SMTP_HOST": SERVICE_NAME,
            "ISTOTA_EMAIL_SMTP_PORT": str(SMTP_TLS_PORT),
            "ISTOTA_EMAIL_POLL_FOLDER": "INBOX",
            "ISTOTA_EMAIL_BOT_ADDRESS": BOT_ADDRESS,
            "ISTOTA_EMAIL_AUTHSERV_ID": SERVICE_NAME,
            "ISTOTA_EMAIL_CONFIRM_SENDER_MATCH": "off",
        }

    def compose_env(self) -> dict[str, str]:
        """Interpolation variables the mail overlay needs, which are not config.

        Distinct from `config_env()` and held to a different rule. Those point
        the *daemon* at a service and may only name variables the shipped
        generator reads; these are host paths a compose file binds, and compose
        resolves a relative bind against the first `-f` file's directory — which
        is `docker/`, not this package. An absolute path passed through the
        env-file is how `docker-compose.test.yml` already handles the rendered
        config directory, and this is the same mechanism.
        """
        return {
            "ISTOTA_TESTBED_MAIL_CONF": str(CONF_DIR),
            "ISTOTA_TESTBED_MAIL_CERT": str(self.cert_file),
            "ISTOTA_TESTBED_MAIL_KEY": str(self.key_file),
            "ISTOTA_TESTBED_MAIL_IMAGE": mail_image(),
        }

    def bind_stack(self, stack) -> None:
        """Learn the published host ports, once the containers are up.

        The ports are ephemeral and assigned by Docker at `up`, so this is the
        first moment they exist. Called by the pool for any service that
        declares it, the same way `NextcloudService` gets its `occ` handle.
        """
        self._stack = stack
        ports = {
            container_port: stack.published_port(SERVICE_NAME, container_port)
            for container_port in CONTAINER_PORTS
        }
        self._server = MailServer(
            host=LOOPBACK,
            imap_port=ports[IMAP_TLS_PORT],
            smtp_port=ports[SMTP_TLS_PORT],
            imap_starttls_port=ports[IMAP_STARTTLS_PORT],
            smtp_starttls_port=ports[SMTP_STARTTLS_PORT],
            ca_file=self.cert_file,
        )
        wait_reachable(
            self._server,
            logs=lambda: stack.logs(tail=40, service=SERVICE_NAME),
        )

    @property
    def server(self) -> MailServer:
        """The address a scenario drives the server through."""
        if self._server is None:
            raise RuntimeError(
                "the mail service has no published ports yet; "
                "StackPool binds them once the containers are up"
            )
        return self._server

    def session(self, account: str = BOT_ADDRESS) -> ImapSession:
        """An `ImapSession` against one of the two mailboxes."""
        return ImapSession(self.server, account=account)

    def send(self, **kwargs) -> str:
        """`send` against this server. Returns the `Message-ID`."""
        return send(self.server, **kwargs)

    def reset(self) -> None:
        """Empty both mailboxes.

        Total, unlike the Nextcloud reset one shape over: a mailbox has nothing
        the boot put there and nothing a scenario needs to keep, so an expunge
        really does return it to the state a fresh test expects. The daemon's
        own poll cursor is left alone deliberately — expunging does not rewind
        UIDs, so a cursor stored against this mailbox stays correct, and
        rewinding it would make the daemon re-ingest a purged test's mail.

        A no-op before the stack is up, because `reset` is also what a failed
        boot's cleanup path reaches for.
        """
        if self._server is None:
            return
        for account in ACCOUNTS:
            with ImapSession(self._server, account=account) as session:
                session.purge()

    def describe(self) -> str:
        """Both mailboxes' subjects, for `Stack.diagnostics`.

        A scenario that failed because no reply arrived and one that failed
        because the reply went to the wrong address print the same task row.
        This is what tells them apart.
        """
        if self._server is None:
            return "mail: no published ports (the stack never came up)"
        lines = []
        for account in ACCOUNTS:
            try:
                with ImapSession(self._server, account=account) as session:
                    found = session.fetch_new_since(0)
            except Exception as exc:  # noqa: BLE001 - a diagnostic never raises
                lines.append(f"  {account}: unreadable ({type(exc).__name__}: {exc})")
                continue
            if not found:
                lines.append(f"  {account}: empty")
            for message in found:
                lines.append(
                    f"  {account}: uid={message.uid} from={message.sender!r} "
                    f"subject={message.subject!r}"
                )
        return "mail:\n" + "\n".join(lines)

    def close(self) -> None:
        """Nothing to release. Compose owns the container.

        Idempotent by being empty, which is the honest implementation rather
        than a missing one: this object opened no socket and started no process.
        """
        self._stack = None
