"""Email operations using imap-tools and smtplib.

Also provides a CLI for sending email directly from Claude Code:
    python -m istota.skills.email send --to <addr> --subject <subj> --body <body> [--html]
"""

import argparse
import json
import os
import smtplib
import ssl
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

from imap_tools import AND, MailBox, MailboxLoginError


@dataclass
class EmailEnvelope:
    id: str
    subject: str
    sender: str
    date: str
    is_read: bool


@dataclass
class Email:
    id: str
    subject: str
    sender: str
    date: str
    body: str
    attachments: list[str]
    message_id: str | None = None  # RFC 5322 Message-ID for threading
    references: str | None = None  # RFC 5322 References header for thread chain


@dataclass
class EmailConfig:
    """Email configuration for IMAP/SMTP access."""
    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str
    smtp_host: str
    smtp_port: int
    smtp_user: str | None = None
    smtp_password: str | None = None
    bot_email: str = ""

    @property
    def effective_smtp_user(self) -> str:
        return self.smtp_user or self.imap_user

    @property
    def effective_smtp_password(self) -> str:
        return self.smtp_password or self.imap_password


def _sanitize_header(value: str) -> str:
    """Strip newlines from header values to prevent injection."""
    return value.replace("\r", " ").replace("\n", " ").strip()


def _get_mailbox(config: EmailConfig) -> MailBox:
    """Create a MailBox connection based on config."""
    # Port 993 uses implicit TLS, port 143 uses STARTTLS
    if config.imap_port == 993:
        return MailBox(config.imap_host, port=config.imap_port)
    else:
        return MailBox(config.imap_host, port=config.imap_port, starttls=True)


def _generate_message_id(domain: str) -> str:
    """Generate a unique Message-ID for an email."""
    unique_id = uuid.uuid4().hex
    return f"<{unique_id}@{domain}>"


def list_emails(
    folder: str = "INBOX",
    limit: int = 20,
    config: EmailConfig | None = None,
    # Legacy parameter for backwards compatibility, ignored
    account: str = "istota",
) -> list[EmailEnvelope]:
    """List email envelopes in a folder."""
    if config is None:
        raise ValueError("config is required")

    with _get_mailbox(config) as mailbox:
        mailbox.login(config.imap_user, config.imap_password)
        mailbox.folder.set(folder)

        envelopes = []
        for msg in mailbox.fetch(limit=limit, reverse=True, mark_seen=False):
            envelopes.append(EmailEnvelope(
                id=msg.uid,
                subject=msg.subject or "(no subject)",
                sender=msg.from_ or "unknown",
                date=msg.date_str or "",
                is_read="\\Seen" in msg.flags,
            ))

        return envelopes


def read_email(
    email_id: str,
    folder: str = "INBOX",
    config: EmailConfig | None = None,
    envelope: EmailEnvelope | None = None,
    # Legacy parameter for backwards compatibility, ignored
    account: str = "istota",
) -> Email:
    """Read a specific email by UID."""
    if config is None:
        raise ValueError("config is required")

    with _get_mailbox(config) as mailbox:
        mailbox.login(config.imap_user, config.imap_password)
        mailbox.folder.set(folder)

        # Fetch specific email by UID
        for msg in mailbox.fetch(AND(uid=email_id), mark_seen=False):
            # Get Message-ID header for threading
            message_id = msg.headers.get("message-id")
            if isinstance(message_id, tuple):
                message_id = message_id[0] if message_id else None

            # Get References header for thread chain
            references = msg.headers.get("references")
            if isinstance(references, tuple):
                references = references[0] if references else None

            return Email(
                id=msg.uid,
                subject=msg.subject or "(no subject)",
                sender=msg.from_ or "unknown",
                date=msg.date_str or "",
                body=msg.text or msg.html or "",
                attachments=[att.filename for att in msg.attachments if att.filename],
                message_id=message_id,
                references=references,
            )

    raise RuntimeError(f"Email {email_id} not found in {folder}")


