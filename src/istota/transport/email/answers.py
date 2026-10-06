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

A first line that is a `!drafts` command answers a held outbound draft
(ISSUE-662), read by `commands.parse_drafts_words` under the same rule. By mail
the draft id is required even with one draft open: the answer can arrive long
after the notice, and a release cannot be taken back. A discard is applied in
the poll's transaction; a release runs after it commits, with the ack, because
`drafts.release` opens its own connection to claim the draft before SMTP.
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
class DraftsAnswer:
    """A mailed `!drafts` command. ``error`` set means it could not be read."""

    verb: str
    draft_id: int | None
    error: str | None = None


@dataclass(frozen=True)
class AnswerAck:
    """The reply to an accepted answer, mailed back after the transaction.

    ``release_draft_id`` set means the answer was `!drafts send`, and the
    release itself runs then, outside the poll's transaction; ``text`` is
    replaced by what the release reports.
    """

    user_id: str
    task_id: int | None
    text: str
    draft_id: int | None = None
    release_draft_id: int | None = None
    recipients: str = ""


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


def drafts_answer_places(config) -> str:
    """Where `!drafts` can be answered, for wording a notice about a held draft."""
    if email_answers_available(config):
        return "In Talk, web chat or by email (as a mail's first line)"
    return "In Talk or web chat"


def request_answer_line(config, task_id: int) -> str:
    """The request's last line: how to answer it, and from where."""
    commands = f"!confirm {task_id} to process, or !confirm {task_id} no to discard."
    if email_answers_available(config):
        return f"From any surface, email included: {commands}"
    return (
        f"From {answer_places(config)}: {commands} "
        "Answers are not accepted by email on this deployment."
    )


def read_answer(conn, user_id: str, email) -> MailAnswer | DraftsAnswer | None:
    """Read a mail as an answer to a held task or draft, or None if it is not one.

    A reply counts as one only when it names the Message-ID the request was
    mailed under (`confirmations.replies_to_request`); a matching subject alone
    is something anyone can write.
    """
    from istota.commands import (
        _COMMAND_ALIASES, parse_command, parse_confirm_words, parse_drafts_words,
    )

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
        canonical = _COMMAND_ALIASES.get(name, name)
        if canonical == "drafts":
            parsed = parse_drafts_words(args)
            if isinstance(parsed, str):
                return DraftsAnswer(verb="", draft_id=None, error=parsed)
            verb, draft_id = parsed
            return DraftsAnswer(verb=verb, draft_id=draft_id)
        if canonical != "confirm":
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


