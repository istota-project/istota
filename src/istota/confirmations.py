"""Answering a held task, from any surface.

A task parked in ``pending_confirmation`` is a question, and the answer has to
be reachable wherever the user happens to be reading. Three call sites act on
one — the Talk poller's ``handle_confirmation_reply``, the ``!confirm``
command, and the web chat confirm/cancel endpoints — and before ISSUE-241 each
carried its own version of "approve this", which is how the transcript-mirror
restore came to exist in none of them. The verbs live here so the three cannot
drift.

ISSUE-243 moved the last two pieces Talk still kept private in with them: the
word lists a bare "yes" is read against (:func:`parse_answer`) and the
three-path lookup that decides *which* question it answers (:func:`resolve`).
Web had neither, so "yes" typed there started a task whose prompt was the word
"yes" — and, via ``_chat_create_web_task``'s room-scoped cancel, discarded the
very question it was meant to approve. The acks came with them
(:func:`apply_answer`, :func:`ambiguity_listing`): Talk used to say nothing at
all on a plain approve, and one prompt read on two surfaces should not have two
answers.

Nothing in here decides *whether* to ask. That is the gate in
``transport/email/inbound.py``, and it stays there.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

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


def flatten(text: str) -> str:
    """Public name for :func:`_flatten`.

    Exported because the notification inbox stores a label that is *delivered*
    into Talk and rendered in the web panel, and it has to flatten the same
    characters this module already decided on. A second definition elsewhere
    would be a second answer to "what is safe to put in a label".
    """
    return _flatten(text)


def describe_email(sender: str | None, subject: str | None) -> str:
    """The label for a gated email, from its sender and subject alone.

    Split out so a producer that has the envelope in hand — the inbound gate
    writes its notification row *before* ``mark_email_processed``, so
    :func:`describe` would have no ``processed_emails`` row to read yet — builds
    the identical string. The stored title and the resolver's title have to
    agree; two spellings of the same label is how one of them goes stale.
    """
    # Both halves are truncated. The subject always was; the sender is newly
    # load-bearing because this string is now stored as `notifications.title`
    # and handed to `send_notification(title=…)`, which reaches ntfy as an HTTP
    # header — an oversized one is refused by the server, and the push is then
    # lost with `last_delivered_at` correctly left null and nothing saying why.
    flat_subject = _flatten(subject or "") or "(no subject)"
    flat_sender = _flatten(sender or "") or "unknown sender"
    return (
        f"email from {flat_sender[:_PREVIEW_CHARS]} — "
        f"{flat_subject[:_PREVIEW_CHARS]}"
    )


def describe_prompt(prompt: str | None) -> str:
    """The label for a non-email held task: the first line of its own question."""
    text = (prompt or "").strip()
    first_line = _flatten(text.splitlines()[0]) if text else ""
    return first_line[:_PREVIEW_CHARS] or "(no description)"


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
        return describe_email(record.sender_email, record.subject)
    return describe_prompt(task.confirmation_prompt)


def format_listing(conn, tasks: list[db.Task]) -> str:
    """The "which one?" reply — one addressable line per held task."""
    lines = [f"- `#{t.id}` {describe(conn, t)}" for t in tasks]
    return "\n".join(lines)


def ambiguity_listing(conn, tasks) -> str:
    """The whole "several are open — say which" reply, not just its lines.

    One string rather than three copies of the same sentence: the Talk poller,
    the web composer and ``!confirm`` all reach this state, and a listing that
    names a different command on one surface than another is worse than none.
    """
    tasks = list(tasks)
    return (
        f"{len(tasks)} things are waiting for your confirmation — say which:\n"
        f"{format_listing(conn, tasks)}\n\n"
        "Answer with `!confirm <task-id>` or `!confirm <task-id> no`."
    )


def _close_notification(conn, task: db.Task, by: str) -> None:
    """Close the inbox row for a held task, on the caller's own connection.

    Here rather than at the five call sites for the same reason every other verb
    in this module is: a close that lives on the surfaces would exist on some of
    them. The store never raises, so this needs no guard of its own, and the
    resolver is the backstop if it ever misses — the panel then flips the row to
    `stale` on the next read rather than showing an answered question.
    """
    from .notification_resolvers import confirmation as confirmation_source

    confirmation_source.resolve_for_task(conn, task.user_id, task.id, by=by)


def cancel_for_conversation(
    conn, conversation_token: str, user_id: str, *, by: str = "system",
) -> int:
    """Discard this room's held questions because the user moved on.

    The fifth close path, and the one that is easiest to miss: a new message in
    a room cancels the questions already open there, and it does so with a bare
    UPDATE rather than through :func:`decline`. Left alone, those rows stayed
    open — and unlike an ordinary miss they were not merely slow to clear. The
    resolver backstop runs inside ``list_open``, i.e. only on a panel *open*,
    while the bell polls ``counts``, which is plain SQL; a user who never opened
    the panel would have carried the count for those rows indefinitely.

    The ids are read before the cancel, on the caller's own connection, because
    ``db.cancel_pending_confirmations`` reports a count rather than the rows it
    touched. Both callers hold a write transaction — the Talk poller wraps its
    whole batch in one — so opening a connection here would deadlock against it.
    """
    from .notification_resolvers import confirmation as confirmation_source

    held = conn.execute(
        "SELECT id FROM tasks WHERE conversation_token = ? AND user_id = ? "
        "AND status = 'pending_confirmation'",
        (conversation_token, user_id),
    ).fetchall()
    cancelled = db.cancel_pending_confirmations(conn, conversation_token, user_id)
    for row in held:
        confirmation_source.resolve_for_task(conn, user_id, row[0], by=by)
    return cancelled


def approve(
    conn, task: db.Task, *, trust_sender: bool = False, config=None,
    by: str = "system",
) -> bool:
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

    ``config`` is optional and used only to attribute the restored row: it
    supplies the task user's own email addresses, which is what separates "the
    user mailed themselves" from "a stranger wrote in". Pass it whenever one is
    in scope — the DB-only fallback cannot see addresses configured in TOML
    alone, and would label such a self-mail as an external speaker.

    ``by`` names the surface the answer came from, and is recorded on the closed
    notification row. It defaults to ``"system"`` rather than to any one surface
    so a caller that forgets is visibly unattributed instead of quietly filed
    under somebody else's.
    """
    db.confirm_task(conn, task.id)
    db.log_task(conn, task.id, "info", "User confirmed task")

    # Drop the parked attempt's two terminal frames, and nothing else
    # (ISSUE-235). The work it did before it asked — `task_started`, the tool
    # rows — stays: the park path persists no `execution_trace` (only
    # completion does, and the re-run overwrites it), so those rows are the
    # only durable record of what ran before permission was given. The web
    # endpoint used to delete the *whole* log here, on the reasoning that the
    # re-run's seq counter would collide on UNIQUE(task_id, seq); untrue since
    # `EventWriter._resume_seq` began seeding from `get_max_task_event_seq`.
    #
    # `confirmation` and `done` cannot stay, because a replaying client cannot
    # tell them from live ones: a web client opens the task stream at seq 0
    # (the confirm path sends no `since_seq`, nor does a reload that picks the
    # task back up), `chat_task_stream` returns on the first `done` it sends,
    # and the reducer re-arms the confirmation card with nothing to clear it —
    # so the re-run would stream to nobody and the answered card would come
    # back. The question itself is not lost; it is on
    # `tasks.confirmation_prompt`. Same kind-scoped prune the scheduler already
    # does for `text_delta` once a terminal frame lands, and gaps in `seq` are
    # harmless (SSE resume is `seq > last`).
    #
    # This lives in the shared verb rather than on the web endpoint because a
    # task confirmed over Talk or by `!confirm` is read back on the web surface
    # too, and a log that depends on which surface answered is exactly the
    # drift this module exists to prevent.
    db.delete_task_events_by_kind(conn, task.id, "confirmation")
    db.delete_task_events_by_kind(conn, task.id, "done")

    trusted = False
    if trust_sender and task.source_type == "email":
        record = db.get_email_for_task(conn, task.id)
        if record is not None:
            db.add_trusted_sender(conn, task.user_id, record.sender_email)
            db.log_task(
                conn, task.id, "info", f"Trusted sender: {record.sender_email}",
            )
            trusted = True

    _restore_transcript_mirror(conn, task, config)
    _close_notification(conn, task, by)
    return trusted


