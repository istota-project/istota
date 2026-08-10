"""The store for outbound emails the approval gate held.

A held draft is a durable artifact, not a deferred file. Deferred files are
unlinked on drain, carry no identity the web UI can address, and cannot survive
an edit-and-resend cycle — all three are things a draft has to do.

The row is **self-sufficient by design**. A reply's threading headers come from
a message fetched over IMAP at compose time, so re-fetching them at release
would be a second network round trip that can fail or come back different. The
recipients, subject, body and threading headers are snapshotted when the draft
is held, and :func:`release` sends from the row.

What the user approves is exactly what they read. Nothing re-enters the model on
approval — :func:`release` builds the message from stored bytes and sends it.
That is the whole reason the artifact is held rather than the task: re-running
the model on approval would let it decide again, and send something other than
what was shown.

Plain functions over a ``sqlite3.Connection``, matching :mod:`istota.db` — with
one deliberate exception. :func:`release` takes a ``Config`` and opens its own
connection instead of accepting the caller's, because it has to commit a claim
*before* it talks to SMTP and cannot do that inside a transaction it does not
own. See its docstring.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from email.utils import getaddresses
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_DISCARDED = "discarded"


class DraftError(Exception):
    """Base for draft-store refusals."""


class DraftNotFound(DraftError):
    pass


class DraftNotPending(DraftError):
    """The draft is not in a state that can be edited, discarded or released.

    Raised for a row that is already sent or discarded, and for one another
    caller has claimed and is mid-send.
    """


class DraftSentButUnrecorded(DraftError):
    """The mail went out and the bookkeeping after it did not.

    Its own class because it is the one failure a caller must **not** describe
    as "still waiting, try again". The finalize step runs after an irreversible
    external act, so a lock, a full disk or a schema fault there leaves the
    recipient holding the message while the row still reads `sending`. Reported
    as an ordinary send failure it invites a retry that would either be refused
    (the row is not pending) or, worse on some future code path, send twice.

    ``message_id`` is the id of the message that really did go out, so the
    caller can name it.
    """

    def __init__(self, message_id: str, cause: BaseException):
        super().__init__(
            f"the message was sent ({message_id}) but recording it failed: "
            f"{cause}"
        )
        self.message_id = message_id
        self.cause = cause


class DraftCorrupt(DraftError):
    """A stored JSON column does not hold what it must.

    Loud rather than lenient: degrading a malformed recipient column to an
    empty list means sending to a *different* set than the row nominally holds,
    and then marking it sent.
    """


@dataclass
class OutboundDraft:
    id: int
    user_id: str
    task_id: int | None
    room_token: str | None
    status: str
    to_addrs: list[str]
    cc_addrs: list[str]
    bcc_addrs: list[str]
    subject: str
    body: str
    html: bool
    in_reply_to: str | None
    references: str | None
    reply_to: str | None
    attachments: list[str]
    origin_target: str | None
    hold_reason: str
    sent_message_id: str | None
    created_at: str
    resolved_at: str | None
    nagged_at: str | None

    @property
    def all_recipients(self) -> list[str]:
        """To + Cc + Bcc, in envelope order.

        Genuinely the envelope: :func:`hold` normalizes each address to a bare
        addr-spec, one per element, so this list, the policy decision and the
        SMTP recipients are the same set. Storing raw entries would break that
        — ``send_email`` re-parses with ``getaddresses``, so one stored string
        holding two addresses becomes two envelope recipients while this
        property reports one.
        """
        return [*self.to_addrs, *self.cc_addrs, *self.bcc_addrs]


@dataclass
class DraftListing:
    """What a listing read found: the rows it could build, and the ids it could not.

    Two fields rather than a bare list because dropping an unreadable row makes
    held mail vanish without a word, and raising on one makes nine readable
    drafts unreachable. Neither is acceptable on the surface the user answers
    from, so the caller gets both and renders each in its own shape.
    """

    drafts: list[OutboundDraft]
    unreadable: list[int]


def _json_list(value: object, *, column: str) -> list[str]:
    """A JSON array column as a list of strings, raising on anything else.

    Deliberately strict. A hand-edited or truncated column that degrades to
    ``[]`` sends the message to a different recipient set than the row records
    and then marks it sent, which is worse in every direction than refusing.
    """
    if not value:
        return []
    try:
        parsed = json.loads(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise DraftCorrupt(f"{column} is not decodable JSON: {e}") from e
    if not isinstance(parsed, list):
        raise DraftCorrupt(
            f"{column} holds {type(parsed).__name__}, expected a list"
        )
    for item in parsed:
        if not isinstance(item, str):
            raise DraftCorrupt(
                f"{column} holds a non-string entry ({type(item).__name__})"
            )
    return list(parsed)


def normalize_addresses(values: list[str], *, field: str) -> list[str]:
    """Bare, lowercased addr-specs — one per element, duplicates preserved.

    Applied at hold time so the stored row, the approval card and the SMTP
    envelope can never disagree about who a message goes to. An entry carrying
    two addresses is split; a ``Name <addr>`` form is reduced to the address.

    Raises on anything that does not yield a usable address, including a value
    with an embedded newline — `send_email` sets ``To``/``Cc`` without running
    them through ``_sanitize_header``, so such a value would raise deep inside
    the email package at release time rather than being refused now.
    """
    out: list[str] = []
    for entry in values or []:
        if not isinstance(entry, str):
            raise DraftError(
                f"{field} entry is {type(entry).__name__}, expected a string"
            )
        if "\n" in entry or "\r" in entry:
            raise DraftError(f"{field} entry contains a line break: {entry!r}")
        pairs = getaddresses([entry.strip()])
        found = False
        for _, addr in pairs:
            addr = addr.strip().lower()
            local, _, domain = addr.partition("@")
            # Both halves, not merely a stray `@`: `getaddresses` echoes back
            # `@x.invalid` and `garbage` unchanged, and neither is deliverable.
            if not local or not domain:
                continue
            out.append(addr)
            found = True
        if not found:
            raise DraftError(f"{field} entry is not an email address: {entry!r}")
    return out


def _row(row: sqlite3.Row) -> OutboundDraft:
    return OutboundDraft(
        id=row["id"],
        user_id=row["user_id"],
        task_id=row["task_id"],
        room_token=row["room_token"],
        status=row["status"] or STATUS_PENDING,
        to_addrs=_json_list(row["to_addrs"], column="to_addrs"),
        cc_addrs=_json_list(row["cc_addrs"], column="cc_addrs"),
        bcc_addrs=_json_list(row["bcc_addrs"], column="bcc_addrs"),
        subject=row["subject"] or "",
        body=row["body"] or "",
        html=bool(row["html"]),
        in_reply_to=row["in_reply_to"],
        references=row["references"],
        reply_to=row["reply_to"],
        attachments=_json_list(row["attachments"], column="attachments"),
        origin_target=row["origin_target"],
        hold_reason=row["hold_reason"] or "",
        sent_message_id=row["sent_message_id"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
        nagged_at=row["nagged_at"],
    )


def hold(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    task_id: int | None,
    room_token: str | None,
    to_addrs: list[str],
    cc_addrs: list[str],
    bcc_addrs: list[str],
    subject: str,
    body: str,
    html: bool,
    in_reply_to: str | None,
    references: str | None,
    attachments: list[str],
    origin_target: str | None,
    hold_reason: str,
    reply_to: str | None = None,
) -> int:
    """Record a held draft and return its id.

    Everything needed to send later is stored now. See the module docstring for
    why the threading headers in particular are snapshotted rather than
    re-derived.

    Recipients are normalized to bare addr-specs here rather than at release, so
    the row, the card and the envelope are one list. Raises on an address that
    cannot be parsed — a draft that could never be sent should fail where the
    caller can still report it, not on the user's approval.
    """
    to_addrs = normalize_addresses(to_addrs, field="to")
    cc_addrs = normalize_addresses(cc_addrs, field="cc")
    bcc_addrs = normalize_addresses(bcc_addrs, field="bcc")
    if not to_addrs:
        raise DraftError("a draft needs at least one To recipient")

    cursor = conn.execute(
        """
        INSERT INTO outbound_drafts (
            user_id, task_id, room_token, status,
            to_addrs, cc_addrs, bcc_addrs,
            subject, body, html, in_reply_to, "references", reply_to,
            attachments, origin_target, hold_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            user_id, task_id, room_token, STATUS_PENDING,
            json.dumps(list(to_addrs or [])),
            json.dumps(list(cc_addrs or [])),
            json.dumps(list(bcc_addrs or [])),
            subject or "", body or "", 1 if html else 0,
            in_reply_to, references, reply_to,
            json.dumps(list(attachments or [])),
            origin_target, hold_reason or "",
        ),
    )
    return cursor.fetchone()[0]


