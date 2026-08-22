"""The notification inbox: the durable set of what is currently waiting on a user.

Not to be confused with :mod:`istota.notifications`, which is *delivery* and is
untouched by this module. The naming follows `secrets_store.py`: this is the
store, that is the dispatcher. Raising a notification writes a row here and,
separately, fans out through the delivery layer — so a user with no alerts
channel loses nothing (the bell is always there) and a user who lives in Talk
keeps getting system messages (the routing table is untouched).

**The write and the send are two calls, deliberately.** Most producers raise a
notification from inside an open write transaction: `outbound_drafts.hold` is an
INSERT on the caller's connection, `scheduler.py`'s confirmation gate sits
inside a transaction that has just run an UPDATE, and so do the cron
auto-disable sites. `db.get_db` uses `timeout=30.0` and Python's legacy
`isolation_level` mode, so a *second* connection opened from inside one of those
blocks waits the full thirty seconds on the write lock the caller is holding,
raises, and gets swallowed by the never-raises contract below — a silent
thirty-second stall per notification, on the dispatch loop. The hazard is
already documented in the tree: `run_cleanup_checks` buffers its expiry notices
for exactly this reason, with a comment saying so.

So a producer calls :func:`write_notification` on its own connection, buffers
the :class:`RaiseResult`, and calls :func:`deliver_pending` after its `with`
block closes. :func:`raise_notification` is the convenience for a caller that
holds no lock, and every use of it should say in a comment why that is true.

**Nothing in this module raises.** It is called from the scheduler's hot paths,
from `confirmations.approve`, and from best-effort daemon code. Every public
function wraps its body and logs at WARNING on failure, following the convention
already used by `host_pressure.py`, `process_group.py` and `git_remote_scrub.py`.
A producer never has to guard its call.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

from . import db, notification_sources as sources
from .notification_sources import (
    NotificationAction,
    NotificationRow,
    NotificationView,
)

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)


STATE_OPEN = "open"
STATE_RESOLVED = "resolved"
STATE_DISMISSED = "dismissed"
STATE_STALE = "stale"
CLOSED_STATES = (STATE_RESOLVED, STATE_DISMISSED, STATE_STALE)

DEFAULT_SEVERITY = "info"

# Closed rows are kept for reopen and for post-hoc debugging, then deleted.
NOTIFICATION_RETENTION_DAYS = 30
# The backstop for the fire-and-forget class: a row that fell below the render
# limit, or one belonging to a user who never opens the panel, is never seen and
# so never auto-resolves. Without this the badge climbs monotonically forever.
NOTIFICATION_ALERT_MAX_AGE_DAYS = 14
# The liveness pass covers the *whole* open set, not just what renders — the
# rows most likely to have dead objects are the oldest ones, which a
# render-bounded sweep never reaches. Bounded anyway: reaching this many open
# rows is itself a fault to investigate.
LIVENESS_SCAN_MAX = 500
# The panel-open GET must not wait the default thirty seconds on the scheduler's
# write lock to run its stale sweep. Same budget the room stream uses; dropping
# the sweep on contention is fine, since it runs again next time.
_STALE_SWEEP_BUSY_TIMEOUT_MS = 2000
# SQLite's default parameter limit is 999; stay well under it per statement.
_ID_CHUNK = 400

_UNREGISTERED_NOTE = "This notification's source is no longer available."
_UNRENDERABLE_NOTE = "This notification could not be rendered."


@dataclass(frozen=True)
class RaiseResult:
    """What one :func:`write_notification` call produced, for later delivery.

    `deliver` is True only when this call is the one that should send: the
    insert branch and the reopen branch, never the bump. `user_id` rides along
    because :func:`deliver_pending` runs after the caller's connection is gone
    and has no other way to know whose routing table to resolve.
    """

    notification_id: int
    user_id: str
    deliver: bool
    text: str
    title: str
    purpose: str


@dataclass(frozen=True)
class ResolvedNotification:
    """A row as the panel renders it: stored fields plus the resolver's view."""

    id: int
    source: str
    severity: str
    actionable: bool
    title: str
    body: str
    link: str | None
    occurrences: int
    created_at: str
    updated_at: str
    seen_at: str | None
    object_type: str | None
    object_id: str | None
    actions: tuple[NotificationAction, ...] = ()
    status_note: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "severity": self.severity,
            "actionable": self.actionable,
            "title": self.title,
            "body": self.body,
            "link": self.link,
            "occurrences": self.occurrences,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "seen_at": self.seen_at,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "actions": [a.to_dict() for a in self.actions],
            "status_note": self.status_note,
        }


