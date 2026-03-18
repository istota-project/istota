"""Email polling and task creation."""

import hashlib
import logging
import re
import uuid
from datetime import datetime

from . import db
from .config import Config
from .skills.email import (
    EmailConfig,
    delete_email,
    download_attachments,
    list_emails,
    read_email,
)
from .storage import ensure_user_directories_v2, upload_file_to_inbox_v2

logger = logging.getLogger("istota.email_poller")


def _match_thread(conn, email) -> db.SentEmail | None:
    """Check if an inbound email is a reply to one of our sent emails.

    Checks In-Reply-To first (direct reply), then References (thread chain).
    Returns the matching SentEmail or None.
    """
    # In-Reply-To is typically a single Message-ID
    in_reply_to = None
    if hasattr(email, "message_id"):
        # email object from read_email — check for In-Reply-To in references
        pass

    # For imap-tools Email objects, In-Reply-To isn't directly exposed.
    # But References header contains the full thread chain including
    # In-Reply-To. We parse both from the email's headers.

    # Check references: split by whitespace to get individual Message-IDs
    if email.references:
        ref_ids = email.references.split()
        if ref_ids:
            # Check the last reference first (most likely the direct parent)
            match = db.find_sent_email_by_message_id(conn, ref_ids[-1])
            if match:
                return match
            # Fall back to checking all references
            match = db.find_sent_email_by_references(conn, ref_ids)
            if match:
                return match

    return None


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


def poll_emails(config: Config) -> list[int]:
    """
    Poll for new emails, create tasks for known senders.
    Returns list of created task_ids.
    """
    if not config.email.enabled:
        return []

    email_config = get_email_config(config)
    created_tasks = []

    # List recent emails
    try:
        envelopes = list_emails(
            folder=config.email.poll_folder,
            limit=50,
            config=email_config,
        )
    except Exception as e:
        logger.error("Error listing emails: %s", e)
        return []

    with db.get_db(config.db_path) as conn:
        for envelope in envelopes:
            # Skip already processed
            if db.is_email_processed(conn, envelope.id):
                continue

            # Skip bot's own emails
            if config.email.bot_email:
                if envelope.sender.lower() == config.email.bot_email.lower():
                    db.mark_email_processed(
                        conn,
                        email_id=envelope.id,
                        sender_email=envelope.sender,
                        subject=envelope.subject,
                    )
                    continue

            # Find user by sender email
            user_id = config.find_user_by_email(envelope.sender)

            # For unknown senders, check if this is a reply to a thread we initiated
            sent_email_match = None
            if not user_id:
                try:
                    email = read_email(
                        envelope.id,
                        folder=config.email.poll_folder,
                        config=email_config,
                        envelope=envelope,
                    )
                    sent_email_match = _match_thread(conn, email)
                except Exception as e:
                    logger.error("Error reading email %s for thread matching: %s", envelope.id, e)

                if sent_email_match:
                    # Route to the user who initiated the thread
                    user_id = sent_email_match.user_id
                    logger.info(
                        "Thread match: email from %s is a reply to sent email %s (user %s)",
                        envelope.sender, sent_email_match.message_id, user_id,
                    )
                else:
                    # Unknown sender, not a reply to our thread — discard
                    db.mark_email_processed(
                        conn,
                        email_id=envelope.id,
                        sender_email=envelope.sender,
                        subject=envelope.subject,
                    )
                    continue

            # Read full email content (if not already read during thread matching)
            if sent_email_match is None:
                try:
                    email = read_email(
                        envelope.id,
                        folder=config.email.poll_folder,
                        config=email_config,
                        envelope=envelope,
                    )
                except Exception as e:
                    logger.error("Error reading email %s: %s", envelope.id, e)
                    continue

            # Download attachments directly to target directory
            attachment_id = uuid.uuid4().hex[:8]
            attachment_dir = config.temp_dir / f"attachments_{attachment_id}"
            local_attachment_paths = download_attachments(
                envelope.id,
                target_dir=attachment_dir,
                folder=config.email.poll_folder,
                config=email_config,
            )

            # Upload attachments to user's Nextcloud inbox
            attachment_paths = []
            if local_attachment_paths:
                # Ensure user directories exist
                ensure_user_directories_v2(config, user_id)

                for local_path in local_attachment_paths:
                    # Add unique prefix to avoid filename collisions
                    remote_filename = f"{attachment_id}_{local_path.name}"
                    remote_path = upload_file_to_inbox_v2(
                        config,
                        user_id,
                        local_path,
                        remote_filename,
                    )
                    if remote_path:
                        attachment_paths.append(remote_path)
                    else:
                        # Fall back to local path if upload fails
                        attachment_paths.append(str(local_path))

            # Compute thread_id for conversation context
            participants = [envelope.sender, config.email.bot_email]
            thread_id = compute_thread_id(envelope.subject, participants)

            # Build prompt from email
            attachments_text = ""
            if attachment_paths:
                attachments_text = "\nAttachments (in Nextcloud):\n" + "\n".join(
                    f"  - {p}" for p in attachment_paths
                )

            # For emissary thread replies, include routing context in the prompt
            if sent_email_match:
                prompt = f"""Emissary email reply — an external contact has replied to an email you sent on behalf of this user.

From: {email.sender}
Subject: {email.subject}
Date: {email.date}
Original thread initiated by you (sent to: {sent_email_match.to_addr})
{attachments_text}

{email.body}

Notify the user about this reply and summarize its content. If the conversation requires a response, draft one for the user's approval."""
            else:
                prompt = f"""Email from: {email.sender}
Subject: {email.subject}
Date: {email.date}
{attachments_text}

{email.body}"""

            # Determine output target — emissary replies go to Talk
            output_target = None
            conversation_token = thread_id
            if sent_email_match:
                output_target = "talk"
                # Route to the Talk conversation where the original send was requested
                if sent_email_match.conversation_token:
                    conversation_token = sent_email_match.conversation_token

            # Create task with attachment paths (already strings from Nextcloud upload)
            attachment_strs = attachment_paths if attachment_paths else None
            task_id = db.create_task(
                conn,
                prompt=prompt,
                user_id=user_id,
                source_type="email",
                conversation_token=conversation_token,
                attachments=attachment_strs,
                output_target=output_target,
            )

            # Mark email as processed with task link
            db.mark_email_processed(
                conn,
                email_id=envelope.id,
                sender_email=envelope.sender,
                subject=envelope.subject,
                thread_id=thread_id,
                message_id=email.message_id,
                references=email.references,
                user_id=user_id,
                task_id=task_id,
            )

            created_tasks.append(task_id)
            logger.info("Created task %d from email '%s' by %s", task_id, envelope.subject, envelope.sender)

    return created_tasks


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
