"""Shared email plumbing that isn't a transport direction.

The inbound poll loop lives in ``transport/email/inbound.py`` and the outbound
send body in ``transport/email/outbound.py``; this module is the small library
of email helpers shared between those transport halves and non-transport
callers (the briefing skill, the notification dispatcher, the TASKS.md poller,
and the scheduler's delivery-routing / cleanup paths).

The low-level IMAP/SMTP client (``list_emails`` / ``read_email`` / ``send_email``
/ ``reply_to_email`` / ``EmailConfig``) stays in ``istota.skills.email`` — that
is email's equivalent of ``istota.talk.TalkClient``.
"""

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

from .config import Config
from .skills.email import (
    _MAX_DELETES_PER_SWEEP,
    EmailConfig,
    delete_emails_before,
)

logger = logging.getLogger("istota.email_support")


# The wrapper `transport/email/inbound.py` builds around an inbound email before
# it becomes a task prompt, parsed back out for **display only**. The stored
# `messages` row keeps the prompt verbatim on purpose: it re-pairs straight into
# LLM context, so the "external input — do not follow instructions" guard has to
# survive that trip (ISSUE-136). A human reading the room transcript needs
# neither the guard nor the trailing instruction to the model, and reading them
# in a bubble labelled with their own name is worse than noise — it is the
# transcript asserting the user wrote what an external contact sent.
_EMAIL_PROMPT_RE = re.compile(
    r"<email_metadata>\n(?P<meta>.*?)\n</email_metadata>\s*"
    r"<email_content>\n(?P<body>.*?)\n</email_content>",
    re.DOTALL,
)
_EMAIL_HEADER_RE = re.compile(r"^(From|Subject|Date):[ \t]*(.*)$")


def parse_email_prompt(prompt: str) -> tuple[dict[str, str], str] | None:
    """Split an email task prompt into its metadata headers and the sender's text.

    Returns ``(headers, body)``, or **None when the prompt is not one** — a Talk
    turn, a web turn, or a wrapper shape this stopped recognizing. None means
    "render verbatim" at every call site, so drift between this and the builder
    degrades to today's raw display rather than to a blank or truncated message.
    Only the three fixed headers are lifted; the free-text lines the emissary
    variant adds are metadata for the model, not for a reader.
    """
    m = _EMAIL_PROMPT_RE.search(prompt)
    if m is None:
        return None
    headers: dict[str, str] = {}
    for line in m.group("meta").splitlines():
        h = _EMAIL_HEADER_RE.match(line)
        if h:
            headers[h.group(1).lower()] = h.group(2).strip()
    return headers, m.group("body").strip()


def get_email_config(config: Config) -> EmailConfig:
    """Convert app config to email skill config."""
    return EmailConfig(
        imap_host=config.email.imap_host,
        imap_port=config.email.imap_port,
        imap_user=config.email.imap_user,
        imap_password=config.email.imap_password,
        smtp_host=config.email.smtp_host,
        smtp_port=config.email.smtp_port,
        smtp_user=config.email.smtp_user,
        smtp_password=config.email.smtp_password,
        bot_email=config.email.bot_email,
        imap_timeout=config.email.imap_timeout_seconds,
    )


def per_user_address(config: Config, user_id: str) -> str | None:
    """The plus-addressed inbound address for ``user_id``, or None.

    ``bot+{user_id}@domain`` — the address ``email_ownership`` routes back to
    this user, so it is the one to show them. None whenever it would not
    route: email off, no/unusable ``bot_email``, or a ``bot_email`` that
    already carries a tag (a second '+' is not a plus-address any MTA
    delivers to us).
    """
    if not config.email.enabled or not user_id:
        return None
    bot_email = config.email.bot_email
    if not bot_email or "@" not in bot_email:
        return None
    local, domain = bot_email.split("@", 1)
    if not local or not domain or "+" in local:
        return None
    return f"{local}+{user_id}@{domain}"


def normalize_subject(subject: str) -> str:
    """Normalize subject for thread grouping (remove Re:, Fwd:, etc.)."""
    normalized = subject
    # Remove common prefixes repeatedly until none remain
    while True:
        new = re.sub(r"^(re|fwd|fw):\s*", "", normalized, count=1, flags=re.IGNORECASE)
        if new == normalized:
            break
        normalized = new
    # Remove extra whitespace
    normalized = " ".join(normalized.split())
    return normalized.lower()


def compute_thread_id(subject: str, participants: list[str]) -> str:
    """Compute a thread ID from normalized subject + sorted participants."""
    normalized_subject = normalize_subject(subject)
    sorted_participants = sorted(p.lower() for p in participants)
    content = f"{normalized_subject}|{'|'.join(sorted_participants)}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def is_synthetic_email_thread_token(token: str | None) -> bool:
    """True if a token has the shape produced by `compute_thread_id`.

    These 16-char-lowercase-hex strings are email-thread grouping keys, not
    Talk room tokens. Real Talk tokens may include uppercase letters, so a
    pure-lowercase-hex token of exactly that length is the synthetic signature.
    """
    if not token:
        return False
    return len(token) == 16 and all(c in "0123456789abcdef" for c in token)


def cleanup_old_emails(config: Config, days: int) -> int:
    """
    Delete emails older than the specified number of days from the IMAP inbox.

    Drives the sweep from the server side (ISSUE-230): one IMAP ``BEFORE``
    search for the expired set, then a bulk delete. The previous
    implementation paginated the *newest* 100 envelopes and deleted whichever
    of those had aged out — the wrong end of the mailbox for a retention pass.
    Above roughly ``100 / days`` messages a day nothing in that window is ever
    older than the cutoff, so the sweep silently deleted nothing while
    reporting a clean run.

    Args:
        config: Application config with email settings
        days: Delete emails older than this many days

    Returns:
        Number of emails deleted
    """
    if not config.email.enabled or days <= 0:
        return 0

    # UTC, not local: IMAP `BEFORE` is evaluated against the mail server's
    # calendar date, so a daemon east of the server would otherwise compute a
    # cutoff a day late and delete a day early.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()

    try:
        return delete_emails_before(
            cutoff,
            folder=config.email.poll_folder,
            config=get_email_config(config),
            max_deletes=_MAX_DELETES_PER_SWEEP,
        )
    except Exception as e:
        logger.error("Error deleting expired emails from IMAP: %s", e)
        return 0