def get(conn: sqlite3.Connection, draft_id: int) -> OutboundDraft | None:
    row = conn.execute(
        "SELECT * FROM outbound_drafts WHERE id = ?", (draft_id,),
    ).fetchone()
    return _row(row) if row else None


def pending_for_user(conn: sqlite3.Connection, user_id: str) -> list[OutboundDraft]:
    rows = conn.execute(
        "SELECT * FROM outbound_drafts WHERE user_id = ? AND status = ? "
        "ORDER BY id",
        (user_id, STATUS_PENDING),
    ).fetchall()
    return [_row(r) for r in rows]


def open_for_user(conn: sqlite3.Connection, user_id: str) -> DraftListing:
    """Every unresolved draft for a user, readable rows and unreadable ids apart.

    Two departures from :func:`pending_for_user`, both for the web surface.

    It carries `sending` as well as `pending`. Every other producer filters on
    `pending`, so a row left `sending` — the process died between the claim and
    the finalize, or the bookkeeping after a successful send failed — dropped
    out of the list and the stream entirely. That state is deliberately terminal
    and needs a human to check the Sent folder, and the web UI is where that
    human is, so the one status that most needs saying was the one that could
    not be shown.

    And it is **row-resilient**: a row whose JSON columns do not parse is
    reported as an id in ``unreadable`` rather than raising. Strictness is right
    in :func:`release` — sending to a recipient set we cannot read is exactly
    the thing to refuse — and wrong in a listing, where one malformed row would
    500 the whole endpoint and empty the approval surface for the other nine.
    The id is reported rather than swallowed, so the card can say a draft exists
    that cannot be read instead of the mail silently disappearing.
    """
    rows = conn.execute(
        "SELECT * FROM outbound_drafts WHERE user_id = ? AND status IN (?, ?) "
        "ORDER BY id",
        (user_id, STATUS_PENDING, STATUS_SENDING),
    ).fetchall()
    readable: list[OutboundDraft] = []
    unreadable: list[int] = []
    for row in rows:
        try:
            readable.append(_row(row))
        except DraftCorrupt:
            logger.warning(
                "outbound draft %s has a corrupt column; listing it as "
                "unreadable", row["id"], exc_info=True,
            )
            unreadable.append(row["id"])
    return DraftListing(drafts=readable, unreadable=unreadable)


