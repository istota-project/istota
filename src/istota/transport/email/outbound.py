"""Email send body — the EmailTransport outbound half.

Owns turning a task result into an outbound email: structured-output parsing
(deferred file preferred over inline JSON), thread-reply vs fresh-send routing,
and recording the sent message for emissary thread matching.
``EmailTransport.deliver`` calls ``deliver_email_result``; the scheduler's
``post_result_to_email`` is a thin shim over the transport, mirroring
``post_result_to_talk`` / ``TalkTransport.deliver``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from ... import db
from ...email_support import get_email_config
from ...skills.email import reply_to_email, send_email

# NOTE: ``strip_markdown`` (from the briefing skill) is imported function-locally
# inside ``deliver_email_result``, not here. A transport must not structurally
# depend on a sibling feature-skill at import time — keeping it lazy stops
# ``import istota.transport`` from eagerly dragging in ``skills.briefing`` and
# averts a latent import cycle (the email client + storage imports above are the
# transport's own surface, analogous to talk/inbound.py importing TalkClient).

if TYPE_CHECKING:
    from ...config import Config

logger = logging.getLogger("istota.transport.email.outbound")


def _parse_email_output(message: str) -> dict | None:
    """
    Parse Claude Code's email output as JSON.

    Expected format:
        {"subject": "...", "body": "...", "format": "plain"|"html"}

    Handles common Claude quirks:
    - Markdown code fences (```json ... ```)
    - Preamble text before the JSON object
    - Trailing text after the JSON object

    Returns None if no structured email JSON is found — this prevents
    double-sending when Claude already sent the email via `email send`.
    """
    def _try_parse(text: str) -> dict | None:
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "body" in data and "format" in data:
                fmt = data["format"]
                if fmt not in ("plain", "html"):
                    fmt = "plain"
                return {
                    "subject": data.get("subject"),
                    "body": data["body"],
                    "format": fmt,
                }
        except (json.JSONDecodeError, TypeError, KeyError):
            return None

    text = message.strip()

    # Try 1: parse as-is
    result = _try_parse(text)
    if result:
        return result

    # Try 2: strip markdown code fences
    if "```" in text:
        lines = text.split("\n")
        # Find fenced block
        start = None
        end = None
        for i, line in enumerate(lines):
            if line.strip().startswith("```") and start is None:
                start = i
            elif line.strip() == "```" and start is not None:
                end = i
                break
        if start is not None and end is not None:
            fenced = "\n".join(lines[start + 1:end]).strip()
            result = _try_parse(fenced)
            if result:
                return result

    # Try 3: find outermost { ... } in the message
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace:last_brace + 1]
        result = _try_parse(candidate)
        if result:
            return result

    # Try 4: normalize Unicode smart quotes to ASCII and retry.
    # Models sometimes silently replace ASCII quotes with smart quotes
    # (U+201C/U+201D/U+2018/U+2019) when echoing JSON, which breaks parsing.
    _SMART_QUOTE_MAP = {
        "“": '"',  # left double
        "”": '"',  # right double
        "‘": "'",  # left single
        "’": "'",  # right single
    }
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace:last_brace + 1]
        for smart, ascii_char in _SMART_QUOTE_MAP.items():
            candidate = candidate.replace(smart, ascii_char)
        result = _try_parse(candidate)
        if result:
            logger.warning("Email JSON required smart-quote normalization to parse")
            return result

    # No structured email JSON found.  Log a warning if it looks like broken
    # JSON — helps diagnose transcription corruption.  Return None so the
    # caller knows there is no structured output (prevents double-send when
    # Claude already sent the email directly via `email send`).
    if first_brace != -1 and '"format"' in text:
        logger.warning(
            "Email output looks like malformed JSON but could not be parsed"
        )
    return None


def _load_deferred_email_output(config: "Config", task: db.Task) -> dict | None:
    """Load email output from a deferred JSON file written by the email output tool.

    Returns parsed dict with subject/body/format keys, or None if no file exists.
    """
    from ...executor import get_user_temp_dir
    user_temp_dir = get_user_temp_dir(config, task.user_id)
    path = user_temp_dir / f"task_{task.id}_email_output.json"
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text())
        path.unlink(missing_ok=True)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Bad deferred email output file for task %d: %s", task.id, e)
        path.unlink(missing_ok=True)
        return None

    if not isinstance(data, dict) or "body" not in data or "format" not in data:
        logger.warning("Deferred email output file for task %d missing required fields", task.id)
        return None

    fmt = data["format"]
    if fmt not in ("plain", "html"):
        fmt = "plain"

    return {
        "subject": data.get("subject"),
        "body": data["body"],
        "format": fmt,
    }


def _record_sent_email(
    config: "Config",
    task: db.Task,
    message_id: str,
    to_addr: str,
    subject: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> None:
    """Record an outbound email for emissary thread matching (non-critical)."""
    try:
        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id=task.user_id,
                message_id=message_id,
                to_addr=to_addr,
                subject=subject,
                task_id=task.id,
                in_reply_to=in_reply_to,
                references=references,
                conversation_token=task.conversation_token,
                talk_delivery_token=task.talk_delivery_token,
            )
    except Exception as e:
        logger.warning("Failed to record sent email for task %d: %s", task.id, e)


async def deliver_email_result(config: "Config", task: db.Task, message: str) -> bool:
    """Send task result as email reply, or fresh email for scheduled/briefing jobs.

    Returns True on success, False on failure.
    """
    from ...skills.briefing import strip_markdown

    # Prefer deferred email output file (tool-based, no transcription risk)
    # over inline JSON parsing (legacy, subject to smart-quote corruption).
    # If neither source provides structured output, fall back to legacy briefing
    # path (raw model output stripped of markdown) for briefing tasks, or skip
    # sending for other tasks (Claude likely sent directly via `email send`).
    parsed = _load_deferred_email_output(config, task) or _parse_email_output(message)

    if parsed is None and task.source_type == "briefing":
        # Legacy path: model output is Talk-formatted text, send directly
        user_config = config.users.get(task.user_id)
        if not user_config or not user_config.email_addresses:
            logger.warning("No email address for user %s (task %d)", task.user_id, task.id)
            return False
        try:
            email_config = get_email_config(config)
            match = re.search(r"Generate a (\w+) briefing", task.prompt)
            briefing_type = match.group(1).title() if match else ""
            send_email(
                to=user_config.email_addresses[0],
                subject=f"{briefing_type} Briefing".strip(),
                body=strip_markdown(message),
                config=email_config,
                from_addr=config.email.bot_email,
                content_type="plain",
            )
            return True
        except Exception as e:
            logger.error("Failed to send briefing email (task %s): %s", task.id, e)
            return False
    if parsed is None:
        logger.info(
            "No structured email output for task %d; skipping scheduler delivery "
            "(email was likely sent directly during execution)",
            task.id,
        )
        return True

    # Safety net: strip markdown from briefing plain text emails (briefing content
    # is generated with Talk formatting; strip it for email delivery)
    if task.source_type == "briefing" and parsed["format"] == "plain":
        parsed["body"] = strip_markdown(parsed["body"])

    with db.get_db(config.db_path) as conn:
        processed_email = db.get_email_for_task(conn, task.id)

    if processed_email:
        # Reply to existing email thread
        try:
            email_config = get_email_config(config)

            # Build References: parent's references + parent's message_id (RFC 5322)
            if processed_email.references and processed_email.message_id:
                references = f"{processed_email.references} {processed_email.message_id}"
            elif processed_email.message_id:
                references = processed_email.message_id
            else:
                references = None

            # Use parsed subject if provided, otherwise keep original
            subject = parsed["subject"] if parsed["subject"] else (processed_email.subject or "")

            sent_message_id = reply_to_email(
                to_addr=processed_email.sender_email,
                subject=subject,
                body=parsed["body"],
                config=email_config,
                from_addr=config.email.bot_email,
                in_reply_to=processed_email.message_id,
                references=references,
                content_type=parsed["format"],
            )
            _record_sent_email(
                config, task, sent_message_id,
                to_addr=processed_email.sender_email,
                subject=subject,
                in_reply_to=processed_email.message_id,
                references=references,
            )
            return True
        except Exception as e:
            logger.error("Failed to send email reply (task %s): %s", task.id, e)
            return False
    else:
        # No original email — send fresh email to user (e.g., scheduled job)
        user_config = config.users.get(task.user_id)
        if not user_config or not user_config.email_addresses:
            logger.warning("No email address for user %s (task %d)", task.user_id, task.id)
            return False

        # Use parsed subject if provided, otherwise fall back to prompt excerpt
        subject = parsed["subject"] if parsed["subject"] else f"[{config.bot_name}] {task.prompt[:80]}"

        try:
            email_config = get_email_config(config)
            sent_message_id = send_email(
                to=user_config.email_addresses[0],
                subject=subject,
                body=parsed["body"],
                config=email_config,
                from_addr=config.email.bot_email,
                content_type=parsed["format"],
            )
            _record_sent_email(
                config, task, sent_message_id,
                to_addr=user_config.email_addresses[0],
                subject=subject,
            )
            return True
        except Exception as e:
            logger.error("Failed to send email (task %s): %s", task.id, e)
            return False
