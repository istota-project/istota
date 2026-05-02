"""SQLite layer for the native feeds module.

One DB per user, lives at ``{ctx.db_path}``. Schema is small enough that the
DDL lives inline as constants — no migration framework, just an idempotent
``init_db`` that runs ``CREATE TABLE IF NOT EXISTS``.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from istota.feeds.models import (
    CategoryRecord,
    EntryRecord,
    FeedRecord,
    parse_image_urls,
)


SCHEMA_VERSION = 1


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
    UNIQUE(feed_id, guid)
);

CREATE INDEX IF NOT EXISTS idx_entries_feed_status
    ON feed_entries(feed_id, status);
CREATE INDEX IF NOT EXISTS idx_entries_published
    ON feed_entries(published_at DESC);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def init_db(db_path: Path) -> None:
    """Create / migrate the SQLite schema for the feeds DB.

    Idempotent. Safe to call on every startup.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            ("version", str(SCHEMA_VERSION)),
        )
        conn.commit()


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with the conventions this module expects.

    - ``foreign_keys = ON`` so the FK from feeds.category_id and from
      feed_entries.feed_id behave.
    - ``Row`` factory for column-name access in callers.
    - ``WAL`` journaling for concurrent reads (poller writes, web reads).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
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
    if before_published_ts is not None:
        # Miniflux semantics: strictly less than. Compare ISO timestamps lexically;
        # works because we always store UTC ISO 8601.
        cutoff = datetime.fromtimestamp(before_published_ts, tz=timezone.utc).isoformat()
        clauses.append("e.published_at < ?")
        params.append(cutoff)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    order_col = {
        "published_at": "e.published_at",
        "created_at": "e.fetched_at",
        "id": "e.id",
    }.get(order, "e.published_at")
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
    )
