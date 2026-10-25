"""SQLite layer for the apps module.

One DB per user at ``config.module_db_path(user_id, "apps")``, so there is no
``user_id`` column: the file is the user. ``init_db`` is idempotent. The query
functions do not commit; the caller owns the transaction.

JSON columns are written from Python values and read back decoded: a row from
``get_version`` carries ``manifest``, ``source``, ``script_hashes`` and
``warnings`` rather than their ``*_json`` text. Timestamps are SQLite's
``datetime('now')`` shape, ``YYYY-MM-DD HH:MM:SS`` in UTC, so a caller-supplied
clock compares with the column defaults.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from istota.lib import sqlite_util


SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS apps (
    app_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    icon TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    current_version INTEGER,
    origin TEXT NOT NULL CHECK (origin IN ('draft', 'starter')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app_documents (
    content_hash TEXT PRIMARY KEY,
    document BLOB NOT NULL,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app_versions (
    app_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL REFERENCES app_documents(content_hash),
    manifest_json TEXT NOT NULL,
    source_json TEXT NOT NULL,
    narrowest TEXT NOT NULL,
    script_hashes_json TEXT NOT NULL,
    csp TEXT NOT NULL,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    created_by_task INTEGER,
    task_read_untrusted INTEGER,
    installed_at TEXT NOT NULL DEFAULT (datetime('now')),
    approved_at TEXT,
    PRIMARY KEY (app_id, version)
);

CREATE TABLE IF NOT EXISTS app_grants (
    app_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    capability TEXT NOT NULL,
    scope_json TEXT NOT NULL DEFAULT 'null',
    granted_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (app_id, version, capability)
);

CREATE TABLE IF NOT EXISTS app_pending (
    pending_id TEXT PRIMARY KEY,
    app_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    source_json TEXT NOT NULL,
    document BLOB NOT NULL,
    script_hashes_json TEXT NOT NULL,
    csp TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    origin TEXT NOT NULL,
    preview_json TEXT,
    created_by_task INTEGER,
    task_read_untrusted INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS app_pending_one_per_app ON app_pending(app_id);

CREATE TABLE IF NOT EXISTS app_starter_state (
    app_id TEXT PRIMARY KEY,
    installed_hash TEXT,
    dismissed_at TEXT
);

CREATE TABLE IF NOT EXISTS app_calls (
    id INTEGER PRIMARY KEY,
    app_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    capability TEXT NOT NULL,
    op TEXT NOT NULL,
    args_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS app_calls_by_app ON app_calls(app_id, at);
CREATE INDEX IF NOT EXISTS app_calls_by_at ON app_calls(at);

CREATE TABLE IF NOT EXISTS app_errors (
    id INTEGER PRIMARY KEY,
    app_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('error', 'rejection', 'navigated', 'shell')),
    message TEXT NOT NULL,
    stack TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS app_errors_by_version ON app_errors(app_id, version, id);
"""