def decline(conn, task: db.Task, *, by: str = "system") -> None:
    """Discard a held task. The withheld transcript mirror stays withheld."""
    db.cancel_task(conn, task.id)
    db.log_task(conn, task.id, "info", "User cancelled task")
    _close_notification(conn, task, by)


# The words a bare answer may be spelled with. Fixed lists rather than a
# classifier: this runs on every inbound message on every surface, and the cost
# of a false positive is an unrelated message being swallowed as an
# authorization decision.
_TRUST = ("yes trust", "yes, trust", "y trust")
_YES = ("yes", "y", "ok", "okay", "proceed", "confirm", "do it", "go ahead")
_NO = ("no", "n", "cancel", "abort", "stop", "don't", "nevermind")


@dataclass(frozen=True)
class Answer:
    """What a bare "yes" / "no" / "yes trust" says."""

    approve: bool
    trust_sender: bool


def parse_answer(text: str) -> Answer | None:
    """Read a message as an answer to a held task, or None if it isn't one.

    Exact-match against the fixed word lists, not a prefix or substring test: a
    message that merely *starts* with "no" is a message, and swallowing it as
    an answer loses it. None means "not an answer" and the caller must fall
    through to normal task creation — "yes" is also a perfectly ordinary reply
    to a question the bot asked in prose, and only a parked task makes it a
    verb. That is why matching here does not by itself suppress task creation;
    only :func:`resolve` finding a question may.

    Surrounding whitespace is stripped and nothing else. Collapsing *internal*
    whitespace too would make ``"do  it"`` an answer, and every widening here
    widens the set of ordinary messages that can be swallowed as an
    authorization decision — the direction this function is least willing to
    move in.
    """
    t = text.strip().lower()
    if t in _TRUST:
        return Answer(approve=True, trust_sender=True)
    if t in _YES:
        return Answer(approve=True, trust_sender=False)
    if t in _NO:
        return Answer(approve=False, trust_sender=False)
    return None