def apply_drafts_answer(conn, config, user_id: str, mail: DraftsAnswer) -> AnswerAck:
    """Act on an authenticated `!drafts` answer, in the caller's transaction.

    A discard is applied here. A send is only checked here and returned as an
    ack carrying ``release_draft_id``, released by `deliver_acks` after commit.
    """
    from istota.commands import _drafts_listing, _visible_recipients
    from istota.mail import drafts

    def ack(text: str, draft_id: int | None = None, **extra) -> AnswerAck:
        return AnswerAck(
            user_id=user_id, task_id=None, text=text, draft_id=draft_id, **extra,
        )

    if mail.error is not None:
        return ack(f"Your answer was not read, so nothing changed. {mail.error}")
    try:
        pending = drafts.pending_for_user(conn, user_id)
    except drafts.DraftError:
        # A malformed stored row fails the whole read. Answer rather than let
        # the poll file the mail as a read error with no reply at all.
        logger.warning("Could not read user %s's held drafts", user_id, exc_info=True)
        return ack(
            "Your held drafts could not be read, so nothing changed. Use the "
            "drafts card in the web chat, which can still discard a damaged one.",
        )
    if not pending:
        return ack("No outbound mail is waiting for your approval.")
    if mail.verb in ("", "list"):
        if mail.draft_id is not None:
            return ack(
                f"`!drafts {mail.draft_id}` doesn't say what to do with it. Use "
                f"`!drafts send {mail.draft_id}` or `!drafts discard {mail.draft_id}`.",
            )
        return ack(_drafts_listing(
            pending, lead=f"**{len(pending)} draft(s) waiting for your approval:**",
        ))
    if mail.draft_id is None:
        return ack(_drafts_listing(
            pending,
            lead=(
                "By email, name the draft, for example "
                f"`!drafts {mail.verb} {pending[0].id}`. Nothing changed. Open right now:"
            ),
        ))
    draft = next((d for d in pending if d.id == mail.draft_id), None)
    if draft is None:
        # One message for "no such draft", "not yours" and "already answered",
        # as `cmd_drafts` says: an answer must not be an oracle for draft ids.
        return ack(_drafts_listing(
            pending,
            lead=f"Draft #{mail.draft_id} isn't waiting for your approval. Open right now:",
        ))
    recipients = _visible_recipients(draft)
    if mail.verb == "discard":
        try:
            drafts.discard(conn, draft.id, by="email")
        except drafts.DraftError as e:
            return ack(f"Couldn't discard #{draft.id}: {e}", draft.id)
        return ack(f"Discarded #{draft.id} — nothing was sent to {recipients}.", draft.id)
    # Owed only in memory until the batch ends: a process that dies before
    # then sends nothing and the draft stays pending. This line is what
    # matches the `drafts_answer` ledger row to a release that never ran.
    logger.info("Draft %s queued for release by email answer from user %s", draft.id, user_id)
    return ack(
        f"Sending #{draft.id} to {recipients}.", draft.id,
        release_draft_id=draft.id, recipients=recipients,
    )


def refusal_text(config, reason: str, kind: str = "task") -> str:
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
    if kind == "draft":
        return (
            "A mail from your address answered held mail, and it was not acted "
            f"on: {why} Nothing was sent or discarded. Answer from "
            f"{answer_places(config)} with !drafts and the draft number."
        )
    return (
        "A mail from your address answered a held task, and it was not acted "
        f"on: {why} Nothing was approved or discarded. Answer from "
        f"{answer_places(config)} with !confirm and the task number."
    )


def write_refusal_notice(conn, config, user_id: str, reason: str, kind: str = "task"):
    """Raise the one notice for a refused answer, on the caller's connection.

    Keyed by kind and reason alone, so a run of forged answers bumps one open
    row rather than pushing once each.
    """
    from istota.notifications.resolvers import task_alert

    prefix = "email-draft-answer-refused" if kind == "draft" else "email-answer-refused"
    return task_alert.write(
        conn, user_id,
        dedup_key=f"{prefix}:{task_alert._slug(reason, limit=32)}",
        title="An answer by email was ignored",
        body=refusal_text(config, reason, kind),
        severity="warning", actionable=True,
        params={"status": "email_answer_refused", "reason": reason},
    )


def deliver_acks(config, acks: list[AnswerAck]) -> None:
    """Mail each accepted answer's ack back. Outside every transaction."""
    from istota.notifications.delivery import send_notification

    from istota.commands import release_draft_reply

    for ack in acks:
        # Not the request's subject: a reply to the ack must not read as a
        # second answer to the same task.
        if ack.task_id is not None:
            subject = f"Answered: task #{ack.task_id}"
        elif ack.draft_id is not None:
            subject = f"Answered: draft #{ack.draft_id}"
        else:
            subject = "Your answer by email"
        text = ack.text
        if ack.release_draft_id is not None:
            text = release_draft_reply(
                config, ack.release_draft_id, ack.recipients, by="email",
            )
        try:
            send_notification(config, ack.user_id, text, surface="email", title=subject)
        except Exception:
            # The answer is applied and recorded; only the receipt is lost.
            logger.warning(
                "Could not mail the answer's ack to user %s", ack.user_id, exc_info=True,
            )