def init_db(db_path: Path) -> None:
    """Create the schema and record its version. Idempotent.

    WAL is set here once and persists in the file header; ``connect`` does not
    re-issue it (``lib/sqlite_util`` says why).
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()


@contextmanager
def connect(db_path: Path | str, *, busy_timeout_ms: int = 30_000) -> Iterator[sqlite3.Connection]:
    """A ``Row``-factory connection with ``foreign_keys`` on."""
    with sqlite_util.open_db(db_path, busy_timeout_ms=busy_timeout_ms) as conn:
        yield conn


def sqlite_timestamp(when: datetime | None = None) -> str:
    """``when`` (default now) in the shape of the ``datetime('now')`` defaults."""
    if when is None:
        when = datetime.now(timezone.utc)
    elif when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    return when.strftime("%Y-%m-%d %H:%M:%S")


def _dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _decode(row: dict, *columns: str) -> dict:
    for column in columns:
        row[column.removesuffix("_json")] = json.loads(row.pop(column))
    return row


# -- apps ---------------------------------------------------------------------


def upsert_app(
    conn: sqlite3.Connection,
    app_id: str,
    *,
    name: str,
    icon: str,
    description: str,
    origin: str,
) -> None:
    """Create the app row, or update its display fields. ``origin`` is kept on update."""
    conn.execute(
        """
        INSERT INTO apps (app_id, name, icon, description, origin)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(app_id) DO UPDATE SET
            name = excluded.name,
            icon = excluded.icon,
            description = excluded.description
        """,
        (app_id, name, icon, description, origin),
    )


def get_app(conn: sqlite3.Connection, app_id: str) -> dict | None:
    return _row(conn.execute("SELECT * FROM apps WHERE app_id = ?", (app_id,)).fetchone())


def list_apps(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM apps ORDER BY app_id").fetchall()
    return [dict(r) for r in rows]


def delete_app(conn: sqlite3.Connection, app_id: str) -> None:
    """Remove every row the app owns, then documents nothing references.

    ``app_starter_state`` is kept: its ``dismissed_at`` is what stops an
    uninstalled starter app coming back on the next visit.
    """
    for table in ("app_grants", "app_versions", "app_pending", "app_calls", "app_errors", "apps"):
        conn.execute(f"DELETE FROM {table} WHERE app_id = ?", (app_id,))
    gc_documents(conn)


def set_current_version(conn: sqlite3.Connection, app_id: str, version: int | None) -> None:
    conn.execute(
        "UPDATE apps SET current_version = ? WHERE app_id = ?", (version, app_id),
    )


# -- versions and documents ---------------------------------------------------


def insert_version(
    conn: sqlite3.Connection,
    app_id: str,
    *,
    document: bytes,
    manifest: dict,
    source: dict,
    narrowest: str,
    script_hashes: list[str],
    csp: str,
    warnings: list[str],
    created_by_task: int | None = None,
    task_read_untrusted: bool | None = None,
    approved_at: str | None = None,
) -> int:
    """Store ``document`` (deduplicated by hash) as the app's next version.

    Returns the new version number, one past the highest the app has had. It
    does not make the version current.
    """
    content_hash = hashlib.sha256(document).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO app_documents (content_hash, document, size) VALUES (?, ?, ?)",
        (content_hash, document, len(document)),
    )
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS next FROM app_versions WHERE app_id = ?",
        (app_id,),
    ).fetchone()
    version = int(row["next"])
    untrusted = None if task_read_untrusted is None else int(bool(task_read_untrusted))
    conn.execute(
        """
        INSERT INTO app_versions (
            app_id, version, content_hash, manifest_json, source_json, narrowest,
            script_hashes_json, csp, warnings_json, created_by_task,
            task_read_untrusted, approved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            app_id, version, content_hash, _dumps(manifest), _dumps(source), narrowest,
            _dumps(script_hashes), csp, _dumps(warnings), created_by_task,
            untrusted, approved_at,
        ),
    )
    return version


_VERSION_JSON = ("manifest_json", "source_json", "script_hashes_json", "warnings_json")


def get_version(conn: sqlite3.Connection, app_id: str, version: int) -> dict | None:
    row = _row(conn.execute(
        "SELECT * FROM app_versions WHERE app_id = ? AND version = ?", (app_id, version),
    ).fetchone())
    return _decode(row, *_VERSION_JSON) if row else None


def list_versions(conn: sqlite3.Connection, app_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM app_versions WHERE app_id = ? ORDER BY version", (app_id,),
    ).fetchall()
    return [_decode(dict(r), *_VERSION_JSON) for r in rows]


def delete_version(conn: sqlite3.Connection, app_id: str, version: int) -> None:
    """Delete a non-current version and its grants. Documents are left to ``gc_documents``."""
    app = get_app(conn, app_id)
    if app is not None and app["current_version"] == version:
        raise ValueError(f"version {version} is the current version of {app_id!r}")
    conn.execute("DELETE FROM app_grants WHERE app_id = ? AND version = ?", (app_id, version))
    conn.execute("DELETE FROM app_versions WHERE app_id = ? AND version = ?", (app_id, version))


def get_document(conn: sqlite3.Connection, content_hash: str) -> bytes | None:
    row = conn.execute(
        "SELECT document FROM app_documents WHERE content_hash = ?", (content_hash,),
    ).fetchone()
    return bytes(row["document"]) if row is not None else None