@dataclass(frozen=True)
class Resolution:
    """Which held task an answer lands on. At most one field is set."""

    task: db.Task | None = None
    ambiguous: tuple[db.Task, ...] = ()


def resolve(
    conn,
    user_id: str,
    *,
    conversation_token: str | None = None,
    talk_response_id: int | None = None,
) -> Resolution:
    """The three-path lookup a bare answer is matched through.

    A: an explicit reply to the prompt message, addressed by the Talk id the
    prompt was posted under. B: a question parked in this same conversation.
    C: the user's single open question anywhere — bounded to *one*, because a
    bare "yes" with several open would otherwise approve whichever arrived last
    rather than the one being answered (ISSUE-241). The ambiguity is returned,
    not resolved; the caller asks which.

    The ownership check lives here rather than at the call sites. Path C
    already filters by user, so it only ever mattered for A and B — and both
    are reachable with an id the answerer does not own.
    """
    task = None
    if talk_response_id:
        task = db.get_pending_confirmation_by_response_id(conn, talk_response_id)
    if task is None and conversation_token:
        task = db.get_pending_confirmation(conn, conversation_token)
    if task is None:
        open_for_user = pending_for_user(conn, user_id)
        if len(open_for_user) > 1:
            return Resolution(ambiguous=tuple(open_for_user))
        if open_for_user:
            task = open_for_user[0]
    if task is not None and task.user_id != user_id:
        return Resolution()
    return Resolution(task=task)


def apply_answer(
    conn, task: db.Task, answer: Answer, config=None, *, by: str = "system",
) -> str:
    """Act on ``answer`` and return the ack every surface posts.

    The ack text is shared deliberately. Talk used to stay silent on a plain
    approve — the only feedback was the task's own result arriving minutes
    later — while the web endpoints and ``!confirm`` each said something else.
    One prompt reaches a user on whichever surface they read, so one answer
    should read the same wherever they give it.

    ``config`` is passed straight through to ``approve`` for attribution only.
    """
    if not answer.approve:
        decline(conn, task, by=by)
        return "Task cancelled."

    trusted = approve(
        conn, task, trust_sender=answer.trust_sender, config=config, by=by,
    )
    if trusted:
        record = db.get_email_for_task(conn, task.id)
        if record is not None:
            return (
                f"Trusted {record.sender_email} — future emails will be "
                "processed automatically."
            )
    return "Confirmed."


def record_exchange(
    conn,
    room_token: str | None,
    *,
    answer_text: str,
    ack: str,
    origin_surface: str,
    client_msg_id: str | None = None,
    answered_by: str | None = None,
) -> tuple[int | None, int | None]:
    """Write the answer and its ack into a room's canonical transcript.

    An inline-only exchange matches what a ``!command`` does, and that is the
    wrong precedent here: a confirmation is an authorization decision, and
    "did I approve that untrusted email?" deserves an answer after a refresh.
    Storing both halves also makes an answer given on one surface visible in
    the other's view of the same room, which is the whole point of pairing this
    with the Talk→room mirror.

    ``task_id`` is left NULL, following ``!steer``: the ``(room, role,
    task_id)`` unique index reserves the per-task user slot for the original
    prompt, and LLM-context reconstruction joins on ``task_id`` — so a
    NULL-task_id row is display-only and never re-paired as a phantom turn.

    ``answered_by`` is stamped as the answer row's author. A ``task_id=NULL``
    row has no task to recover an identity from, so without it the transcript
    labels the answer with whoever is *reading* — and in a shared room an
    authorization decision attributed to the wrong member is the one row worth
    getting right. The ack is left unattributed: it is the bot's.

    Existence-only, like every other room write: a synthetic email-thread token
    is not a room and approving mail must not mint one in anyone's sidebar.
    Best-effort — the answer has already been recorded and is what the user
    asked for. Returns the two ``messages.id``s, or ``(None, None)``.
    """
    if not room_token:
        return (None, None)
    try:
        if db.get_room(conn, room_token) is None:
            return (None, None)
        user_msg_id = db.add_message(
            conn, room_token, role="user", body=answer_text,
            origin_surface=origin_surface, client_msg_id=client_msg_id,
            author_user_id=answered_by,
        )
        system_msg_id = db.add_message(
            conn, room_token, role="system", body=ack,
            origin_surface=origin_surface,
        )
        return (user_msg_id, system_msg_id)
    except Exception:
        logger.warning(
            "Failed to record the confirmation exchange in %r", room_token,
            exc_info=True,
        )
        return (None, None)


