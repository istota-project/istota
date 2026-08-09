"""Answering a held task, from any surface.

A task parked in ``pending_confirmation`` is a question, and the answer has to
be reachable wherever the user happens to be reading. Three call sites act on
one — the Talk poller's ``handle_confirmation_reply``, the ``!confirm``
command, and the web chat confirm/cancel endpoints — and before ISSUE-241 each
carried its own version of "approve this", which is how the transcript-mirror
restore came to exist in none of them. The verbs live here so the three cannot
drift.

Nothing in here decides *whether* to ask. That is the gate in
``transport/email/inbound.py``, and it stays there.
"""

from __future__ import annotations

import logging

from . import db

logger = logging.getLogger("istota.confirmations")

# How much of a task's own prompt may appear in a listing. A gated email's
# prompt is attacker-supplied text wrapped in the untrusted-input guard, so it
# is the *last* resort for describing one — `describe` reaches for the
# bot-composed confirmation prompt and the `processed_emails` metadata first.
_PREVIEW_CHARS = 60


def pending_for_user(conn, user_id: str) -> list[db.Task]:
    """Every task this user has been asked about, oldest first.

    Oldest first because that is the order the questions arrived in, and a
    listing the user reads top-down should match it.
    """
    return db.list_pending_confirmations_for_user(conn, user_id)


# Characters that turn a label into markup once a surface renders it. Talk
# renders markdown, so a subject reading `[click me](http://evil)` would become
# a live link in the user's room — attacker-authored, in a message the bot
# appears to have written. The web client puts the same string in a text node
# and needs no help, but the label has one definition and the stricter consumer
# decides it.
_MARKUP_CHARS = str.maketrans({c: " " for c in "[]()`*_~<>|\r\n"})


def _flatten(text: str) -> str:
    """Strip the markup characters out of a label and collapse the whitespace."""
    return " ".join(text.translate(_MARKUP_CHARS).split())


def describe(conn, task: db.Task) -> str:
    """A one-line label for a held task, safe to show before it is approved.

    For an email gate this is the sender and subject off ``processed_emails``,
    never the body: the whole point of the gate is that the body has not been
    approved yet. Both are still attacker-supplied, so the label is truncated
    and flattened — see :data:`_MARKUP_CHARS`.

    An email task with no ``processed_emails`` row gets a fixed label rather
    than falling back to its prompt. That fallback would print the withheld
    body, which is the one thing this function exists not to do; today the first
    line happens to be the bot's own ``<email_metadata>`` wrapper, but that is
    prompt-assembly detail and not something to rest the invariant on.
    """
    if task.source_type == "email":
        record = db.get_email_for_task(conn, task.id)
        if record is None:
            return "an inbound email"
        subject = _flatten(record.subject or "") or "(no subject)"
        sender = _flatten(record.sender_email or "") or "unknown sender"
        return f"email from {sender} — {subject[:_PREVIEW_CHARS]}"
    prompt = (task.confirmation_prompt or "").strip()
    first_line = _flatten(prompt.splitlines()[0]) if prompt else ""
    return first_line[:_PREVIEW_CHARS] or "(no description)"


def format_listing(conn, tasks: list[db.Task]) -> str:
    """The "which one?" reply — one addressable line per held task."""
    lines = [f"- `#{t.id}` {describe(conn, t)}" for t in tasks]
    return "\n".join(lines)


def approve(conn, task: db.Task, *, trust_sender: bool = False) -> bool:
    """Release a held task for execution.

    Also undoes the transcript-mirror suppression the gate applied. The gate
    sets ``IncomingMessage.suppress_transcript_mirror`` so an unapproved
    message is not published into a room the user is reading — the mirror
    commits in the task's own transaction, and ``db.cancel_task`` on a decline
    touches only ``tasks``, so mirroring first would leave attacker text there
    permanently. Nothing ever undid it on the *approval* side, so an approved
    email left the room showing the bot's answer with no question above it (the
    ISSUE-136 defect, re-reached through the gate).

    ``trust_sender`` additionally writes the sending address into the runtime
    trusted list, which is what the "yes trust" shortcut means. Only meaningful
    for an email task with a recorded sender; the **return value** says whether
    it actually happened, so a caller does not report having trusted somebody it
    could not name.
    """
    db.confirm_task(conn, task.id)
    db.log_task(conn, task.id, "info", "User confirmed task")

    trusted = False
    if trust_sender and task.source_type == "email":
        record = db.get_email_for_task(conn, task.id)
        if record is not None:
            db.add_trusted_sender(conn, task.user_id, record.sender_email)
            db.log_task(
                conn, task.id, "info", f"Trusted sender: {record.sender_email}",
            )
            trusted = True

    _restore_transcript_mirror(conn, task)
    return trusted


def decline(conn, task: db.Task) -> None:
    """Discard a held task. The withheld transcript mirror stays withheld."""
    db.cancel_task(conn, task.id)
    db.log_task(conn, task.id, "info", "User cancelled task")


def _restore_transcript_mirror(conn, task: db.Task) -> None:
    """Publish the approved turn's question into its room, if it has one.

    Existence-only, mirroring `transport.ingest.record_inbound`'s `mirror_only`
    rule: a first-contact email carries a synthetic thread token that is not a
    room, and approving it must not mint one in anyone's sidebar. The stored
    body is the task prompt verbatim — wrapper and untrusted-input guard
    included — for the same reason `record_inbound` stores it that way: it is
    re-paired straight back into LLM context, and a prettified body would drop
    the guard.
    """
    room_token = task.conversation_token
    if not room_token:
        return
    try:
        if db.get_room(conn, room_token) is None:
            return
        already = conn.execute(
            "SELECT 1 FROM messages WHERE room_token = ? AND task_id = ? "
            "AND role = 'user' LIMIT 1",
            (room_token, task.id),
        ).fetchone()
        if already:
            return
        db.add_message(
            conn, room_token, role="user", body=task.prompt,
            origin_surface=task.source_type or "email", task_id=task.id,
        )
    except Exception:
        # The approval itself has already been recorded and is what the user
        # asked for; a transcript row that failed to write must not undo it.
        logger.warning(
            "Failed to restore the transcript mirror for task %d", task.id,
            exc_info=True,
        )
