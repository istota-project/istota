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
from datetime import datetime

from .config import Config
from .skills.email import EmailConfig, delete_email, list_emails

logger = logging.getLogger("istota.email_support")


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
    )


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

    Args:
        config: Application config with email settings
        days: Delete emails older than this many days

    Returns:
        Number of emails deleted
    """
    if not config.email.enabled or days <= 0:
        return 0

    email_config = get_email_config(config)

    try:
        envelopes = list_emails(
            folder=config.email.poll_folder,
            limit=100,
            config=email_config,
        )
    except Exception as e:
        logger.error("Error listing emails for cleanup: %s", e)
        return 0

    cutoff = datetime.now().timestamp() - (days * 24 * 3600)
    deleted_count = 0

    for envelope in envelopes:
        try:
            from email.utils import parsedate_to_datetime
            email_time = parsedate_to_datetime(envelope.date).timestamp()
            if email_time < cutoff:
                if delete_email(envelope.id, folder=config.email.poll_folder, config=email_config):
                    deleted_count += 1
        except Exception:
            # If we can't parse the date, skip this email
            continue

    return deleted_count