def _room_holds_no_copy_of_this_exchange(conn, task: db.Task) -> bool:
    """Whether this email turn is one the room deliberately never mirrored.

    The gate's suppression means "not yet" and is undone below; ISSUE-254's
    means "never", and approving must not hand back the copy that fix removed.
    The two co-occur only under ``confirm_sender_match``, which stops the
    own-address claim from counting as trust and so lets a self-addressed thread
    reply reach the gate at all.

    A column read since ISSUE-255. The poller computes the decision and now
    records it on the task, so this asks the writer rather than reconstructing
    the answer from two observable halves — the plan naming no room, and the
    sender being the user — which needed a ``Config`` in scope and could only
    ever be an inference about what some other code had already concluded.

    A task created before that column existed reads False and is restored as it
    would have been before, which is the same direction the reconstruction's own
    fail-open branch chose: restoring wrongly costs one duplicated turn, while
    suppressing wrongly hides an approved stranger's message from the room the
    user is watching for it. The exposure is a single confirmation open across
    the upgrade, against a two-hour timeout.
    """
    return task.source_type == "email" and task.withheld_from_room


def _restore_transcript_mirror(conn, task: db.Task, config=None) -> None:
    """Publish the approved turn's question into its room, if it has one.

    The room comes from the same resolver the inbound path used
    (`transport.routing.transcript_room_for_task`) rather than from
    `task.conversation_token`, which on an email task is a thread hash: reading
    it as a room meant a first-contact approval published the question nowhere,
    and the answer then had nothing to sit under (ISSUE-247). Existence, never
    creation, at every rung, so approving still cannot mint a room in anyone's
    sidebar. The stored body is the task prompt verbatim — wrapper and
    untrusted-input guard included — for the same reason `record_inbound` stores
    it that way: it is re-paired straight back into LLM context, and a
    prettified body would drop the guard.

    Attribution is resolved here rather than inherited, because this row is
    written on a path that never saw the inbound message. With a `config` the
    task user's own addresses are authoritative; without one the resolver falls
    back to what the database can prove, which is weaker — see
    `db.own_addresses_without_config`.

    Without a `config` there is no routing table to consult, so the resolution
    is the token itself — and the existence check below is what keeps that from
    being wrong rather than merely weaker: it is rung 1 of the same ladder, so a
    routed email's thread hash writes nothing instead of minting or mis-filing.
    Every live caller passes a config (`commands.cmd_confirm`, the web confirm
    endpoint, `apply_answer`); the bare form survives for the DB-only
    attribution path `db.author_for_email_task` documents.
    """
    room_token = task.conversation_token
    if config is not None:
        from .transport.routing import transcript_room_for_task
        room_token = transcript_room_for_task(conn, config, task)
    if not room_token:
        return
    try:
        if _room_holds_no_copy_of_this_exchange(conn, task):
            return
        if db.get_room(conn, room_token) is None:
            return
        already = conn.execute(
            "SELECT 1 FROM messages WHERE room_token = ? AND task_id = ? "
            "AND role = 'user' LIMIT 1",
            (room_token, task.id),
        ).fetchone()
        if already:
            return
        own = None
        if config is not None:
            user_config = config.users.get(task.user_id or "")
            if user_config is not None:
                own = list(user_config.email_addresses or [])
        author_user_id, author_label = db.author_for_email_task(
            conn, task.id, task.user_id, own,
        )
        db.add_message(
            conn, room_token, role="user", body=task.prompt,
            origin_surface=task.source_type or "email", task_id=task.id,
            author_user_id=author_user_id, author_label=author_label,
        )
    except Exception:
        # The approval itself has already been recorded and is what the user
        # asked for; a transcript row that failed to write must not undo it.
        logger.warning(
            "Failed to restore the transcript mirror for task %d", task.id,
            exc_info=True,
        )