def download_attachments(
    email_id: str,
    target_dir: Path,
    folder: str = "INBOX",
    config: EmailConfig | None = None,
    # Legacy parameters for backwards compatibility, ignored
    himalaya_downloads_dir: Path | None = None,
    account: str | None = None,
) -> list[Path]:
    """
    Download attachments for an email directly to target_dir.

    Args:
        email_id: The email UID to download attachments from
        target_dir: Directory to save attachments to
        folder: IMAP folder name
        config: Email configuration

    Returns:
        List of paths to downloaded attachment files
    """
    if config is None:
        raise ValueError("config is required")

    target_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    with _get_mailbox(config) as mailbox:
        mailbox.login(config.imap_user, config.imap_password)
        mailbox.folder.set(folder)

        for msg in mailbox.fetch(AND(uid=email_id), mark_seen=False):
            for att in msg.attachments:
                if att.filename:
                    file_path = target_dir / att.filename
                    file_path.write_bytes(att.payload)
                    downloaded.append(file_path)

    return downloaded


def send_email(
    to: str,
    subject: str,
    body: str,
    config: EmailConfig | None = None,
    from_addr: str | None = None,
    content_type: str = "plain",
    # Legacy parameter for backwards compatibility, ignored
    account: str = "istota",
) -> None:
    """Send an email."""
    if config is None:
        raise ValueError("config is required")

    from_address = from_addr or config.bot_email
    domain = from_address.split("@")[-1] if "@" in from_address else "localhost"

    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = _sanitize_header(subject)
    msg["From"] = from_address
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = _generate_message_id(domain)
    msg.set_content(body, subtype=content_type)

    _send_smtp(msg, config)


def reply_to_email(
    to_addr: str,
    subject: str,
    body: str,
    config: EmailConfig | None = None,
    from_addr: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    content_type: str = "plain",
    # Legacy parameter for backwards compatibility, ignored
    account: str = "istota",
) -> None:
    """
    Send a reply email with proper threading headers.

    Args:
        to_addr: Recipient address (original sender)
        subject: Original subject (will add Re: if needed)
        body: Reply body text
        config: Email configuration
        from_addr: Sender address
        in_reply_to: Message-ID of the email being replied to (for threading)
        references: References header (Message-IDs of thread, for threading)
        content_type: Email content type - "plain" or "html"
    """
    if config is None:
        raise ValueError("config is required")

    # Build reply subject
    reply_subject = subject
    if not reply_subject.lower().startswith("re:"):
        reply_subject = f"Re: {reply_subject}"
    reply_subject = _sanitize_header(reply_subject)

    from_address = from_addr or config.bot_email
    domain = from_address.split("@")[-1] if "@" in from_address else "localhost"

    msg = EmailMessage()
    msg["To"] = to_addr
    msg["Subject"] = reply_subject
    msg["From"] = from_address
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = _generate_message_id(domain)

    # Threading headers (sanitize to strip folded newlines from original email)
    if in_reply_to:
        msg["In-Reply-To"] = _sanitize_header(in_reply_to)
    if references:
        msg["References"] = _sanitize_header(references)
    elif in_reply_to:
        # If no references but we have in_reply_to, use that as references
        msg["References"] = _sanitize_header(in_reply_to)

    msg.set_content(body, subtype=content_type)

    _send_smtp(msg, config)


def _send_smtp(msg: EmailMessage, config: EmailConfig) -> None:
    """Send an email message via SMTP and save to Sent folder."""
    # Port 587 typically uses STARTTLS, port 465 uses implicit TLS
    if config.smtp_port == 465:
        # Implicit TLS
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, context=context) as server:
            server.login(config.effective_smtp_user, config.effective_smtp_password)
            server.send_message(msg)
    else:
        # STARTTLS (typically port 587)
        with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
            server.starttls()
            server.login(config.effective_smtp_user, config.effective_smtp_password)
            server.send_message(msg)

    # Save a copy to Sent Items folder via IMAP
    _save_to_sent(msg, config)


def _save_to_sent(msg: EmailMessage, config: EmailConfig) -> None:
    """Save a sent email to the Sent Items folder via IMAP."""
    try:
        with _get_mailbox(config) as mailbox:
            mailbox.login(config.imap_user, config.imap_password)
            # Append the message to Sent Items folder
            mailbox.append(msg.as_bytes(), "Sent Items", dt=None, flag_set=["\\Seen"])
    except Exception:
        # Don't fail the send if saving to Sent fails
        pass


