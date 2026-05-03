"""SQLite layer for the native feeds module.

One DB per user, lives at ``{ctx.db_path}``. Schema lives inline; ``init_db``
is idempotent and walks ``_MIGRATIONS`` to bring an existing DB up to
``SCHEMA_VERSION`` one step at a time.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

from istota.feeds.models import (
    CategoryRecord,
    EntryRecord,
    FeedRecord,
    parse_image_urls,
)


logger = logging.getLogger(__name__)


SCHEMA_VERSION = 2


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS feed_categories (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT,
    site_url TEXT,
    category_id INTEGER REFERENCES feed_categories(id) ON DELETE SET NULL,
    source_type TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    last_fetched_at TEXT,
    last_error TEXT,
    error_count INTEGER NOT NULL DEFAULT 0,
    poll_interval_minutes INTEGER NOT NULL DEFAULT 30,
    next_poll_at TEXT
);

CREATE TABLE IF NOT EXISTS feed_entries (
    id INTEGER PRIMARY KEY,
    feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid TEXT NOT NULL,
    title TEXT,
    url TEXT,
    author TEXT,
    content_html TEXT,
    content_text TEXT,
    image_urls TEXT,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unread',
    starred INTEGER NOT NULL DEFAULT 0,
    starred_at TEXT,
    UNIQUE(feed_id, guid)
);

CREATE INDEX IF NOT EXISTS idx_entries_feed_status
    ON feed_entries(feed_id, status);
CREATE INDEX IF NOT EXISTS idx_entries_published
    ON feed_entries(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_entries_starred
    ON feed_entries(starred) WHERE starred = 1;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Add ``starred`` / ``starred_at`` columns + partial index.

    Guarded by ``PRAGMA table_info`` so re-running on a fresh DB (where the
    columns are already present from ``SCHEMA_SQL``) is a no-op.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(feed_entries)")}
    if "starred" not in cols:
        conn.execute(
            "ALTER TABLE feed_entries ADD COLUMN starred INTEGER NOT NULL DEFAULT 0"
        )
    if "starred_at" not in cols:
        conn.execute("ALTER TABLE feed_entries ADD COLUMN starred_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entries_starred "
        "ON feed_entries(starred) WHERE starred = 1"
    )


_MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (2, _migrate_v1_to_v2),
]


def init_db(db_path: Path) -> None:
    """Create / migrate the SQLite schema for the feeds DB.

    Idempotent. Safe to call on every startup. Migrations run *before*
    ``SCHEMA_SQL`` because ``SCHEMA_SQL`` includes ``CREATE INDEX … WHERE
    starred = 1`` — that reference would fail on a v1 DB unless the column
    has been added first.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        # WAL is persistent in the SQLite file header — set it exactly once at
        # DB creation, not per-connection. Re-issuing the pragma while sibling
        # readers hold a transaction races and raises "database is locked".
        conn.execute("PRAGMA journal_mode = WAL")
        current = _read_schema_version(conn)
        for target_version, migrate in _MIGRATIONS:
            if current < target_version:
                migrate(conn)
                logger.info(
                    "feeds_db_migrated from=v%s to=v%s", current, target_version,
                )
                current = target_version
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            ("version", str(SCHEMA_VERSION)),
        )
        conn.commit()


def _read_schema_version(conn: sqlite3.Connection) -> int:
    """Return the persisted schema version.

    On a brand-new file the ``schema_meta`` table doesn't exist yet — return
    ``SCHEMA_VERSION`` so we skip migrations and let ``SCHEMA_SQL`` create
    everything from scratch. On an existing DB without a recorded version
    (extremely old, pre-``schema_meta``), fall back to v1.
    """
    has_meta = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if not has_meta:
        has_entries = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='feed_entries'"
        ).fetchone()
        return 1 if has_entries else SCHEMA_VERSION
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()
    if row is None:
        return 1
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 1


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with the conventions this module expects.

    - ``foreign_keys = ON`` so the FK from feeds.category_id and from
      feed_entries.feed_id behave.
    - ``Row`` factory for column-name access in callers.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