def gc_documents(conn: sqlite3.Connection) -> int:
    """Delete documents no version and no pending install references. Returns the count."""
    cur = conn.execute(
        """
        DELETE FROM app_documents
        WHERE content_hash NOT IN (SELECT content_hash FROM app_versions)
          AND content_hash NOT IN (SELECT content_hash FROM app_pending)
        """
    )
    return cur.rowcount


# -- grants -------------------------------------------------------------------


def get_grants(conn: sqlite3.Connection, app_id: str, version: int) -> dict[str, object]:
    """Capability name to its decoded scope (``None`` for an unscoped grant)."""
    rows = conn.execute(
        "SELECT capability, scope_json FROM app_grants WHERE app_id = ? AND version = ?"
        " ORDER BY capability",
        (app_id, version),
    ).fetchall()
    return {r["capability"]: json.loads(r["scope_json"]) for r in rows}


def set_grants(
    conn: sqlite3.Connection, app_id: str, version: int, grants: dict[str, object],
) -> None:
    """Replace the version's grants with ``grants`` (capability to scope)."""
    conn.execute("DELETE FROM app_grants WHERE app_id = ? AND version = ?", (app_id, version))
    conn.executemany(
        "INSERT INTO app_grants (app_id, version, capability, scope_json) VALUES (?, ?, ?, ?)",
        [(app_id, version, cap, _dumps(scope)) for cap, scope in grants.items()],
    )


def put_grant(
    conn: sqlite3.Connection, app_id: str, version: int, capability: str, scope: object,
) -> None:
    """Grant one capability on a version, or replace its scope."""
    conn.execute(
        "INSERT OR REPLACE INTO app_grants (app_id, version, capability, scope_json)"
        " VALUES (?, ?, ?, ?)",
        (app_id, version, capability, _dumps(scope)),
    )


# -- pending installs ---------------------------------------------------------


_PENDING_JSON = ("manifest_json", "source_json", "script_hashes_json", "warnings_json")