def search_emails(
    query: str,
    folder: str = "INBOX",
    limit: int = 20,
    config: EmailConfig | None = None,
    # Legacy parameter for backwards compatibility, ignored
    account: str = "istota",
) -> list[EmailEnvelope]:
    """
    Search emails using IMAP search syntax.

    Supports simple patterns:
    - from:address - search by sender
    - subject:text - search by subject
    """
    if config is None:
        raise ValueError("config is required")

    with _get_mailbox(config) as mailbox:
        mailbox.login(config.imap_user, config.imap_password)
        mailbox.folder.set(folder)

        # Parse query into imap-tools criteria
        criteria = _parse_search_query(query)

        envelopes = []
        for msg in mailbox.fetch(criteria, limit=limit, reverse=True, mark_seen=False):
            envelopes.append(EmailEnvelope(
                id=msg.uid,
                subject=msg.subject or "(no subject)",
                sender=msg.from_ or "unknown",
                date=msg.date_str or "",
                is_read="\\Seen" in msg.flags,
            ))

        return envelopes


def _parse_search_query(query: str):
    """Parse a simple search query into imap-tools AND criteria."""
    # Simple parsing for common patterns
    query = query.strip()

    if query.startswith("from:"):
        value = query[5:].strip().strip('"')
        return AND(from_=value)
    elif query.startswith("subject:"):
        value = query[8:].strip().strip('"')
        return AND(subject=value)
    else:
        # Treat as subject search by default
        return AND(subject=query)


def get_emails_from_senders(
    senders: list[str],
    max_age_hours: int = 6,
    folder: str = "INBOX",
    config: EmailConfig | None = None,
    # Legacy parameter for backwards compatibility, ignored
    account: str | None = None,
) -> list[EmailEnvelope]:
    """Get recent emails from specific senders (for news briefings)."""
    if config is None:
        raise ValueError("config is required")

    # Get emails and filter by sender and age
    emails = list_emails(folder=folder, limit=100, config=config)

    cutoff = datetime.now().timestamp() - (max_age_hours * 3600)
    senders_lower = [s.lower() for s in senders]
    recent = []

    for email in emails:
        # Check sender
        if email.sender.lower() not in senders_lower:
            continue

        # Check age
        try:
            email_time = parsedate_to_datetime(email.date).timestamp()
            if email_time >= cutoff:
                recent.append(email)
        except Exception:
            # If we can't parse the date, include it to be safe
            recent.append(email)

    return recent


def _parse_email_date(date_str: str) -> datetime | None:
    """
    Parse email date from various formats.

    Handles:
    - RFC 2822: "Tue, 27 Jan 2026 11:19:17 +0000"
    - ISO 8601: "2026-01-27 14:47+00:00" or "2026-01-26 08:17-08:00"

    Returns:
        Parsed datetime or None if unparseable
    """
    # Try RFC 2822 first (standard email format)
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        pass

    # Try ISO 8601 format
    try:
        # Handle "2026-01-27 14:47+00:00" format
        # Python's fromisoformat needs 'T' separator, not space
        iso_str = date_str.replace(" ", "T")
        return datetime.fromisoformat(iso_str)
    except Exception:
        pass

    return None


def get_newsletters(
    sources: list[dict],
    lookback_hours: int = 12,
    folder: str = "INBOX",
    config: EmailConfig | None = None,
    # Legacy parameter for backwards compatibility, ignored
    account: str = "istota",
) -> list[EmailEnvelope]:
    """
    Get recent newsletter emails from configured sources.

    Supports two source types:
    - {"type": "email", "value": "newsletter@example.com"} - match exact sender
    - {"type": "domain", "value": "example.com"} - match sender domain

    Args:
        sources: List of source dictionaries with type and value
        lookback_hours: Maximum age of emails to include
        folder: IMAP folder to search
        config: Email configuration

    Returns:
        List of matching EmailEnvelope objects
    """
    if config is None:
        raise ValueError("config is required")

    if not sources:
        return []

    # Separate sources by type
    email_senders = []
    domains = []
    for source in sources:
        source_type = source.get("type", "email")
        value = source.get("value", "")
        if not value:
            continue
        if source_type == "domain":
            domains.append(value.lower())
        else:
            email_senders.append(value.lower())

    # Fetch recent emails - get a larger batch to filter
    all_emails = list_emails(folder=folder, limit=100, config=config)

    # Filter by age and sender
    cutoff = datetime.now().timestamp() - (lookback_hours * 3600)
    recent = []
    for email in all_emails:
        # Parse date - skip emails we can't date or that are too old
        email_dt = _parse_email_date(email.date)
        if email_dt is None:
            # Can't parse date - skip to avoid including very old emails
            continue
        if email_dt.timestamp() < cutoff:
            continue

        # Check if sender matches any source
        sender_lower = email.sender.lower()

        # Check exact email match
        if sender_lower in email_senders:
            recent.append(email)
            continue

        # Check domain match (supports subdomains - news.bloomberg.com matches bloomberg.com)
        sender_domain = sender_lower.split("@")[-1] if "@" in sender_lower else ""
        for domain in domains:
            if sender_domain == domain or sender_domain.endswith("." + domain):
                recent.append(email)
                break

    return recent