def identity(conn: sqlite3.Connection, draft_id: int) -> tuple[str, str] | None:
    """``(user_id, status)`` for a draft, or ``None`` if there is no such row.

    Deliberately reads the two plain columns and parses nothing else. Ownership
    checks and the discard guard need only these, and routing them through
    :func:`get` meant a malformed recipient list denied the owner the one action
    — discarding — that does not depend on reading it.
    """
    row = conn.execute(
        "SELECT user_id, status FROM outbound_drafts WHERE id = ?", (draft_id,),
    ).fetchone()
    if row is None:
        return None
    return row["user_id"], row["status"] or STATUS_PENDING


def pending_for_room(
    conn: sqlite3.Connection, room_token: str,
) -> list[OutboundDraft]:
    """Pending drafts for a room, in creation order.

    Creation order because a task that holds two drafts renders both, each
    independently answerable, and the order they were composed in is the only
    one that means anything to the reader.
    """
    rows = conn.execute(
        "SELECT * FROM outbound_drafts WHERE room_token = ? AND status = ? "
        "ORDER BY id",
        (room_token, STATUS_PENDING),
    ).fetchall()
    return [_row(r) for r in rows]


def edit_body(
    conn: sqlite3.Connection, draft_id: int, body: str, *, html: bool | None = None,
) -> None:
    """Replace a pending draft's body.

    Only the body. Recipients and threading are deliberately not editable: an
    editable recipient list is a gate the user can be talked through, which is
    the failure this whole feature exists to prevent.
    """
    draft = get(conn, draft_id)
    if draft is None:
        raise DraftNotFound(f"no draft {draft_id}")
    if draft.status != STATUS_PENDING:
        raise DraftNotPending(
            f"draft {draft_id} is {draft.status}, not pending"
        )
    if html is None:
        cursor = conn.execute(
            "UPDATE outbound_drafts SET body = ? WHERE id = ? AND status = ?",
            (body, draft_id, STATUS_PENDING),
        )
    else:
        cursor = conn.execute(
            "UPDATE outbound_drafts SET body = ?, html = ? "
            "WHERE id = ? AND status = ?",
            (body, 1 if html else 0, draft_id, STATUS_PENDING),
        )
    if cursor.rowcount != 1:
        # The read above said pending, so losing here means a concurrent
        # release claimed the row while we were deciding. Reporting success
        # would tell the user their edit applied to a message already going out
        # with the old body.
        raise DraftNotPending(
            f"draft {draft_id} changed state while being edited; the edit was "
            "not applied"
        )