# -- categories ---------------------------------------------------------------


def upsert_category(conn: sqlite3.Connection, slug: str, title: str) -> int:
    """Insert or update a category by slug. Returns the row id."""
    cur = conn.execute(
        """
        INSERT INTO feed_categories(slug, title) VALUES (?, ?)
        ON CONFLICT(slug) DO UPDATE SET title = excluded.title
        RETURNING id
        """,
        (slug, title),
    )
    row = cur.fetchone()
    return int(row["id"])


def list_categories(conn: sqlite3.Connection) -> list[CategoryRecord]:
    rows = conn.execute(
        "SELECT id, slug, title FROM feed_categories ORDER BY title COLLATE NOCASE"
    ).fetchall()
    return [CategoryRecord(id=r["id"], slug=r["slug"], title=r["title"]) for r in rows]


def get_category_by_slug(conn: sqlite3.Connection, slug: str) -> CategoryRecord | None:
    row = conn.execute(
        "SELECT id, slug, title FROM feed_categories WHERE slug = ?", (slug,)
    ).fetchone()
    if not row:
        return None
    return CategoryRecord(id=row["id"], slug=row["slug"], title=row["title"])


def delete_category(conn: sqlite3.Connection, slug: str) -> None:
    conn.execute("DELETE FROM feed_categories WHERE slug = ?", (slug,))


# -- feeds --------------------------------------------------------------------


def upsert_feed(
    conn: sqlite3.Connection,
    *,
    url: str,
    title: str | None,
    site_url: str | None,
    source_type: str,
    category_id: int | None,
    poll_interval_minutes: int,
) -> int:
    """Insert or update a feed by URL. Returns the row id.

    Doesn't touch fetch state (etag, last_modified, error_count) — those are
    owned by the poller.
    """
    cur = conn.execute(
        """
        INSERT INTO feeds(
            url, title, site_url, source_type, category_id,
            poll_interval_minutes
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            title = COALESCE(excluded.title, feeds.title),
            site_url = COALESCE(excluded.site_url, feeds.site_url),
            source_type = excluded.source_type,
            category_id = excluded.category_id,
            poll_interval_minutes = excluded.poll_interval_minutes
        RETURNING id
        """,
        (url, title, site_url, source_type, category_id, poll_interval_minutes),
    )
    return int(cur.fetchone()["id"])


def get_feed_by_url(conn: sqlite3.Connection, url: str) -> FeedRecord | None:
    row = conn.execute("SELECT * FROM feeds WHERE url = ?", (url,)).fetchone()
    if not row:
        return None
    return _row_to_feed(row)


def list_feeds(conn: sqlite3.Connection) -> list[FeedRecord]:
    rows = conn.execute(
        "SELECT * FROM feeds ORDER BY title COLLATE NOCASE, url"
    ).fetchall()
    return [_row_to_feed(r) for r in rows]


def feeds_due_for_poll(
    conn: sqlite3.Connection, now: datetime | None = None,
) -> list[FeedRecord]:
    """Return feeds whose ``next_poll_at`` is in the past (or null)."""
    now = now or datetime.now(timezone.utc)
    iso = now.isoformat()
    rows = conn.execute(
        """
        SELECT * FROM feeds
        WHERE next_poll_at IS NULL OR next_poll_at <= ?
        ORDER BY (next_poll_at IS NULL) DESC, next_poll_at ASC, id ASC
        """,
        (iso,),
    ).fetchall()
    return [_row_to_feed(r) for r in rows]


def update_feed_fetch_state(
    conn: sqlite3.Connection,
    feed_id: int,
    *,
    etag: str | None,
    last_modified: str | None,
    last_fetched_at: str,
    last_error: str | None,
    error_count: int,
    next_poll_at: str,
    discovered_title: str | None = None,
    discovered_site_url: str | None = None,
) -> None:
    """Persist the outcome of a single poll attempt."""
    conn.execute(
        """
        UPDATE feeds
        SET etag = ?,
            last_modified = ?,
            last_fetched_at = ?,
            last_error = ?,
            error_count = ?,
            next_poll_at = ?,
            title = COALESCE(?, title),
            site_url = COALESCE(?, site_url)
        WHERE id = ?
        """,
        (
            etag, last_modified, last_fetched_at, last_error,
            error_count, next_poll_at, discovered_title,
            discovered_site_url, feed_id,
        ),
    )


