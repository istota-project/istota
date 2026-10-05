"""Answering a held task by email (ISSUE-649).

A mailed answer is an incoming mail from the user's own address, and a From
naming that address is a claim anyone can make. So the answer is not decided by
``confirm_sender_match``, whose default ``off`` takes that claim on trust: it
is accepted only when the receiving MTA stamped a DMARC pass under our own
``authserv_id``, aligned with the address, whatever that setting says. The
check itself is `inbound._authentication_verdict`; this module reads the
answer, applies it and words the notices.

Two shapes are answers: a first line that is a `!confirm` command (`!yes`,
`!no` and the other aliases included), read by `commands.parse_confirm_words`,
and a reply to the mailed request, whose subject names the task
(`confirmations.request_subject`), whose `In-Reply-To` or `References` names the
request's recorded Message-ID, and whose first line is a bare answer read by
`confirmations.parse_answer`. Both act through `confirmations.apply_answer`.
Anything else is ordinary mail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from istota import confirmations
from istota.transport.email import threads as email_threads

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MailAnswer:
    """What an answering mail said. ``error`` set means it could not be read."""

    task_id: int | None
    answer: confirmations.Answer | None
    error: str | None = None


@dataclass(frozen=True)
class AnswerAck:
    """The reply to an accepted answer, mailed back after the transaction."""

    user_id: str
    task_id: int | None
    text: str


def email_answers_available(config) -> bool:
    """Whether an answer by mail can be authenticated at all."""
    return bool(config.email.enabled and (config.email.authserv_id or "").strip())


def answer_places(config) -> str:
    """Where a user can answer when mail is not accepted, for the wording."""
    places = []
    if config.web.enabled:
        places.append("the web chat")
    if config.talk.enabled:
        places.append("Talk")
    return " or ".join(places) or "the web chat or Talk"


def request_answer_line(config, task_id: int) -> str:
    """The request's last line: how to answer it, and from where."""
    commands = f"!confirm {task_id} to process, or !confirm {task_id} no to discard."
    if email_answers_available(config):
        return f"From any surface, email included: {commands}"
    return (
        f"From {answer_places(config)}: {commands} "
        "Answers are not accepted by email on this deployment."
    )


def read_answer(conn, user_id: str, email) -> MailAnswer | None:
    """Read a mail as an answer to a held task, or None if it is not one.

    A reply counts as one only when it names the Message-ID the request was
    mailed under (`confirmations.replies_to_request`); a matching subject alone
    is something anyone can write.
    """
    from istota.commands import _COMMAND_ALIASES, parse_command, parse_confirm_words

    text = email_threads.new_text(email.body)
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    reply_to = confirmations.task_id_from_reply_subject(email.subject)
    if reply_to is not None and not confirmations.replies_to_request(
        conn, user_id, reply_to, email,
    ):
        reply_to = None

    command = parse_command(first)
    if command is not None:
        name, args = command
        if _COMMAND_ALIASES.get(name, name) != "confirm":
            return None
        parsed = parse_confirm_words(name, args)
        if isinstance(parsed, str):
            return MailAnswer(task_id=reply_to, answer=None, error=parsed)
        target, verb = parsed
        return MailAnswer(
            task_id=target if target is not None else reply_to,
            answer=confirmations.Answer(
                approve=verb != "decline", trust_sender=verb == "trust",
            ),
        )

    if reply_to is None:
        return None
    answer = confirmations.parse_answer(first)
    if answer is None:
        return None
    return MailAnswer(task_id=reply_to, answer=answer)


def apply_mail_answer(conn, config, user_id: str, mail: MailAnswer) -> tuple[int | None, str]:
    """Act on an authenticated answer. Returns (the task answered, the ack)."""
    if mail.error is not None:
        return None, f"Your answer was not read, so nothing changed. {mail.error}"
    if mail.task_id is None:
        return None, (
            "Your answer did not name a task, so nothing changed. Write "
            "!confirm <task-id>, or reply to the request itself."
        )
    pending = confirmations.pending_for_user(conn, user_id)
    task = next((t for t in pending if t.id == mail.task_id), None)
    if task is None:
        # One message for "no such task", "not yours" and "already answered",
        # as `cmd_confirm` says: an answer must not be an oracle for task ids.
        return None, f"Task #{mail.task_id} isn't waiting for your confirmation."
    if task.whatsapp_confirmation_request_id:
        # A relay question's preview is shown in a private room, never mailed.
        return None, (
            f"Task #{task.id} is a relay question. Confirm it from a verified "
            "private conversation."
        )
    label = confirmations.describe(conn, task)
    ack = confirmations.apply_answer(conn, task, mail.answer, config, by="email")
    return task.id, f"Task #{task.id} ({label}): {ack}"


def refusal_text(config, reason: str) -> str:
    """The notice for an answer that was not acted on. Never quotes the mail."""
    if reason == "unstamped":
        why = (
            "the mail did not carry your mail server's own authentication "
            "result, so it could have been sent by someone else using your "
            "address."
        )
    elif reason == "unavailable":
        why = (
            "this deployment does not accept answers by email. The operator has "
            "not set email.authserv_id, which is what lets a mail be checked as "
            "really coming from you."
        )
    else:
        why = (
            "the mail did not pass a DMARC check at your mail server "
            f"(result: {reason}), so it could have been sent by someone else "
            "using your address."
        )
    return (
        "A mail from your address answered a held task, and it was not acted "
        f"on: {why} Nothing was approved or discarded. Answer from "
        f"{answer_places(config)} with !confirm and the task number."
    )


def write_refusal_notice(conn, config, user_id: str, reason: str):
    """Raise the one notice for a refused answer, on the caller's connection.

    Keyed by reason alone, so a run of forged answers bumps one open row
    rather than pushing once each.
    """
    from istota.notifications.resolvers import task_alert

    return task_alert.write(
        conn, user_id,
        dedup_key=f"email-answer-refused:{task_alert._slug(reason, limit=32)}",
        title="An answer by email was ignored",
        body=refusal_text(config, reason),
        severity="warning", actionable=True,
        params={"status": "email_answer_refused", "reason": reason},
    )


def deliver_acks(config, acks: list[AnswerAck]) -> None:
    """Mail each accepted answer's ack back. Outside every transaction."""
    from istota.notifications.delivery import send_notification

    for ack in acks:
        # Not the request's subject: a reply to the ack must not read as a
        # second answer to the same task.
        subject = (
            f"Answered: task #{ack.task_id}" if ack.task_id is not None
            else "Your answer by email"
        )
        try:
            send_notification(config, ack.user_id, ack.text, surface="email", title=subject)
        except Exception:
            # The answer is applied and recorded; only the receipt is lost.
            logger.warning(
                "Could not mail the answer's ack to user %s", ack.user_id, exc_info=True,
            )