# --- helpers -------------------------------------------------------------


def _row_to_notification(row: sqlite3.Row) -> NotificationRow:
    try:
        params = json.loads(row["params"] or "{}")
    except (TypeError, ValueError):
        params = {}
    if not isinstance(params, dict):
        params = {}
    return NotificationRow(
        id=row["id"],
        user_id=row["user_id"],
        source=row["source"],
        dedup_key=row["dedup_key"],
        object_type=row["object_type"],
        object_id=row["object_id"],
        severity=row["severity"],
        actionable=bool(row["actionable"]),
        title=row["title"],
        body=row["body"],
        params=params,
        link=row["link"],
        room_token=row["room_token"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_delivered_at=row["last_delivered_at"],
        occurrences=row["occurrences"],
        seen_at=row["seen_at"],
        state=row["state"],
        resolved_at=row["resolved_at"],
        resolved_by=row["resolved_by"],
    )


def _delivery_text(title: str, body: str) -> str:
    return f"{title}\n\n{body}" if body else title


def _chunks(values: list[Any], size: int = _ID_CHUNK) -> Iterable[list[Any]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


# --- write path ----------------------------------------------------------


def write_notification(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    source: str,
    dedup_key: str,
    title: str,
    body: str = "",
    severity: str = DEFAULT_SEVERITY,
    actionable: bool = False,
    object_type: str | None = None,
    object_id: str | None = None,
    params: dict | None = None,
    link: str | None = None,
    room_token: str | None = None,
    purpose: str = "alert",
) -> RaiseResult | None:
    """Write the row on the caller's connection, inside the caller's transaction.

    Read-modify-write, not a single-statement upsert: the branch depends on the
    row's state *before* the write, and SQLite gives no way to see that from an
    upsert (no `OLD.`, `RETURNING` reports the post-image, `lastrowid` and
    `rowcount` are identical on both branches). Since the caller is already
    holding a write lock, an honest SELECT-then-INSERT/UPDATE is both correct
    and free.

    Three branches, two of which deliver:

    - **No row.** Insert. Deliver.
    - **Row open.** Bump `occurrences`, refresh the text, refresh `updated_at`.
      Do *not* deliver — this is the deduplication a chat channel structurally
      cannot do: a nightly cron failure appends a message every night and
      updates one row in place here.
    - **Row closed.** Reopen, keeping `created_at`. Deliver — a second failure
      of the same nightly job is a second thing to hear about, and dismissing
      means "not now", not "never again".

    Returns None when the write was refused or failed; the caller carries on.
    """
    try:
        if not user_id or not source or not dedup_key or not title:
            logger.warning(
                "refusing a notification with incomplete identity "
                "(user=%r source=%r key=%r)",
                user_id, source, dedup_key,
            )
            return None

        if severity not in sources.SEVERITIES:
            logger.warning(
                "notification source %r used unknown severity %r", source, severity
            )
            severity = DEFAULT_SEVERITY

        try:
            params_json = json.dumps(params or {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            logger.warning("notification source %r sent unserializable params", source)
            params_json = "{}"

        now = db.iso_utc_now()
        existing = conn.execute(
            "SELECT id, state FROM notifications "
            "WHERE user_id = ? AND source = ? AND dedup_key = ?",
            (user_id, source, dedup_key),
        ).fetchone()

        if existing is None:
            cursor = conn.execute(
                """
                INSERT INTO notifications (
                    user_id, source, dedup_key, object_type, object_id,
                    severity, actionable, title, body, params, link, room_token,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, source, dedup_key, object_type, object_id,
                    severity, 1 if actionable else 0, title, body or "",
                    params_json, link, room_token, now, now,
                ),
            )
            return RaiseResult(
                notification_id=int(cursor.lastrowid),
                user_id=user_id,
                deliver=True,
                text=_delivery_text(title, body or ""),
                title=title,
                purpose=purpose,
            )

        notification_id = int(existing["id"])
        reopening = existing["state"] != STATE_OPEN
        # `object_type`, `object_id`, `link` and `room_token` are deliberately
        # not rewritten: a dedup key names one object, so a change there is a
        # different row, not an update to this one.
        conn.execute(
            """
            UPDATE notifications
               SET occurrences = occurrences + 1,
                   title = ?, body = ?, params = ?, severity = ?,
                   updated_at = ?,
                   state = 'open',
                   resolved_at = NULL,
                   resolved_by = NULL,
                   seen_at = CASE WHEN ? THEN NULL ELSE seen_at END
             WHERE id = ?
            """,
            (
                title, body or "", params_json, severity, now,
                1 if reopening else 0, notification_id,
            ),
        )
        return RaiseResult(
            notification_id=notification_id,
            user_id=user_id,
            deliver=reopening,
            text=_delivery_text(title, body or ""),
            title=title,
            purpose=purpose,
        )
    except Exception:
        logger.warning(
            "write_notification failed (user=%r source=%r key=%r)",
            user_id, source, dedup_key, exc_info=True,
        )
        return None


def deliver_pending(config: "Config", results: Iterable[RaiseResult | None]) -> None:
    """Send everything buffered, after the caller's write transaction has closed.

    Stamps `last_delivered_at` only where `send_notification` returned True.
    That return is False when the user has no destination configured, and
    suppressing a future re-delivery on the strength of a send that reached
    nobody is the exact failure the inbox exists to fix — the row stands either
    way, which is the point.

    Takes an iterable that may contain `None`, so a producer can hand over its
    buffer without filtering the refused writes out of it first.
    """
    try:
        from .notifications import send_notification

        delivered: list[int] = []
        for result in results or []:
            if result is None or not result.deliver:
                continue
            try:
                ok = send_notification(
                    config,
                    result.user_id,
                    result.text,
                    purpose=result.purpose,
                    title=result.title,
                )
            except Exception:
                logger.warning(
                    "notification delivery raised (id=%s user=%r)",
                    result.notification_id, result.user_id, exc_info=True,
                )
                continue
            if ok:
                delivered.append(result.notification_id)

        if not delivered:
            return

        now = db.iso_utc_now()
        with db.get_db(config.db_path) as conn:
            for chunk in _chunks(delivered):
                placeholders = ",".join("?" for _ in chunk)
                conn.execute(
                    "UPDATE notifications SET last_delivered_at = ? "
                    f"WHERE id IN ({placeholders})",
                    [now, *chunk],
                )
    except Exception:
        logger.warning("deliver_pending failed", exc_info=True)


def raise_notification(config: "Config", user_id: str, **kwargs) -> int | None:
    """Write and deliver in one call, on a connection of this function's own.

    For a caller that is **not** inside a write transaction. Every use must be
    justified in a comment naming why the caller holds no write lock — a caller
    that does hold one stalls for the full thirty-second busy timeout here, per
    notification, and the never-raises contract hides it.
    """
    try:
        with db.get_db(config.db_path) as conn:
            result = write_notification(conn, user_id, **kwargs)
        if result is None:
            return None
        deliver_pending(config, [result])
        return result.notification_id
    except Exception:
        logger.warning(
            "raise_notification failed (user=%r kwargs=%r)",
            user_id, sorted(kwargs), exc_info=True,
        )
        return None


# --- closing -------------------------------------------------------------


def resolve_notification(
    conn: sqlite3.Connection, user_id: str, source: str, dedup_key: str, *, by: str
) -> None:
    """Close the open row for `(user_id, source, dedup_key)`, if there is one.

    Idempotent: an already-closed row keeps the `resolved_at` / `resolved_by` of
    the surface that actually closed it.
    """
    try:
        conn.execute(
            "UPDATE notifications "
            "   SET state = 'resolved', resolved_at = ?, resolved_by = ?, "
            "       updated_at = ? "
            " WHERE user_id = ? AND source = ? AND dedup_key = ? AND state = 'open'",
            (db.iso_utc_now(), by, db.iso_utc_now(), user_id, source, dedup_key),
        )
    except Exception:
        logger.warning(
            "resolve_notification failed (user=%r source=%r key=%r)",
            user_id, source, dedup_key, exc_info=True,
        )
    return None


def resolve_by_object(
    conn: sqlite3.Connection,
    user_id: str,
    source: str,
    object_type: str,
    object_id: str,
    *,
    by: str,
) -> None:
    """Close the open row a producer just closed the object of.

    `user_id` is first-class and required, and that is the whole point: panel
    ids come from the per-user health module DB, where every user has a panel
    `12`. Scoped by session user, never by a value from the request — inside the
    store as well as at the endpoint.
    """
    try:
        conn.execute(
            "UPDATE notifications "
            "   SET state = 'resolved', resolved_at = ?, resolved_by = ?, "
            "       updated_at = ? "
            " WHERE user_id = ? AND source = ? AND object_type = ? "
            "   AND object_id = ? AND state = 'open'",
            (
                db.iso_utc_now(), by, db.iso_utc_now(),
                user_id, source, object_type, str(object_id),
            ),
        )
    except Exception:
        logger.warning(
            "resolve_by_object failed (user=%r source=%r %s=%r)",
            user_id, source, object_type, object_id, exc_info=True,
        )
    return None


def dismiss(conn: sqlite3.Connection, notification_id: int, user_id: str,
            *, by: str = "web") -> bool:
    """Dismiss a row the user cleared by hand. "Not now", not "never again".

    Returns whether the row is the user's *at all* — so the endpoint can 404 an
    id belonging to someone else (never 403: the row's existence is not the
    other user's business to confirm) while a second dismiss of an
    already-dismissed row is still a 200.
    """
    try:
        row = conn.execute(
            "SELECT id, state FROM notifications WHERE id = ? AND user_id = ?",
            (notification_id, user_id),
        ).fetchone()
        if row is None:
            return False
        if row["state"] == STATE_OPEN:
            now = db.iso_utc_now()
            conn.execute(
                "UPDATE notifications SET state = 'dismissed', resolved_at = ?, "
                "resolved_by = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (now, by, now, notification_id, user_id),
            )
        return True
    except Exception:
        logger.warning(
            "dismiss failed (id=%r user=%r)", notification_id, user_id, exc_info=True
        )
        return False


def mark_stale(conn: sqlite3.Connection, notification_ids: list[int]) -> None:
    """Close rows whose resolver reported the underlying object gone.

    Ids come from the liveness pass, which has already scoped them to one user.
    """
    try:
        ids = [int(i) for i in notification_ids or []]
        if not ids:
            return None
        now = db.iso_utc_now()
        for chunk in _chunks(ids):
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(
                "UPDATE notifications SET state = 'stale', resolved_at = ?, "
                "resolved_by = 'system', updated_at = ? "
                f"WHERE id IN ({placeholders}) AND state = 'open'",
                [now, now, *chunk],
            )
    except Exception:
        logger.warning("mark_stale failed (ids=%r)", notification_ids, exc_info=True)
    return None


def mark_seen(
    conn: sqlite3.Connection, user_id: str, seen: list[tuple[int, str]]
) -> None:
    """Stamp `seen_at`, and close the fire-and-forget rows that were rendered.

    Takes `(id, updated_at)` pairs, not bare ids. `seen_at` is stamped on every
    id belonging to the session user; rows whose source declares
    `auto_resolve_on_seen` are *additionally* resolved, but only where the
    stored `updated_at` still equals the one the client rendered.

    Without that version check two sequences close an occurrence nobody saw: a
    row bumped between the client's fetch and its POST (the bump does not
    deliver, so the new occurrence would vanish silently), and a late or retried
    POST arriving after the row was reopened and re-delivered.

    An id belonging to another user is skipped silently — a partial batch is not
    worth failing a panel open over. Both updates run on the caller's
    connection, so they land in one transaction.

    **Stamping never moves `updated_at`**, which is the sort key and the token
    the client hands back: moving it on a plain read would re-sort the panel
    under every other tab and invalidate the version they hold. The auto-resolve
    write does move it, along with every other close path — by then the row has
    left the open set, so there is no ordering left to disturb and no open-row
    version for another tab to match against.
    """
    try:
        wanted: dict[int, str] = {}
        for pair in seen or []:
            try:
                notification_id, rendered_at = pair
            except (TypeError, ValueError):
                continue
            if not isinstance(rendered_at, str):
                continue
            try:
                wanted[int(notification_id)] = rendered_at
            except (TypeError, ValueError):
                continue
        if not wanted:
            return None

        auto_sources = sources.auto_resolve_sources()
        now = db.iso_utc_now()

        for chunk in _chunks(list(wanted)):
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                "SELECT id, source, state, updated_at FROM notifications "
                f"WHERE user_id = ? AND id IN ({placeholders})",
                [user_id, *chunk],
            ).fetchall()
            if not rows:
                continue

            mine = [r["id"] for r in rows]
            seen_placeholders = ",".join("?" for _ in mine)
            conn.execute(
                f"UPDATE notifications SET seen_at = ? WHERE id IN ({seen_placeholders})",
                [now, *mine],
            )

            closeable = [
                r["id"]
                for r in rows
                if r["source"] in auto_sources
                and r["state"] == STATE_OPEN
                and r["updated_at"] == wanted.get(r["id"])
            ]
            if closeable:
                close_placeholders = ",".join("?" for _ in closeable)
                conn.execute(
                    "UPDATE notifications SET state = 'resolved', resolved_at = ?, "
                    "resolved_by = 'web', updated_at = ? "
                    f"WHERE id IN ({close_placeholders})",
                    [now, now, *closeable],
                )
    except Exception:
        logger.warning("mark_seen failed (user=%r)", user_id, exc_info=True)
    return None


# --- sweeps --------------------------------------------------------------


def sweep_expired_alerts(conn: sqlite3.Connection) -> int:
    """Close open fire-and-forget rows older than the alert age. Returns the count.

    Ages from `updated_at`, so a row bumped yesterday is not an old row whatever
    its `created_at`. Object-backed sources are never swept here: their close
    condition is the object, not the clock.
    """
    try:
        auto_sources = sorted(sources.auto_resolve_sources())
        if not auto_sources:
            return 0
        cutoff = db.iso_utc_days_ago(NOTIFICATION_ALERT_MAX_AGE_DAYS)
        now = db.iso_utc_now()
        closed = 0
        for chunk in _chunks(auto_sources):
            placeholders = ",".join("?" for _ in chunk)
            cursor = conn.execute(
                "UPDATE notifications SET state = 'resolved', resolved_at = ?, "
                "resolved_by = 'system', updated_at = ? "
                f"WHERE state = 'open' AND source IN ({placeholders}) "
                "  AND updated_at < ?",
                [now, now, *chunk, cutoff],
            )
            closed += cursor.rowcount or 0
        if closed:
            logger.info("notification alert sweep closed %d row(s)", closed)
        return closed
    except Exception:
        logger.warning("sweep_expired_alerts failed", exc_info=True)
        return 0


def sweep_retention(conn: sqlite3.Connection) -> int:
    """Delete closed rows past the retention window. Returns the count.

    Open rows are never deleted at any age — an object-backed item's close
    condition is the object. `COALESCE` because a row closed by an older version
    may carry no `resolved_at`.
    """
    try:
        cutoff = db.iso_utc_days_ago(NOTIFICATION_RETENTION_DAYS)
        placeholders = ",".join("?" for _ in CLOSED_STATES)
        cursor = conn.execute(
            f"DELETE FROM notifications WHERE state IN ({placeholders}) "
            "AND COALESCE(resolved_at, updated_at) < ?",
            [*CLOSED_STATES, cutoff],
        )
        deleted = cursor.rowcount or 0
        if deleted:
            logger.info("notification retention sweep deleted %d row(s)", deleted)
        return deleted
    except Exception:
        logger.warning("sweep_retention failed", exc_info=True)
        return 0


# --- read path -----------------------------------------------------------


def counts(conn: sqlite3.Connection, user_id: str) -> dict[str, int]:
    """`{"open": N, "actionable": M}` — plain SQL, no resolvers.

    The bell polls this every thirty seconds, and a resolver pass on a timer
    would open per-user module DBs repeatedly. So it is exact immediately after
    any panel open (which runs the liveness pass) and can briefly over-count
    between one panel open and the next if a producer missed a close.
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS open_count, "
            "       COALESCE(SUM(actionable), 0) AS actionable_count "
            "  FROM notifications WHERE user_id = ? AND state = 'open'",
            (user_id,),
        ).fetchone()
        return {
            "open": int(row["open_count"] or 0),
            "actionable": int(row["actionable_count"] or 0),
        }
    except Exception:
        logger.warning("notification counts failed (user=%r)", user_id, exc_info=True)
        return {"open": 0, "actionable": 0}


def _fallback(row: NotificationRow, note: str) -> ResolvedNotification:
    """Stored text, no actions, and a note saying why. Never hidden — a row
    nobody can explain is still one the user should be able to clear."""
    return ResolvedNotification(
        id=row.id,
        source=row.source,
        severity=row.severity,
        actionable=row.actionable,
        title=row.title,
        body=row.body,
        link=None,
        occurrences=row.occurrences,
        created_at=row.created_at,
        updated_at=row.updated_at,
        seen_at=row.seen_at,
        object_type=row.object_type,
        object_id=row.object_id,
        actions=(),
        status_note=note,
    )


def _rendered(row: NotificationRow, view: NotificationView) -> ResolvedNotification:
    severity = view.severity if view.severity in sources.SEVERITIES else row.severity
    return ResolvedNotification(
        id=row.id,
        source=row.source,
        severity=severity,
        actionable=row.actionable,
        title=view.title or row.title,
        body=view.body if view.body is not None else row.body,
        link=view.link,
        occurrences=row.occurrences,
        created_at=row.created_at,
        updated_at=row.updated_at,
        seen_at=row.seen_at,
        object_type=row.object_type,
        object_id=row.object_id,
        actions=tuple(view.actions or ()),
        status_note=view.status_note,
    )


def list_open(
    config: "Config",
    conn: sqlite3.Connection,
    user_id: str,
    *,
    filter: str = "all",
    limit: int = 50,
) -> tuple[list[ResolvedNotification], int]:
    """The panel payload: `(rendered rows, total open after the stale sweep)`.

    Two passes, and the split is what makes the badge honest:

    1. **Liveness** over the whole open set (to `LIVENESS_SCAN_MAX`). Every open
       row gets its resolver called; rows returning `None` are collected and
       marked `stale`. It has to cover the whole set rather than just what
       renders, because the rows most likely to have dead objects are the oldest
       ones — exactly the ones a render-bounded sweep never reaches.
    2. **Render** over the survivors: filter, newest `updated_at` first, take
       `limit`.

    A resolver that raises degrades its own row to stored text and nothing else.
    A view carrying a URL that fails the allowlist is downgraded the same way and
    logged at ERROR — that check runs here, at runtime, on every view, because a
    test against a synthetic row cannot falsify the property it claims to
    protect.

    `total_open` is the honest open count, not the filtered or truncated one:
    the client derives both tab labels from it and the returned rows.
    """
    try:
        limit = max(1, min(int(limit), LIVENESS_SCAN_MAX))
        rows = conn.execute(
            "SELECT * FROM notifications WHERE user_id = ? AND state = 'open' "
            "ORDER BY updated_at DESC, id DESC LIMIT ?",
            (user_id, LIVENESS_SCAN_MAX),
        ).fetchall()

        survivors: list[tuple[NotificationRow, ResolvedNotification]] = []
        dead: list[int] = []

        for raw in rows:
            row = _row_to_notification(raw)
            resolver = sources.get_resolver(row.source)
            if resolver is None:
                survivors.append((row, _fallback(row, _UNREGISTERED_NOTE)))
                continue
            # The whole per-row pass is guarded, not just the `resolve` call:
            # a view is a resolver-supplied object, so validating and rendering
            # it are equally capable of raising on a malformed one (a `None`
            # action tuple is enough). Outside this block that lands in the
            # function-level handler and returns an empty panel for the user —
            # one broken source blanking the bell for everything else.
            try:
                view = resolver.resolve(config, conn, row)
                if view is None:
                    dead.append(row.id)
                    continue
                bad = sources.invalid_paths(view)
                if bad:
                    logger.error(
                        "notification resolver %r emitted an unsafe path on row %s: %s",
                        row.source, row.id, "; ".join(bad),
                    )
                    survivors.append((row, _fallback(row, _UNRENDERABLE_NOTE)))
                    continue
                survivors.append((row, _rendered(row, view)))
            except Exception:
                logger.warning(
                    "notification resolver %r failed on row %s",
                    row.source, row.id, exc_info=True,
                )
                survivors.append((row, _fallback(row, _UNRENDERABLE_NOTE)))

        if dead:
            _sweep_stale(config, dead)

        # Counted from what the pass actually observed, not re-queried. The
        # sweep runs on its own connection and commits, so a re-query would
        # already exclude the dead rows and subtracting them again would
        # under-count — and it would report the wrong number in the other
        # direction whenever the sweep was dropped on contention. Only a
        # truncated scan has to ask the DB, and there the badge is allowed to
        # over-count: 500 open rows is itself a fault to investigate.
        if len(rows) < LIVENESS_SCAN_MAX:
            total_open = len(survivors)
        else:
            total_open = max(counts(conn, user_id)["open"], len(survivors))

        rendered = [item for _, item in survivors]
        if filter == "action":
            rendered = [item for item in rendered if item.actionable]
        return rendered[:limit], total_open
    except Exception:
        logger.warning("list_open failed (user=%r)", user_id, exc_info=True)
        return [], 0


def _sweep_stale(config: "Config", notification_ids: list[int]) -> None:
    """Close dead rows on a connection of our own, with a short lock budget.

    A panel-open GET must not wait the default thirty seconds on the scheduler's
    write lock. Dropping the sweep on contention is fine — it runs again next
    time — so the failure is swallowed rather than surfaced.
    """
    try:
        with db.get_db(
            config.db_path, busy_timeout_ms=_STALE_SWEEP_BUSY_TIMEOUT_MS
        ) as conn:
            mark_stale(conn, notification_ids)
    except Exception:
        logger.warning(
            "notification stale sweep skipped (%d row(s))",
            len(notification_ids), exc_info=True,
        )


__all__ = [
    "CLOSED_STATES",
    "LIVENESS_SCAN_MAX",
    "NOTIFICATION_ALERT_MAX_AGE_DAYS",
    "NOTIFICATION_RETENTION_DAYS",
    "RaiseResult",
    "ResolvedNotification",
    "STATE_DISMISSED",
    "STATE_OPEN",
    "STATE_RESOLVED",
    "STATE_STALE",
    "counts",
    "deliver_pending",
    "dismiss",
    "list_open",
    "mark_seen",
    "mark_stale",
    "raise_notification",
    "resolve_by_object",
    "resolve_notification",
    "sweep_expired_alerts",
    "sweep_retention",
    "write_notification",
]