def replace_pending(
    conn: sqlite3.Connection,
    *,
    app_id: str,
    document: bytes,
    manifest: dict,
    source: dict,
    script_hashes: list[str],
    csp: str,
    warnings: list[str],
    origin: str,
    preview: object | None = None,
    created_by_task: int | None = None,
    task_read_untrusted: bool | None = None,
) -> str:
    """Store a pending install for ``app_id``, replacing any earlier one. Returns its id.

    The old row is deleted first, so an approval screen still open on it gets
    nothing back for its id.
    """
    pending_id = secrets.token_urlsafe(16)
    conn.execute("DELETE FROM app_pending WHERE app_id = ?", (app_id,))
    untrusted = None if task_read_untrusted is None else int(bool(task_read_untrusted))
    conn.execute(
        """
        INSERT INTO app_pending (
            pending_id, app_id, content_hash, manifest_json, source_json, document,
            script_hashes_json, csp, warnings_json, origin, preview_json,
            created_by_task, task_read_untrusted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pending_id, app_id, hashlib.sha256(document).hexdigest(), _dumps(manifest),
            _dumps(source), document, _dumps(script_hashes), csp, _dumps(warnings),
            origin, None if preview is None else _dumps(preview),
            created_by_task, untrusted,
        ),
    )
    return pending_id


def _pending_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = _decode(dict(row), *_PENDING_JSON)
    preview = out.pop("preview_json")
    out["preview"] = None if preview is None else json.loads(preview)
    out["document"] = bytes(out["document"])
    return out


def get_pending(conn: sqlite3.Connection, pending_id: str) -> dict | None:
    return _pending_row(conn.execute(
        "SELECT * FROM app_pending WHERE pending_id = ?", (pending_id,),
    ).fetchone())


def list_pending(conn: sqlite3.Connection) -> list[dict]:
    """Every pending install, without its document."""
    rows = conn.execute(
        "SELECT pending_id, app_id, content_hash, origin, created_at,"
        " length(document) AS size FROM app_pending ORDER BY app_id"
    ).fetchall()
    return [dict(r) for r in rows]


def delete_pending(conn: sqlite3.Connection, pending_id: str) -> None:
    conn.execute("DELETE FROM app_pending WHERE pending_id = ?", (pending_id,))


# -- the call log -------------------------------------------------------------


def args_digest(args: object) -> str:
    """The sha256 hex of the arguments' canonical JSON; the arguments are never stored."""
    return hashlib.sha256(_dumps(args).encode("utf-8")).hexdigest()


def insert_call(
    conn: sqlite3.Connection,
    *,
    app_id: str,
    version: int,
    capability: str,
    op: str,
    args: object,
    status: str,
    retention_days: int,
    now: datetime | None = None,
) -> None:
    """Record one bridge call and prune calls older than ``retention_days``."""
    when = now or datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO app_calls (app_id, version, capability, op, args_digest, status, at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (app_id, version, capability, op, args_digest(args), status, sqlite_timestamp(when)),
    )
    cutoff = sqlite_timestamp(when - timedelta(days=retention_days))
    conn.execute("DELETE FROM app_calls WHERE at < ?", (cutoff,))


#: A call refused by the rate limit. Not counted against the limit, so an app
#: that keeps calling while refused is answered again once its window clears.
RATE_LIMITED = "rate_limited"


def count_recent_calls(conn: sqlite3.Connection, app_id: str, since: datetime) -> int:
    """Calls the app made at or after ``since``, across all its versions, the refused ones left out."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM app_calls WHERE app_id = ? AND at >= ? AND status != ?",
        (app_id, sqlite_timestamp(since), RATE_LIMITED),
    ).fetchone()
    return int(row["n"])


def oldest_recent_call(conn: sqlite3.Connection, app_id: str, since: datetime) -> datetime | None:
    """When the oldest call :func:`count_recent_calls` counts was made, in UTC."""
    row = conn.execute(
        "SELECT MIN(at) AS at FROM app_calls WHERE app_id = ? AND at >= ? AND status != ?",
        (app_id, sqlite_timestamp(since), RATE_LIMITED),
    ).fetchone()
    if row["at"] is None:
        return None
    return datetime.strptime(row["at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def list_calls(conn: sqlite3.Connection, app_id: str, *, limit: int = 200) -> list[dict]:
    """The app's newest calls first."""
    rows = conn.execute(
        "SELECT * FROM app_calls WHERE app_id = ? ORDER BY at DESC, id DESC LIMIT ?",
        (app_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# -- error reports ------------------------------------------------------------


def insert_error(
    conn: sqlite3.Connection,
    *,
    app_id: str,
    version: int,
    kind: str,
    message: str,
    stack: str = "",
    keep: int,
) -> None:
    """Record an error report and keep only the newest ``keep`` for the version."""
    conn.execute(
        "INSERT INTO app_errors (app_id, version, kind, message, stack) VALUES (?, ?, ?, ?, ?)",
        (app_id, version, kind, message, stack),
    )
    conn.execute(
        """
        DELETE FROM app_errors
        WHERE app_id = ? AND version = ? AND id NOT IN (
            SELECT id FROM app_errors WHERE app_id = ? AND version = ?
            ORDER BY id DESC LIMIT ?
        )
        """,
        (app_id, version, app_id, version, keep),
    )


def list_errors(
    conn: sqlite3.Connection, app_id: str, version: int, *, limit: int = 200,
) -> list[dict]:
    """The version's newest error reports first."""
    rows = conn.execute(
        "SELECT * FROM app_errors WHERE app_id = ? AND version = ? ORDER BY id DESC LIMIT ?",
        (app_id, version, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# -- starter apps -------------------------------------------------------------


def get_starter_state(conn: sqlite3.Connection, app_id: str) -> dict | None:
    return _row(conn.execute(
        "SELECT * FROM app_starter_state WHERE app_id = ?", (app_id,),
    ).fetchone())


def set_starter_state(
    conn: sqlite3.Connection,
    app_id: str,
    *,
    installed_hash: str | None,
    dismissed_at: str | None,
) -> None:
    """Write the starter app's whole state row."""
    conn.execute(
        """
        INSERT INTO app_starter_state (app_id, installed_hash, dismissed_at)
        VALUES (?, ?, ?)
        ON CONFLICT(app_id) DO UPDATE SET
            installed_hash = excluded.installed_hash,
            dismissed_at = excluded.dismissed_at
        """,
        (app_id, installed_hash, dismissed_at),
    )