def delete_feed(conn: sqlite3.Connection, url: str) -> None:
    conn.execute("DELETE FROM feeds WHERE url = ?", (url,))


# -- entries ------------------------------------------------------------------


def insert_entries(
    conn: sqlite3.Connection,
    feed_id: int,
    items: Iterable[EntryRecord],
) -> int:
    """Insert new entries, ignoring duplicates by ``(feed_id, guid)``.

    Returns the count of newly-inserted rows.
    """
    inserted = 0
    for item in items:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO feed_entries(
                feed_id, guid, title, url, author, content_html,
                content_text, image_urls, published_at, fetched_at, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feed_id,
                item.guid,
                item.title,
                item.url,
                item.author,
                item.content_html,
                item.content_text,
                json.dumps(item.image_urls) if item.image_urls else None,
                item.published_at,
                item.fetched_at,
                item.status,
            ),
        )
        if cur.rowcount:
            inserted += 1
    return inserted


def list_entries(
    conn: sqlite3.Connection,
    *,
    limit: int = 500,
    offset: int = 0,
    status: str | None = None,
    feed_id: int | None = None,
    category_id: int | None = None,
    starred: bool | None = None,
    before_published_ts: int | None = None,
    order: str = "published_at",
    direction: str = "desc",
) -> list[EntryRecord]:
    """Page through entries. Mirrors Miniflux's ``/v1/entries`` knobs."""
    clauses: list[str] = []
    params: list = []
    if status:
        clauses.append("e.status = ?")
        params.append(status)
    if feed_id:
        clauses.append("e.feed_id = ?")
        params.append(feed_id)
    if category_id:
        clauses.append("f.category_id = ?")
        params.append(category_id)
    if starred is not None:
        clauses.append("e.starred = ?")
        params.append(1 if starred else 0)
    order_col = {
        "published_at": "e.published_at",
        "created_at": "e.fetched_at",
        "id": "e.id",
        "starred_at": "e.starred_at",
    }.get(order, "e.published_at")
    if before_published_ts is not None:
        # Miniflux semantics: strictly less than. Cursor operates on the same
        # column as `order` so pagination stays stable across sort modes.
        cutoff = datetime.fromtimestamp(before_published_ts, tz=timezone.utc).isoformat()
        clauses.append(f"{order_col} < ?")
        params.append(cutoff)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    direction_sql = "ASC" if direction.lower() == "asc" else "DESC"

    rows = conn.execute(
        f"""
        SELECT e.* FROM feed_entries e
        LEFT JOIN feeds f ON f.id = e.feed_id
        {where}
        ORDER BY {order_col} {direction_sql}, e.id {direction_sql}
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    return [_row_to_entry(r) for r in rows]


def count_entries(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    feed_id: int | None = None,
    category_id: int | None = None,
    starred: bool | None = None,
) -> int:
    clauses: list[str] = []
    params: list = []
    if status:
        clauses.append("e.status = ?")
        params.append(status)
    if feed_id:
        clauses.append("e.feed_id = ?")
        params.append(feed_id)
    if category_id:
        clauses.append("f.category_id = ?")
        params.append(category_id)
    if starred is not None:
        clauses.append("e.starred = ?")
        params.append(1 if starred else 0)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    row = conn.execute(
        f"""
        SELECT COUNT(*) AS c FROM feed_entries e
        LEFT JOIN feeds f ON f.id = e.feed_id
        {where}
        """,
        params,
    ).fetchone()
    return int(row["c"])


def update_entry_status(
    conn: sqlite3.Connection, entry_ids: list[int], status: str,
) -> int:
    if not entry_ids:
        return 0
    placeholders = ",".join("?" for _ in entry_ids)
    cur = conn.execute(
        f"UPDATE feed_entries SET status = ? WHERE id IN ({placeholders})",
        (status, *entry_ids),
    )
    return cur.rowcount


def update_entry_starred(
    conn: sqlite3.Connection, entry_ids: list[int], starred: bool,
) -> int:
    """Toggle the star flag on a batch of entries.

    Sets ``starred_at`` to the current UTC ISO timestamp on true, clears it
    on false. ``starred_at`` is what powers the "starred (recent)" sort.
    """
    if not entry_ids:
        return 0
    placeholders = ",".join("?" for _ in entry_ids)
    if starred:
        now_iso = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            f"""
            UPDATE feed_entries
            SET starred = 1, starred_at = ?
            WHERE id IN ({placeholders})
            """,
            (now_iso, *entry_ids),
        )
    else:
        cur = conn.execute(
            f"""
            UPDATE feed_entries
            SET starred = 0, starred_at = NULL
            WHERE id IN ({placeholders})
            """,
            tuple(entry_ids),
        )
    return cur.rowcount


def mark_as_read(
    conn: sqlite3.Connection,
    *,
    scope: str,
    scope_id: int | None = None,
    before_id: int | None = None,
) -> int:
    """Bulk mark unread entries as read.

    ``scope`` controls which entries get touched:

    - ``"all"`` — every unread entry in the DB (no ``scope_id``).
    - ``"feed"`` — every unread entry on the given ``feed_id``.
    - ``"category"`` — every unread entry whose feed sits in the category id.

    ``before_id`` (optional) caps the operation to entries with ``id <=
    before_id``. The reader uses it to make "mark visible as read" stable
    while infinite scroll keeps loading newer entries.

    Only ``status = 'unread'`` rows are touched — already-read or removed
    rows are left alone, and starring is independent of status so this is
    safe for starred entries too.
    """
    if scope not in ("all", "feed", "category"):
        raise ValueError(f"unknown scope: {scope}")
    if scope in ("feed", "category") and scope_id is None:
        raise ValueError(f"scope={scope!r} requires scope_id")

    clauses = ["status = 'unread'"]
    params: list = []
    if scope == "feed":
        clauses.append("feed_id = ?")
        params.append(scope_id)
    elif scope == "category":
        clauses.append(
            "feed_id IN (SELECT id FROM feeds WHERE category_id = ?)"
        )
        params.append(scope_id)
    if before_id is not None:
        clauses.append("id <= ?")
        params.append(before_id)
    where = " AND ".join(clauses)
    cur = conn.execute(
        f"UPDATE feed_entries SET status = 'read' WHERE {where}",
        tuple(params),
    )
    return cur.rowcount or 0


# -- helpers ------------------------------------------------------------------


def _row_to_feed(row: sqlite3.Row) -> FeedRecord:
    return FeedRecord(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        site_url=row["site_url"],
        category_id=row["category_id"],
        source_type=row["source_type"],
        etag=row["etag"],
        last_modified=row["last_modified"],
        last_fetched_at=row["last_fetched_at"],
        last_error=row["last_error"],
        error_count=row["error_count"],
        poll_interval_minutes=row["poll_interval_minutes"],
        next_poll_at=row["next_poll_at"],
    )


def _row_to_entry(row: sqlite3.Row) -> EntryRecord:
    return EntryRecord(
        id=row["id"],
        feed_id=row["feed_id"],
        guid=row["guid"],
        title=row["title"],
        url=row["url"],
        author=row["author"],
        content_html=row["content_html"],
        content_text=row["content_text"],
        image_urls=parse_image_urls(row["image_urls"]),
        published_at=row["published_at"],
        fetched_at=row["fetched_at"],
        status=row["status"],
        starred=bool(row["starred"]) if "starred" in row.keys() else False,
        starred_at=row["starred_at"] if "starred_at" in row.keys() else None,
    )