def discard(conn: sqlite3.Connection, draft_id: int) -> None:
    """Mark a pending draft discarded. Idempotent on an already-discarded row.

    Reads through :func:`identity` rather than :func:`get`, so a row whose
    stored JSON is malformed can still be binned. Nothing is sent here, so
    nothing about the decision depends on being able to read the recipient
    list — and refusing would leave the user a card with no action that works.
    """
    current = identity(conn, draft_id)
    if current is None:
        raise DraftNotFound(f"no draft {draft_id}")
    _, status = current
    if status == STATUS_DISCARDED:
        return
    if status != STATUS_PENDING:
        raise DraftNotPending(
            f"draft {draft_id} is {status}, not pending"
        )
    cursor = conn.execute(
        "UPDATE outbound_drafts SET status = ?, resolved_at = datetime('now') "
        "WHERE id = ? AND status = ?",
        (STATUS_DISCARDED, draft_id, STATUS_PENDING),
    )
    if cursor.rowcount != 1:
        # A release claimed the row between our read and our write. The mail is
        # going out; saying "discarded" would tell the user the opposite of
        # what happened.
        raise DraftNotPending(
            f"draft {draft_id} is being sent and can no longer be discarded"
        )


def _confined_attachment(config: "Config", draft: OutboundDraft, path: str) -> Path:
    """Resolve a stored attachment path and confine it to its owner's workspace.

    Re-checked here, not trusted from hold time. A pending draft is designed to
    sit indefinitely, the user's workspace stays writable from the sandbox for
    that whole window, and ``release`` runs **unsandboxed in the daemon** — so a
    path validated hours ago can be deleted and recreated as a symlink to
    anything the daemon can read. Attaching that to a mail the user already
    approved for an external recipient is a file-exfiltration primitive, and the
    hold-time check cannot see it.

    Not `skill_host_paths.resolve_host_path`: that reads `ISTOTA_DEFERRED_DIR` /
    `ISTOTA_USER_ID` from the environment, which are set per-task for a skill
    subprocess and absent in the daemon. The root is derived from the draft's
    own `user_id` instead, which is the same boundary computed from data we
    hold rather than from ambient env.
    """
    root = config.workspace_root(draft.user_id)
    if root is None:
        raise DraftError(
            "cannot verify attachment paths without a local workspace; "
            f"refusing to send draft {draft.id}"
        )
    candidate = Path(path)
    if candidate.is_symlink():
        raise DraftError(f"refusing to attach a symlink: {path}")
    if not candidate.is_file():
        raise DraftError(f"attachment is no longer readable: {path}")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(Path(root).resolve())
    except ValueError:
        raise DraftError(
            f"attachment resolves outside the user's workspace: {path}"
        ) from None
    return resolved