def delete_email(
    email_id: str,
    folder: str = "INBOX",
    config: EmailConfig | None = None,
    # Legacy parameter for backwards compatibility, ignored
    account: str = "istota",
) -> bool:
    """
    Delete an email by UID.

    Args:
        email_id: The email UID to delete
        folder: IMAP folder name
        config: Email configuration

    Returns:
        True if deletion succeeded, False otherwise
    """
    if config is None:
        raise ValueError("config is required")

    try:
        with _get_mailbox(config) as mailbox:
            mailbox.login(config.imap_user, config.imap_password)
            mailbox.folder.set(folder)
            mailbox.delete(email_id)
            return True
    except Exception:
        return False


def _config_from_env() -> EmailConfig:
    """Build EmailConfig from environment variables."""
    smtp_host = os.environ.get("SMTP_HOST", "")
    if not smtp_host:
        raise ValueError("SMTP_HOST environment variable is required")

    return EmailConfig(
        imap_host=os.environ.get("IMAP_HOST", ""),
        imap_port=int(os.environ.get("IMAP_PORT", "993")),
        imap_user=os.environ.get("IMAP_USER", ""),
        imap_password=os.environ.get("IMAP_PASSWORD", ""),
        smtp_host=smtp_host,
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_user=os.environ.get("SMTP_USER", ""),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        bot_email=os.environ.get("SMTP_FROM", ""),
    )


def cmd_output(args):
    """Write structured email output to a deferred file for the scheduler.

    Instead of the model producing inline JSON (which risks transcription
    corruption like smart-quote substitution), it calls this command. The
    scheduler reads the file and handles delivery.
    """
    task_id = os.environ.get("ISTOTA_TASK_ID", "")
    deferred_dir = os.environ.get("ISTOTA_DEFERRED_DIR", "")
    if not task_id or not deferred_dir:
        raise ValueError("ISTOTA_TASK_ID and ISTOTA_DEFERRED_DIR must be set")

    # Read body from file if specified
    if args.body_file:
        body = Path(args.body_file).read_text()
    else:
        body = args.body
    if not body:
        raise ValueError("Either --body or --body-file is required")

    fmt = "html" if args.html else "plain"
    data = {
        "subject": args.subject or None,
        "body": body,
        "format": fmt,
    }

    out_path = Path(deferred_dir) / f"task_{task_id}_email_output.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False))

    return {"status": "ok", "file": str(out_path)}


def cmd_send(args):
    """Send an email via CLI."""
    config = _config_from_env()

    # Read body from file if --body-file specified
    if args.body_file:
        body = Path(args.body_file).read_text()
    else:
        body = args.body

    if not body:
        raise ValueError("Either --body or --body-file is required")

    content_type = "html" if args.html else "plain"

    send_email(
        to=args.to,
        subject=args.subject,
        body=body,
        config=config,
        content_type=content_type,
    )

    return {"status": "ok", "to": args.to, "subject": args.subject}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.email",
        description="Email operations CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # send
    p_send = sub.add_parser("send", help="Send an email")
    p_send.add_argument("--to", required=True, help="Recipient email address")
    p_send.add_argument("--subject", required=True, help="Email subject")
    p_send.add_argument("--body", help="Email body text")
    p_send.add_argument("--body-file", help="Read body from file (for large content)")
    p_send.add_argument("--html", action="store_true", help="Send as HTML email")

    # output — write email response for scheduler delivery (replaces inline JSON)
    p_output = sub.add_parser("output", help="Write email response for scheduler delivery")
    p_output.add_argument("--subject", help="Email subject (optional for replies)")
    p_output.add_argument("--body", help="Email body text")
    p_output.add_argument("--body-file", help="Read body from file (for large content)")
    p_output.add_argument("--html", action="store_true", help="Send as HTML email")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "send": cmd_send,
        "output": cmd_output,
    }

    try:
        result = commands[args.command](args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
