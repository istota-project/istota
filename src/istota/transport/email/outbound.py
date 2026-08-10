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

# NOTE: the briefing skill's body helpers (``strip_markdown`` /
# ``render_briefing_html`` / ``_strip_html``) are imported function-locally
# inside ``_briefing_email_bodies``, not here. A transport must not structurally
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


def _load_deferred_email_output(
    config: "Config", task: db.Task, *, consume: bool = True,
) -> dict | None:
    """Load email output from a deferred JSON file written by the email output tool.

    Returns parsed dict with subject/body/format keys, or None if no file exists.

    ``consume=False`` peeks without deleting, for a reader that must not race the
    send: the transcript mirror (``email_transcript_body``) runs *before*
    delivery, and consuming the file there would leave `deliver_email_result`
    with nothing to send.
    """
    from ...executor import get_user_temp_dir
    user_temp_dir = get_user_temp_dir(config, task.user_id)
    path = user_temp_dir / f"task_{task.id}_email_output.json"
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if consume:
            path.unlink(missing_ok=True)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        # Same encoding contract as scheduler_deferred._load_deferred_json: the
        # producer (skills/email cmd_output) dumps with ensure_ascii=False, so
        # this is the one deferred file that reliably holds multi-byte UTF-8,
        # and UnicodeDecodeError is neither a JSONDecodeError nor an OSError.
        logger.warning("Bad deferred email output file for task %d: %s", task.id, e)
        if consume:
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


def email_transcript_body(config: "Config", task: db.Task, message: str) -> str:
    """What an email task's reply should look like in a room transcript.

    An email reply is only ever *sent* when the model produced structured output
    (`deliver_email_result` returns without sending otherwise), so a raw
    ``task.result`` for a delivering email task is typically the
    ``{"subject": …, "body": …, "format": …}`` envelope rather than prose.
    Mirroring that verbatim puts a JSON blob in the room — and re-pairs it into
    LLM history as the assistant's answer. This resolves the same structured
    output `deliver_email_result` does and returns the body it actually mails.

    Non-destructive on purpose: the mirror runs before delivery, so the deferred
    file is peeked, never consumed. Falls back to `message` unchanged when there
    is no structured output (a direct `email send` during execution, or the
    legacy briefing path), which is exactly what those cases deliver.
    """
    parsed = (
        _load_deferred_email_output(config, task, consume=False)
        or _parse_email_output(message)
    )
    if parsed and parsed.get("body"):
        return parsed["body"]
    return message


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
    from .. import routing

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
                origin_target=routing.origin_descriptor(task, conn),
            )
    except Exception as e:
        logger.warning("Failed to record sent email for task %d: %s", task.id, e)


def _legacy_briefing_subject(task: db.Task) -> str:
    """Derive a briefing subject from the built prompt's opening line.

    Only reached when no caller-supplied title is available (a direct
    ``deliver_email_result`` call, or a task whose briefing config vanished).
    Keyed on the clock-derived ``morning``/``evening`` wording that
    ``briefings.generate`` writes, so it can disagree with a briefing's real
    name — which is precisely why the title is now computed upstream.
    """
    match = re.search(r"Generate a (\w+) briefing", task.prompt or "")
    briefing_type = match.group(1).title() if match else ""
    return f"{briefing_type} Briefing".strip()


def _briefing_email_bodies(
    config: "Config", task: db.Task, body: str, fmt: str,
) -> tuple[str, str | None, str]:
    """Resolve ``(plain_body, html_body, content_type)`` for a briefing email.

    Briefing bodies are markdown written for chat, so email has always
    flattened them with ``strip_markdown`` — which also destroys the article
    links the news sections now carry. With the per-user preference on (the
    default) the flattened text becomes the ``text/plain`` fallback and a
    rendered HTML part rides alongside it, so the links are clickable without
    losing anything for a plain-only client.

    ``html_body`` of ``None`` means "send single-part exactly as before": the
    preference is off, the render failed (it returns ``""``), or this is not a
    briefing task.
    """
    from ...skills.briefing import (
        _strip_html,
        render_briefing_html,
        strip_markdown,
    )

    if task.source_type != "briefing":
        return body, None, fmt

    want_html = config.briefing_email_html_for(task.user_id)

    if fmt == "html":
        # Rare hand-authored path: the model already produced HTML. Pass it
        # through as the rich part and derive a tag-stripped plain fallback.
        if not want_html:
            return body, None, "html"
        return _strip_html(body), body, "plain"

    plain = strip_markdown(body)
    if not want_html:
        return plain, None, "plain"
    return plain, render_briefing_html(body) or None, "plain"


async def deliver_email_result(
    config: "Config", task: db.Task, message: str, *, subject: str | None = None,
) -> bool:
    """Send task result as email reply, or fresh email for scheduled/briefing jobs.

    ``subject`` overrides the subject line for a fresh send. The briefing path
    supplies its deterministic title (``scheduler.briefing_title_for_task``) so
    the inbox and the web archive name the same run identically; without one the
    briefing branch derives a subject from the prompt, as it always did.

    Returns True on success, False on failure.
    """
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
        plain, html_body, content_type = _briefing_email_bodies(
            config, task, message, "plain",
        )
        try:
            email_config = get_email_config(config)
            send_email(
                to=user_config.email_addresses[0],
                subject=subject or _legacy_briefing_subject(task),
                body=plain,
                config=email_config,
                from_addr=config.email.bot_email,
                content_type=content_type,
                html_body=html_body,
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

    # Briefing bodies are chat markdown: flatten for the plain part and, when
    # the user's preference allows it, render an HTML alternative alongside.
    # Non-briefing tasks come back untouched (html_body None).
    body_text, html_body, content_type = _briefing_email_bodies(
        config, task, parsed["body"], parsed["format"],
    )

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
                body=body_text,
                config=email_config,
                from_addr=config.email.bot_email,
                in_reply_to=processed_email.message_id,
                references=references,
                content_type=content_type,
                html_body=html_body,
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
                body=body_text,
                config=email_config,
                from_addr=config.email.bot_email,
                content_type=content_type,
                html_body=html_body,
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