def release(config: "Config", draft_id: int) -> str:
    """Send a pending draft and return the sent Message-ID.

    The only function here that touches SMTP.

    **It opens its own connection**, unlike everything else in this module, and
    that is the whole design. Sending is an irreversible external act, so the
    decision to send has to be *committed before* it happens. On a caller's
    connection that is impossible: committing would commit their transaction
    too, and not committing means a rollback or a crash after the send leaves
    the row `pending` and the next approve sends the mail a second time.

    The protocol is claim, send, finalize:

    1. ``UPDATE … SET status='sending' WHERE id=? AND status='pending'``,
       committed, and the **rowcount is the decision**. A plain SELECT takes no
       lock, so a read-then-check guard lets two concurrent approvals both reach
       SMTP; only a conditional write serializes them.
    2. Send.
    3. ``sending`` → ``sent`` with the Message-ID, committed. On a send failure,
       ``sending`` → ``pending`` so the user can retry.

    The claim is also what makes :func:`edit_body` and :func:`discard` safe: both
    refuse a non-pending row, so an edit or a discard arriving mid-send now
    loses deterministically and says so, instead of reporting success while the
    old body goes out or the message the user just "discarded" is delivered.

    **A row left in ``sending`` is deliberately terminal.** It means the process
    died between the claim and the result, and we cannot know whether the mail
    went out. Resetting it to `pending` would risk sending twice; it needs a
    human to check the mailbox.

    **Never marks sent optimistically.** A draft wrongly marked sent is a
    message the user believes went out and did not.
    """
    from . import db
    from .email_support import get_email_config
    from .skills.email import send_email

    email_config = get_email_config(config)
    if not config.email.enabled or not email_config.smtp_host:
        # Checked before the claim, and on `smtp_host` specifically: an empty
        # host resolves to loopback, so a config with IMAP but no SMTP would
        # relay through whatever local MTA happens to be listening rather than
        # refusing. Reads as a config error, not a transient send failure.
        raise DraftError("email sending is not configured on this instance")

    with db.get_db(config.db_path) as conn:
        draft = get(conn, draft_id)
        if draft is None:
            raise DraftNotFound(f"no draft {draft_id}")
        if draft.status == STATUS_SENT:
            if not draft.sent_message_id:
                raise DraftError(
                    f"draft {draft_id} is marked sent but records no "
                    "Message-ID; refusing to guess whether it went out"
                )
            return draft.sent_message_id

        # The claim. rowcount, not the read above, is what decides.
        claimed = conn.execute(
            "UPDATE outbound_drafts SET status = ? WHERE id = ? AND status = ?",
            (STATUS_SENDING, draft_id, STATUS_PENDING),
        ).rowcount
        conn.commit()

    if claimed != 1:
        # Someone else got there first, or the row was never sendable.
        with db.get_db(config.db_path) as conn:
            current = get(conn, draft_id)
        if current is not None and current.status == STATUS_SENT:
            if not current.sent_message_id:
                raise DraftError(
                    f"draft {draft_id} is marked sent but records no Message-ID"
                )
            return current.sent_message_id
        state = current.status if current else "gone"
        raise DraftNotPending(f"draft {draft_id} is {state}, not pending")

    # Re-read, and send from *this* snapshot rather than the pre-claim one.
    #
    # The read above and the claim are two statements, not one: a plain SELECT
    # takes no lock under deferred isolation, and the claim UPDATE then waits up
    # to the full busy timeout for the write lock. `edit_body` is exactly the
    # competing writer, and it leaves `status='pending'` — so an edit committing
    # in that window satisfies its own guard (the user is told the edit landed,
    # and the row holds the new body) while the claim still matches. Sending the
    # pre-claim snapshot would then deliver the *old* text, irreversibly, with
    # nothing recording that the stored and sent bytes differed.
    #
    # After the claim the row is `sending`, which `edit_body` and `discard` both
    # refuse, so this snapshot is stable for the rest of the function. That is
    # what makes one extra read sufficient rather than a lock.
    with db.get_db(config.db_path) as conn:
        claimed_draft = get(conn, draft_id)
    if claimed_draft is None:
        # Nothing deletes an outbound_drafts row, so this is unreachable short
        # of hand surgery on the DB. Refuse rather than fall back to the stale
        # snapshot: we hold a claim on a row we can no longer read.
        raise DraftNotFound(f"draft {draft_id} vanished after being claimed")
    draft = claimed_draft

    def _revert(reason: str) -> None:
        """Undo the claim after a send that did not happen.

        Never raises. It runs inside an ``except`` block, so an exception here
        would replace the send failure the caller needs to see — and would
        strand the row in ``sending``, which readers treat as "we cannot know
        whether the mail went out", in the one case where we know it did not.
        """
        try:
            with db.get_db(config.db_path) as conn:
                conn.execute(
                    "UPDATE outbound_drafts SET status = ? "
                    "WHERE id = ? AND status = ?",
                    (STATUS_PENDING, draft_id, STATUS_SENDING),
                )
                conn.commit()
        except Exception as e:  # noqa: BLE001 — must not mask the send failure
            logger.error(
                "draft %d: the send failed AND the row could not be returned "
                "to pending (%s). It is stuck in 'sending'; nothing was sent.",
                draft_id, e,
            )
            return
        logger.info("draft %d returned to pending: %s", draft_id, reason)

    try:
        attachments = [
            str(_confined_attachment(config, draft, p)) for p in draft.attachments
        ]
        message_id = send_email(
            to=", ".join(draft.to_addrs),
            subject=draft.subject,
            body=draft.body,
            config=email_config,
            from_addr=config.email.bot_email,
            content_type="html" if draft.html else "plain",
            cc=draft.cc_addrs or None,
            bcc=draft.bcc_addrs or None,
            attachments=attachments or None,
            reply_to=draft.reply_to,
            in_reply_to=draft.in_reply_to,
            references=draft.references,
        )
    except Exception:
        _revert("send failed")
        raise

    # Everything from here runs *after* the mail has left. A failure is no
    # longer a send failure, and must never be reported as one — hence the
    # wrapper: it turns "the bookkeeping broke" into a distinct exception
    # carrying the Message-ID, so the caller can say the message went out and
    # tell the user not to resend. Reported as an ordinary failure it invites a
    # retry that would be refused (the row is not pending) and would leave the
    # user believing nothing was delivered.
    try:
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "UPDATE outbound_drafts SET status = ?, sent_message_id = ?, "
                "resolved_at = datetime('now') WHERE id = ? AND status = ?",
                (STATUS_SENT, message_id, draft_id, STATUS_SENDING),
            )
            # The provenance row the direct send path writes, so a reply to the
            # released mail routes back to the room the originating task came
            # from rather than falling to the alerts ladder. `origin_target`
            # was resolved at hold time, when the task was still in scope.
            try:
                db.record_sent_email(
                    conn,
                    user_id=draft.user_id,
                    message_id=message_id,
                    # The whole recipient string, matching what the direct CLI
                    # path stores and what was handed to `to=`.
                    to_addr=", ".join(draft.to_addrs),
                    subject=draft.subject,
                    task_id=draft.task_id,
                    in_reply_to=draft.in_reply_to,
                    references=draft.references,
                    conversation_token=draft.room_token,
                    talk_delivery_token=_talk_delivery_token(conn, draft.task_id),
                    origin_target=draft.origin_target,
                )
            except Exception as e:  # noqa: BLE001 — provenance is not the send
                # Losing this row costs reply routing on this thread, not the
                # send, and the status update above is worth keeping. Swallowed
                # deliberately, unlike the status update itself.
                logger.warning(
                    "released draft %d but failed to record sent_emails: %s",
                    draft_id, e,
                )
    except Exception as e:  # noqa: BLE001 — the mail is already gone
        logger.error(
            "draft %d was SENT as %s but could not be marked sent: %s. "
            "The row is stuck in 'sending'; do not resend it.",
            draft_id, message_id, e,
        )
        raise DraftSentButUnrecorded(message_id, e) from e

    return message_id


