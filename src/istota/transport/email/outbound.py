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

from istota import db
from istota.rooms import veto as room_veto
from istota.mail.support import get_email_config
from istota.lib.llm_json import find_fenced_block
from istota.notifications.resolvers import outbound_draft as draft_source
from istota.notifications.store import RaiseResult, deliver_pending
from ...skills.email import reply_to_email, send_email
from . import threads as email_threads

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
    fenced = find_fenced_block(text)
    if fenced:
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
    send: the file is the only copy of the composed body, so it is dropped by
    `_consume_deferred_email_output` once the message has gone out or been
    recorded as a draft, never at load time.
    """
    from ...executor import task_deferred_dir
    path = task_deferred_dir(config, task) / f"task_{task.id}_email_output.json"
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


def _consume_deferred_email_output(config: "Config", task: db.Task) -> None:
    """Drop the deferred output file once its message is accounted for.

    Called after the send has been attempted, or after a hold has stored the
    body in ``outbound_drafts`` — never on the path where the approval check
    itself failed, because there the file is the only surviving copy.
    """
    from ...executor import task_deferred_dir
    path = task_deferred_dir(config, task) / f"task_{task.id}_email_output.json"
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        # Losing the delete costs a stale file and a warning from
        # `_warn_unconsumed_deferred_files`, never a resend: nothing re-reads it
        # for a task that has already been delivered.
        logger.warning(
            "Could not remove deferred email output for task %d: %s", task.id, e,
        )


def _hold_if_unapproved(
    config: "Config",
    task: db.Task,
    *,
    to_addr: str,
    subject: str,
    body: str,
    html: bool,
    in_reply_to: str | None = None,
    references: str | None = None,
    cc_addrs: list[str] | None = None,
    room_token: str | None = None,
) -> tuple[bool, int | None]:
    """``(may_send, draft_id)`` for one outbound message on the delivery leg.

    ``cc_addrs`` are checked with ``to_addr``, so a reply-all holds when any
    recipient would; ``room_token`` names the room a held draft shows in when
    it is not the task's own (an approved post into an email thread room).

    ``(True, None)`` sends. ``(False, id)`` was held as a draft. ``(False,
    None)`` means the check could not run and the send is refused.

    **Why the check lives here as well as in the CLI verbs.** ``_outbound_gate``
    in the email skill covers ``send`` / ``reply``, which are the two verbs the
    model rarely reaches for on this path. It replies with ``email output``,
    which writes a deferred file and returns; the mail leaves later, from this
    function, through a branch that consulted no policy at all (ISSUE-246). The
    spec excluded ``output`` on the reasoning that its recipient had already
    cleared the *inbound* gate, and both halves of that fail: a plain ``yes`` at
    an inbound prompt authorizes one message and writes no trust row, and a
    reply on a thread from one of its people clears the inbound gate on the
    strength of that alone (ISSUE-234 narrowed the route to the correspondent;
    clearing it was never a grant).

    So this is the backstop that makes the guarantee true rather than
    conventional. It covers ``email output``, a hand-written deferred file, the
    scheduler's gap-case delivery, and any future path that reaches SMTP through
    the transport. The CLI checks stay: they refuse in-turn, in words the model
    can act on, which this one cannot do — by the time delivery runs the task has
    already reported success.

    A hold must not fail the task for that same reason, so the caller reports
    ``True``. A check that *cannot run* is different: nothing was sent and
    nothing was held, so there is no draft to recover from, and the caller
    reports a genuine delivery failure. Refusing rather than sending is the
    point — a gate that fails open on a busy database is not a gate.
    """
    from istota.mail.outbound_policy import effective_policy, recipients_require_hold

    # Resolved before any connection is opened, so `off` costs no database —
    # matching the skill-side gate, where the same ordering keeps an unreachable
    # DB from failing sends on an instance that deliberately switched this off.
    try:
        if effective_policy(config, task.user_id) == "off":
            return True, None
    except Exception as e:  # noqa: BLE001 — a policy we can't resolve is not "off"
        logger.error(
            "Outbound gate: could not resolve the approval policy for task %d "
            "(%s); refusing to send", task.id, e,
        )
        return False, None

    if config.users.get(task.user_id) is None:
        # Every authorization source hangs off the user's config, so an
        # unhydrated user holds all their mail. Said once here because the
        # symptom — an approval queue filling up — reads as a policy decision
        # rather than as the config problem it is.
        logger.warning(
            "Outbound gate: user %s has no config entry, so no address can be "
            "trusted and every message will be held", task.user_id,
        )

    from istota.mail import drafts
    from ...transport import routing

    try:
        with db.get_db(config.db_path) as conn:
            cc = list(cc_addrs or [])
            reason = recipients_require_hold(
                config, conn, task.user_id, [to_addr, *cc],
            )
            if reason is None:
                return True, None

            origin = routing.origin_descriptor(task, conn)
            room = (
                origin[len("room:"):]
                if origin and origin.startswith("room:")
                else None
            )
            if room_token:
                room = room_token
            try:
                draft_id = drafts.hold(
                    conn,
                    user_id=task.user_id,
                    task_id=task.id,
                    room_token=room,
                    to_addrs=[to_addr],
                    cc_addrs=cc,
                    bcc_addrs=[],
                    subject=subject or "",
                    body=body or "",
                    html=html,
                    # Snapshotted rather than re-derived: `release` sends from
                    # the row, and the inbound message it threads onto may be
                    # gone by the time the user answers.
                    in_reply_to=in_reply_to,
                    references=references,
                    attachments=[],
                    origin_target=origin,
                    hold_reason=reason,
                )
                # The durable record, on the gate's own connection inside the
                # transaction that just wrote the draft. Not inside
                # `drafts.hold`: its other caller is the email skill CLI, a
                # short-lived host-side subprocess the skill proxy spawns, and a
                # raise there would put `send_notification`'s Talk and ntfy
                # fan-out in that child process.
                held_notification = draft_source.write(
                    conn, task.user_id, draft_id=draft_id,
                    title=draft_source.title_for(to_addr),
                    body=draft_source.delivery_body_for(
                        subject, draft_id,
                        draft_source.visible_recipients([to_addr, *cc]),
                    ),
                    room_token=room,
                )
            except drafts.DraftError as e:
                # The decision was made and only the recording failed — an
                # unparseable recipient, most likely, since this leg's address
                # comes from an inbound header. Logged apart from the
                # could-not-decide case because it is deterministic: a replay
                # fails identically, so this is a message to go and look at
                # rather than a transient to retry.
                logger.error(
                    "Outbound gate: task %d's reply to %r must be held but "
                    "could not be recorded as a draft (%s); refusing to send. "
                    "The composed body is left in the task's deferred dir.",
                    task.id, to_addr, e,
                )
                return False, None
    except Exception as e:  # noqa: BLE001 — see the docstring: never fall through
        logger.error(
            "Outbound gate: the approval check could not run for task %d (%s); "
            "refusing to send to %s", task.id, e, to_addr,
        )
        return False, None

    logger.info(
        "Outbound gate: held task %d's reply to %s as draft %d (%s)",
        task.id, to_addr, draft_id, reason,
    )
    _announce_hold(
        config, task, to_addr=to_addr, subject=subject, draft_id=draft_id,
        notification=held_notification,
    )
    return False, draft_id


def _announce_hold(
    config: "Config", task: db.Task, *,
    to_addr: str, subject: str, draft_id: int,
    notification: "RaiseResult | None" = None,
) -> None:
    """Tell the user their reply is waiting, at the moment it is held.

    A hold in the CLI verbs returns a `held` envelope the model reads and
    reports in its own answer. This one has no such channel: delivery runs after
    the task finished, so the model has already told the user it replied, and
    that answer has already been posted to the room. Without an explicit notice
    the only remaining surfaces are `!drafts`, an inline card in a room the
    thread may not have, and the 24-hour stale-draft nag — so a reply held on
    the path ISSUE-246 was filed about would sit silent for a day behind an
    answer claiming it had been sent.

    Sent outside the gate's own transaction: a notification routed to the web
    surface opens a second connection to this database and would otherwise
    block on the write lock we were still holding. Best-effort — the draft is
    stored either way, and failing the hold because the notice failed would be
    the worse outcome.

    **This is the inbox row's delivery, not a second notice beside it.** The
    row was written inside the gate's transaction and says the same thing to the
    same routing table; sending both would put two messages in the user's alerts
    channel for one held reply. `deliver_pending` also stamps
    `last_delivered_at`, which a hand-rolled `send_notification` here could not.
    The old direct send survives as the fallback for a row that failed to write
    — the notice predates the inbox (ISSUE-246) and must not be lost with it.
    """
    from istota.notifications.delivery import send_notification

    if notification is not None:
        deliver_pending(config, [notification])
        return

    # Built from the same helpers as the row above, not hand-rolled: the
    # subject on this leg comes from an inbound header, delivery renders
    # markdown, and two branches of one function disagreeing about whether to
    # flatten is how the unflattened one survives.
    try:
        send_notification(
            config, task.user_id,
            draft_source.title_for(to_addr) + ". " + draft_source.delivery_body_for(
                subject, draft_id, draft_source.visible_recipients([to_addr]),
            ),
            purpose="alert",
        )
    except Exception as e:  # noqa: BLE001 — the draft is already safely stored
        logger.warning(
            "Held draft %d for task %d but could not notify %s: %s",
            draft_id, task.id, task.user_id, e,
        )


def email_transcript_body(message: str) -> str:
    """What an email task's answer should look like in a room transcript.

    Unwraps a result that *is* the ``{"subject": …, "body": …, "format": …}``
    envelope — mirroring that verbatim would put a JSON blob in the room and
    re-pair it into LLM history as the assistant's answer. Anything else is
    returned unchanged.

    It used to prefer the **deferred output file** over the result whenever both
    existed, and that is what made Talk and web disagree about what the bot had
    said (ISSUE-247): the two are different objects. `task.result` is the bot's
    answer to its user; the deferred file holds the bytes it mailed to a third
    party. On the reported task the result was a 1168-character explanation and
    the file held a four-line note to the contact, so Talk showed the
    explanation and the room showed the note. The room's job is to carry the
    conversation the user is reading, so the result wins whenever there is one,
    and the mailed body is no longer substituted for it.
    """
    parsed = _parse_email_output(message)
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
    *,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    body: str | None = None,
) -> None:
    """Record an outbound email for thread matching, and put it in its
    thread's room (non-critical). ``to`` and ``cc`` are the recipients
    ``to_addr`` joins (``to`` defaults to ``to_addr`` itself); ``body`` is
    what was mailed."""
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
            email_threads.register_sent_thread(
                conn, config, user_id=task.user_id, message_id=message_id,
                in_reply_to=in_reply_to, references=references,
                to=to if to is not None else [to_addr], cc=cc or [],
                subject=subject, body=body, task_id=task.id,
            )
    except Exception as e:
        logger.warning("Failed to record sent email for task %d: %s", task.id, e)


def composed_mail_body(config: "Config", task: db.Task, result: str) -> str | None:
    """The body of the mail an email task composed, or None when it composed none.

    The deferred file first, then an envelope in the result, as
    `deliver_email_result` reads them; the file is peeked, never consumed.
    """
    parsed = (
        _load_deferred_email_output(config, task, consume=False)
        or _parse_email_output(result)
    )
    if parsed and parsed.get("body"):
        return parsed["body"]
    return None


def composed_email_body(config: "Config", task: db.Task, result: str) -> str:
    """The body of the mail an email task composed, or ``result`` with none."""
    body = composed_mail_body(config, task, result)
    return result if body is None else body


#: Display cap on a recorded subject: it comes from an inbound header.
_MAIL_SUBJECT_MAX_CHARS = 200


def _stamp_thread_mail(
    config: "Config", message_id: int | None, plan: "email_threads.ReplyAll", *,
    subject: str, body: str, state: str, draft_id: int | None = None,
) -> None:
    """Record on the room's row what became of the mail it sent (ISSUE-612).

    Best-effort: the card is a view of the send, and failing the send because
    the view could not be written would be the worse outcome.
    """
    if message_id is None:
        return
    mail = {"to": [plan.to], "cc": list(plan.cc),
            "subject": (subject or "")[:_MAIL_SUBJECT_MAX_CHARS], "state": state}
    if draft_id is not None:
        mail["draft_id"] = draft_id
    try:
        with db.get_db(config.db_path) as conn:
            row = conn.execute("SELECT body FROM messages WHERE id = ?",
                               (message_id,)).fetchone()
            if row is None:
                return
            if db.mailed_body_differs(row["body"], body):
                mail["body"] = body
            db.set_outgoing_mail(conn, message_id, mail)
    except Exception as e:  # noqa: BLE001 — see the docstring
        logger.warning("Could not record the thread mail on message %s: %s", message_id, e)


async def _send_thread_reply(
    config: "Config", task: db.Task, plan: "email_threads.ReplyAll", *,
    subject: str, body: str, content_type: str = "plain",
    html_body: str | None = None, room_token: str | None = None,
    approved: bool = False, message_id: int | None = None,
) -> bool:
    """Reply-all on an email thread room's thread, or the reply to the user in
    their private email room, through the outbound gate.

    True when the mail went out or was held as a draft; False when the gate
    could not run or the send failed. The task's deferred email output is
    dropped once accounted for. ``approved`` is the host's own authenticated
    question on the thread (ISSUE-607), which the gate does not hold.
    ``message_id`` is the room's row for this mail, stamped with its
    recipients and outcome so web chat shows it as a sent mail.

    The operator's disclosure footer, when turned on, goes on here so every
    mail into the thread carries it, a held draft included (ISSUE-605).
    """
    mailed = body
    if content_type == "plain":
        try:
            with db.get_db(config.db_path) as conn:
                thread_room = room_token or email_threads.thread_room_for_task(conn, task)
                body = room_veto.with_email_footer(conn, config, thread_room, body)
        except Exception as e:  # noqa: BLE001 — the footer is a disclosure, not a gate
            logger.warning("Could not add the thread's disclosure footer: %s", e)
    held_subject = subject
    if held_subject and not held_subject.lower().startswith("re:"):
        held_subject = f"Re: {held_subject}"
    if approved:
        may_send, draft_id = True, None
    else:
        may_send, draft_id = _hold_if_unapproved(
            config, task,
            to_addr=plan.to, cc_addrs=plan.cc, subject=held_subject, body=body,
            html=content_type == "html", in_reply_to=plan.in_reply_to,
            references=plan.references, room_token=room_token,
        )
    def stamp(state: str, draft: int | None = None) -> None:
        _stamp_thread_mail(config, message_id, plan, subject=held_subject,
                           body=mailed, state=state, draft_id=draft)

    if not may_send:
        if draft_id is None:
            stamp("failed")
            return False
        stamp("held", draft_id)
        _consume_deferred_email_output(config, task)
        return True
    _consume_deferred_email_output(config, task)
    try:
        sent_message_id = reply_to_email(
            to_addr=plan.to, subject=subject, body=body,
            config=get_email_config(config), from_addr=config.email.bot_email,
            in_reply_to=plan.in_reply_to, references=plan.references,
            content_type=content_type, html_body=html_body, cc=plan.cc,
        )
    except Exception as e:
        logger.error("Failed to send the thread reply-all (task %s): %s", task.id, e)
        stamp("failed")
        return False
    stamp("sent")
    # Every recipient, so a Cc'd correspondent's reply reads as one the bot
    # wrote to (`thread_reply_from_correspondent` parses this as an address list).
    _record_sent_email(
        config, task, sent_message_id, to_addr=", ".join([plan.to, *plan.cc]),
        subject=subject, in_reply_to=plan.in_reply_to, references=plan.references,
        to=[plan.to], cc=list(plan.cc), body=mailed,
    )
    return True


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
    #
    # **Peeked, not consumed.** The file is the only copy of the composed body —
    # `task.result` holds the model's prose, not this envelope — so deleting it
    # before the outcome is known turns a transient fault into permanent message
    # loss. It is dropped by `_consume_deferred_email_output` once the message
    # has either gone out or been safely recorded as a draft, and deliberately
    # left on disk when the approval check could not run, which is the one
    # outcome with nothing else to recover from.
    parsed = (
        _load_deferred_email_output(config, task, consume=False)
        or _parse_email_output(message)
    )

    if parsed is None and task.source_type == "briefing":
        # Legacy path: model output is Talk-formatted text, send directly
        user_config = config.users.get(task.user_id)
        if not user_config or not user_config.email_addresses:
            logger.warning("No email address for user %s (task %d)", task.user_id, task.id)
            return False
        plain, html_body, content_type = _briefing_email_bodies(
            config, task, message, "plain",
        )
        legacy_subject = subject or _legacy_briefing_subject(task)
        may_send, draft_id = _hold_if_unapproved(
            config, task,
            to_addr=user_config.email_addresses[0],
            subject=legacy_subject,
            body=plain,
            html=content_type == "html",
        )
        if not may_send:
            # Held is not a failure; a gate that could not run is.
            return draft_id is not None
        try:
            email_config = get_email_config(config)
            send_email(
                to=user_config.email_addresses[0],
                subject=legacy_subject,
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
        thread_room = email_threads.thread_room_for_task(conn, task)
        thread_reply = (
            email_threads.reply_all(conn, config, thread_room, task_id=task.id)
            if thread_room else None
        )
        if thread_reply is None and processed_email is not None:
            # The user's private email room: a reply to the message, with the
            # user's own address as the only recipient.
            room_token = email_threads.private_room_for_task(conn, task)
            if room_token is not None:
                thread_room = room_token
                thread_reply = email_threads.reply_to_sender(processed_email)
        # The host asked this, in front of everyone on the thread, from a mail
        # that authenticated: that is their consent for the answer to reach
        # those people, so the outbound gate does not hold it (ISSUE-607).
        host_asked = thread_reply is not None and email_threads.host_asked(
            conn, config, thread_room, task, thread_reply,
        )
        # The answer the scheduler already stored in the room, which the mail
        # is recorded on.
        answer_row = conn.execute(
            "SELECT id FROM messages WHERE room_token = ? AND task_id = ? "
            "AND role = 'assistant'", (thread_room, task.id),
        ).fetchone() if thread_reply is not None else None

    if thread_reply is not None:
        # A reply-all to the latest message's people on an email thread room
        # (multiplayer D6), or a reply to the user in their private email
        # room, through the same gate, every recipient checked.
        subject = parsed["subject"] or thread_reply.subject
        return await _send_thread_reply(
            config, task, thread_reply, subject=subject, body=body_text,
            content_type=content_type, html_body=html_body, approved=host_asked,
            # The room, so a held draft is filed where its card is.
            room_token=thread_room,
            message_id=answer_row["id"] if answer_row is not None else None,
        )

    if processed_email:
        # Every admitted inbound mail is a turn in a room. One with none was
        # held and never admitted, so there is no one this may be sent to.
        logger.warning(
            "Email task %d has no room to answer in; the reply was not sent", task.id,
        )
        return False

    # No original email — send fresh email to user (e.g., scheduled job)
    user_config = config.users.get(task.user_id)
    if not user_config or not user_config.email_addresses:
        logger.warning("No email address for user %s (task %d)", task.user_id, task.id)
        return False

    # Use parsed subject if provided, otherwise fall back to prompt excerpt
    subject = parsed["subject"] if parsed["subject"] else f"[{config.bot_name}] {task.prompt[:80]}"

    # Addressed to the user's own address, so both live policies clear it —
    # checked anyway, because "this branch only ever mails the user" is an
    # invariant of today's callers rather than of this function.
    may_send, draft_id = _hold_if_unapproved(
        config, task,
        to_addr=user_config.email_addresses[0],
        subject=subject,
        body=body_text,
        html=content_type == "html",
    )
    if not may_send:
        if draft_id is None:
            return False
        _consume_deferred_email_output(config, task)
        return True
    _consume_deferred_email_output(config, task)

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
            to=[user_config.email_addresses[0]], body=body_text,
        )
        return True
    except Exception as e:
        logger.error("Failed to send email (task %s): %s", task.id, e)
        return False
