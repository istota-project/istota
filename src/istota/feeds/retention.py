"""Retention engine for the native feeds module (ISSUE-388).

A user's ``feed_entries`` table used to keep every entry ever fetched until the
whole feed was removed, and ``entry_images`` grew with it. This is what bounds
them: an age window on ``fetched_at`` and a per-feed maximum, both per user.

Three rules are worth stating here rather than leaving to be read out of SQL,
because each is the answer to a mistake the first draft of this design made.

**The clock is ``fetched_at``** — when the entry entered *this reader* — and
never ``published_at``. An Are.na block created in 2019 and added to a channel
today arrives with a 2019 date and would be purged on the day it appeared;
``published_at`` is also nullable and, on plenty of RSS feeds, wrong.
``fetched_at`` is neither, and ``insert_entries`` has always refused to
overwrite it on refresh, so the field this needs already exists and is stable.

**Churn is closed by "was it in the most recent response", not by a
completeness test.** An entry is deletable only if ``last_seen_at`` is older
than its feed's ``last_items_seen_at``. We never delete something the feed has
just handed us, so the feed cannot hand it back — by construction, rather than
by inference about what the source still holds. That asks nothing about
whether the response was complete, well-formed or a full page, which is why
pagination needs no special handling and no provider gets a branch.

**Admission is the other half of the maximum.** A response carrying more items
than the maximum would otherwise have its tail inserted and immediately
trimmed, then re-inserted next poll. :func:`plan_admission` caps one response
at the same budget the count pass enforces, so there is no tail to churn.

This module owns setting resolution, admission, transaction control, dry-run
behaviour and result construction. The SQL storage helpers stay in
:mod:`istota.feeds.db`; ``prune_feeds`` is the only transaction owner.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from istota.feeds import db as feeds_db
from istota.feeds.models import (
    DEFAULT_ENTRY_RETENTION_DAYS,
    DEFAULT_MAX_ENTRIES_PER_FEED,
    MIN_ENTRIES_PER_FEED,
    FeedsContext,
    FetchedItem,
)


logger = logging.getLogger(__name__)


# Host-parameter chunk for the starred-guid lookup. SQLite's default limit is
# far higher, but a page from an archive-shaped source can be arbitrarily
# large and one over-long IN list would fail the whole poll.
_PARAM_CHUNK = 400


@dataclass(frozen=True)
class PruneResult:
    """What one prune did, or would have done.

    ``reusable_pages`` and ``page_size`` are diagnostics rather than deletion
    deltas: SQLite keeps freed pages on its freelist and reuses them for later
    writes, so the file does not shrink after a prune and a backup snapshot may
    retain its prior size.
    """

    dry_run: bool
    retention_days: int
    max_entries_per_feed: int
    entry_pruning_deferred_until: str | None
    entries_deleted_by_age: int
    entries_deleted_by_cap: int
    entries_held_by_floor: int
    images_deleted_by_cascade: int
    feeds_over_cap_after: int
    protected_excess_entries: int
    reusable_pages: int
    page_size: int


def _resolve(value: int | None, default: int) -> int:
    """A stored setting, or the constant.

    A negative value resolves to the default rather than being used. The API
    rejects negatives, so this is the second guard, at the point of use — and
    it is the one that matters: a negative age window puts the cutoff in the
    *future*, which makes every stored row past it and deletes the reader on
    one bad number.
    """
    if value is None or value < 0:
        return default
    return value


def resolve_retention_days(conn: sqlite3.Connection) -> int:
    """The effective age window in days. ``0`` means age pruning is off."""
    return _resolve(
        feeds_db.get_entry_retention_days(conn), DEFAULT_ENTRY_RETENTION_DAYS,
    )


def resolve_max_entries_per_feed(conn: sqlite3.Connection) -> int:
    """The effective per-feed maximum. ``0`` means there is no maximum."""
    return _resolve(
        feeds_db.get_max_entries_per_feed(conn), DEFAULT_MAX_ENTRIES_PER_FEED,
    )


def _starred_guids(
    conn: sqlite3.Connection, feed_id: int, guids: Sequence[str],
) -> set[str]:
    """Which of ``guids`` this feed already holds as starred rows."""
    found: set[str] = set()
    for start in range(0, len(guids), _PARAM_CHUNK):
        chunk = guids[start:start + _PARAM_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT guid FROM feed_entries WHERE feed_id = ? AND starred = 1 "
            f"AND guid IN ({placeholders})",
            (feed_id, *chunk),
        ).fetchall()
        found.update(r["guid"] for r in rows)
    return found


def plan_admission(
    conn: sqlite3.Connection,
    feed_id: int,
    items: Sequence[FetchedItem],
    *,
    max_entries_per_feed: int,
) -> list[FetchedItem]:
    """The window of one response that may be stored.

    Drops items with no guid, keeps the first occurrence of a duplicated guid
    — a later copy is the same entry, and taking it would make the window
    depend on write order — always admits a returned guid the feed already
    holds as a star, and admits at most ``unstarred_budget`` further items in
    source-response order.

    Source order, not a re-sort: RSS has no universal trustworthy sequence for
    missing or malformed publication dates, so re-sorting would substitute a
    guess and could permanently exclude undated new items.

    ``0`` disables the maximum, and with it this filter: every identifiable
    item is returned.
    """
    identified: list[FetchedItem] = []
    seen: set[str] = set()
    for item in items:
        if not item.guid or item.guid in seen:
            continue
        seen.add(item.guid)
        identified.append(item)

    if max_entries_per_feed <= 0 or not identified:
        return identified

    starred = _starred_guids(conn, feed_id, [i.guid for i in identified])
    stored_stars = conn.execute(
        "SELECT COUNT(*) AS n FROM feed_entries WHERE feed_id = ? AND starred = 1",
        (feed_id,),
    ).fetchone()["n"]
    budget = max(max_entries_per_feed - int(stored_stars), 0)

    admitted: list[FetchedItem] = []
    taken = 0
    for item in identified:
        if item.guid in starred:
            admitted.append(item)
            continue
        if taken >= budget:
            continue
        admitted.append(item)
        taken += 1
    return admitted


def _freelist(conn: sqlite3.Connection) -> tuple[int, int]:
    """``(freelist_count, page_size)``, or zeros where they cannot be read.

    A diagnostic must never turn a committed deletion into a failure, so this
    is the one place an exception is swallowed — and only this one.
    """
    try:
        pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        return pages, size
    except sqlite3.Error as exc:
        logger.debug("feeds_prune_freelist_unavailable err=%s", exc)
        return 0, 0


def _count_images(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM entry_images").fetchone()[0])


def prune_feeds(
    ctx: FeedsContext,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> PruneResult:
    """Apply the retention policy to one user's feeds database.

    One connection, one write transaction, both passes, one commit. A dry run
    executes the same statements and rolls back, so its counts come from the
    real algorithm rather than from a second implementation of it.

    ``now`` must be timezone-aware; a naive value is refused before a
    transaction begins, because every timestamp here is compared lexically
    against a stored ISO string and a naive one compares by its own local
    reading rather than the instant it names.

    The upgrade grace row is read before either pass. While the current time is
    earlier than it, both age and count deletion are skipped and the deferral
    is reported; polling still stamps observation state and applies admission
    during that period, so the row count can rise. A malformed grace timestamp
    is a hard error: fail closed and retain every entry.

    Image rows leave only through ``ON DELETE CASCADE``, so they are counted
    either side of the two entry passes rather than deleted directly — the
    reader applies its dedupe window relative to the historical page being
    displayed, so a retained page from any date still needs its index rows
    from that date.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("prune_feeds requires a timezone-aware `now`")
    now = now.astimezone(timezone.utc)

    with feeds_db.connect(ctx.db_path) as conn:
        # Explicit transaction control: the driver's implicit one begins at the
        # first DML statement, which would leave the reads before it outside
        # the transaction the deletes commit.
        conn.isolation_level = None
        retention_days = resolve_retention_days(conn)
        max_entries = resolve_max_entries_per_feed(conn)

        deleted_by_age = 0
        deleted_by_cap = 0
        held_by_floor = 0
        images_deleted = 0
        feeds_over = 0
        protected = 0
        deferred: str | None = None

        conn.execute("BEGIN IMMEDIATE")
        try:
            raw_grace = feeds_db.get_entry_prune_not_before(conn)
            grace: datetime | None = None
            if raw_grace is not None:
                try:
                    grace = datetime.fromisoformat(raw_grace)
                except ValueError as exc:
                    raise ValueError(
                        "feeds_internal.entry_prune_not_before is not a "
                        f"readable timestamp: {exc}"
                    ) from exc
                if grace.tzinfo is None:
                    grace = grace.replace(tzinfo=timezone.utc)

            if grace is not None and now < grace:
                deferred = raw_grace
            else:
                images_before = _count_images(conn)
                if retention_days > 0:
                    cutoff = (now - timedelta(days=retention_days)).isoformat()
                    deleted_by_age, held_by_floor = feeds_db.prune_entries_by_age(
                        conn,
                        before_iso=cutoff,
                        min_entries_per_feed=MIN_ENTRIES_PER_FEED,
                        max_entries_per_feed=max_entries,
                    )
                (
                    deleted_by_cap, feeds_over, protected,
                ) = feeds_db.prune_entries_to_feed_cap(
                    conn, max_entries_per_feed=max_entries,
                )
                images_deleted = images_before - _count_images(conn)
                if grace is not None:
                    # Only once both passes have succeeded, and in their own
                    # transaction, so a rolled-back prune keeps its grace.
                    feeds_db.clear_entry_prune_not_before(conn)
        except Exception:
            # A partial prune is never reported as success.
            conn.execute("ROLLBACK")
            raise

        conn.execute("ROLLBACK" if dry_run else "COMMIT")
        pages, page_size = _freelist(conn)

    result = PruneResult(
        dry_run=dry_run,
        retention_days=retention_days,
        max_entries_per_feed=max_entries,
        entry_pruning_deferred_until=deferred,
        entries_deleted_by_age=deleted_by_age,
        entries_deleted_by_cap=deleted_by_cap,
        entries_held_by_floor=held_by_floor,
        images_deleted_by_cascade=images_deleted,
        feeds_over_cap_after=feeds_over,
        protected_excess_entries=protected,
        reusable_pages=pages,
        page_size=page_size,
    )
    # Counts only: no titles, guids, URLs, response bodies or database paths.
    line = (
        "feeds_prune user=%s dry_run=%s retention_days=%s max_entries=%s "
        "deferred_until=%s by_age=%s by_cap=%s held_by_floor=%s images=%s "
        "feeds_over_cap=%s protected_excess=%s"
    )
    args = (
        ctx.user_id, dry_run, retention_days, max_entries, deferred,
        deleted_by_age, deleted_by_cap, held_by_floor, images_deleted,
        feeds_over, protected,
    )
    if dry_run:
        logger.debug(line, *args)
    else:
        logger.info(line, *args)
    return result