def _talk_delivery_token(conn: sqlite3.Connection, task_id: int | None) -> str | None:
    """The originating task's resolved Talk room, if the task still exists.

    Rung 1 of the Talk-delivery ladder for a reply whose `origin_target` is
    NULL. A draft routinely outlives its task (retention prunes at seven days),
    so a missing row is ordinary rather than an error.
    """
    if not task_id:
        return None
    try:
        row = conn.execute(
            "SELECT talk_delivery_token FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
    except Exception:  # pragma: no cover - defensive
        return None
    return row["talk_delivery_token"] if row else None


def stale_unnagged(
    conn: sqlite3.Connection, *, older_than_hours: int = 24,
) -> list[OutboundDraft]:
    """Pending drafts old enough to notify about, that have not been notified.

    Global rather than per-user: this backs one scheduler sweep across every
    user, so it takes no ``user_id``.

    The ``nagged_at IS NULL`` half is what makes it fire once per draft rather
    than once per scheduler tick — a draft left pending for a week gets one
    notification, not a hundred and sixty-eight.

    A non-positive window returns nothing rather than being passed through:
    ``datetime('now', '--24 hours')`` is not a legal modifier and evaluates to
    NULL, which makes the whole predicate NULL and the sweep silently empty,
    while ``0`` would mean *now* and nag every pending draft at once.

    Row-resilient for the same reason :func:`open_for_user` is, and with a wider
    blast radius: this is **one global read**, so a single unparseable row
    raising here silenced the stale-draft notification for every user on the
    instance, on every tick, indefinitely. A draft nobody can be told about is
    the exact outcome the notification exists to prevent. The corrupt row is
    skipped and logged; it is still listed on the web surface, where it can be
    seen and discarded.
    """
    if older_than_hours <= 0:
        logger.warning(
            "stale_unnagged called with older_than_hours=%r; returning nothing",
            older_than_hours,
        )
        return []
    rows = conn.execute(
        "SELECT * FROM outbound_drafts WHERE status = ? AND nagged_at IS NULL "
        "AND created_at < datetime('now', ?) ORDER BY id",
        (STATUS_PENDING, f"-{int(older_than_hours)} hours"),
    ).fetchall()
    out: list[OutboundDraft] = []
    for row in rows:
        try:
            out.append(_row(row))
        except DraftCorrupt:
            logger.warning(
                "outbound draft %s has a corrupt column; skipping it in the "
                "stale-draft sweep", row["id"], exc_info=True,
            )
    return out


def mark_nagged(conn: sqlite3.Connection, draft_id: int) -> None:
    """Stamp that the stale-draft notification for this draft was delivered.

    Called only *after* the notification actually goes out, so a send that
    failed leaves ``nagged_at`` NULL and the next sweep retries it.
    """
    conn.execute(
        "UPDATE outbound_drafts SET nagged_at = datetime('now') WHERE id = ?",
        (draft_id,),
    )
