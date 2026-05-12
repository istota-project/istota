"""SQLite layer for the per-user health DB.

One DB per user, lives at ``{ctx.db_path}``. ``init_db`` is idempotent and
sets WAL mode exactly once at creation (re-issuing the pragma races with
sibling readers — same rule as the feeds / location DBs).

Tables:

* ``stats``           — time-series body stats (weight, BP, resting HR, …)
* ``panels``          — one row per lab visit / uploaded report
* ``biomarkers``      — individual values from a panel
* ``biomarker_refs``  — canonical names + Istota-curated reference ranges
* ``health_settings`` — DOB, height, biological sex, display unit prefs
* ``schema_meta``     — schema version + migration sentinels
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from istota.health.models import (
    Biomarker,
    BiomarkerRef,
    Panel,
    Stat,
)


logger = logging.getLogger(__name__)


SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    measured_at TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    source_ref INTEGER,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_stats_metric_date ON stats(metric, measured_at);
CREATE INDEX IF NOT EXISTS idx_stats_measured ON stats(measured_at);

CREATE TABLE IF NOT EXISTS panels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drawn_at TEXT NOT NULL,
    lab_name TEXT,
    panel_type TEXT,
    source_file TEXT,
    source_mime TEXT,
    ocr_text TEXT,
    draft INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    content_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_panels_drawn ON panels(drawn_at);
CREATE INDEX IF NOT EXISTS idx_panels_content_hash ON panels(content_hash);

CREATE TABLE IF NOT EXISTS biomarkers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id INTEGER NOT NULL REFERENCES panels(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    display_name TEXT,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    ref_range_low REAL,
    ref_range_high REAL,
    flag TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_biomarkers_panel ON biomarkers(panel_id);
CREATE INDEX IF NOT EXISTS idx_biomarkers_name ON biomarkers(name);

CREATE TABLE IF NOT EXISTS health_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS biomarker_explainers (
    name TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('high', 'low')),
    summary TEXT NOT NULL,
    causes_json TEXT NOT NULL DEFAULT '[]',
    mitigations_json TEXT NOT NULL DEFAULT '[]',
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (name, direction)
);

CREATE TABLE IF NOT EXISTS biomarker_refs (
    name TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    category TEXT NOT NULL,
    default_unit TEXT NOT NULL,
    ref_range_low REAL,
    ref_range_high REAL,
    ref_range_low_m REAL,
    ref_range_high_m REAL,
    ref_range_low_f REAL,
    ref_range_high_f REAL,
    aliases TEXT,
    description TEXT
);
"""


def init_db(db_path: Path) -> None:
    """Create / migrate the SQLite schema. Idempotent."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not db_path.exists()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        if fresh:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_SQL)
        _migrate_add_content_hash(conn)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    finally:
        conn.close()


def _migrate_add_content_hash(conn: sqlite3.Connection) -> None:
    """Add ``panels.content_hash`` on older DBs. Idempotent.

    Pre-existing rows are left NULL; backfill runs once via
    :func:`backfill_panel_content_hashes` from ``_migrate.ensure_initialised``.
    """
    # PRAGMA table_info returns: (cid, name, type, notnull, dflt_value, pk).
    # ``init_db`` opens the connection without a row factory, so index by
    # position rather than name.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(panels)")}
    if "content_hash" not in cols:
        conn.execute("ALTER TABLE panels ADD COLUMN content_hash TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_panels_content_hash "
            "ON panels(content_hash)",
        )


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a row-factory-equipped connection with FKs on."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- stats -------------------------------------------------------------------


def _row_to_stat(row: sqlite3.Row) -> Stat:
    return Stat(
        id=row["id"],
        measured_at=row["measured_at"],
        metric=row["metric"],
        value=float(row["value"]),
        unit=row["unit"],
        source=row["source"],
        source_ref=row["source_ref"],
        notes=row["notes"],
        created_at=row["created_at"],
    )


def insert_stat(
    conn: sqlite3.Connection,
    *,
    metric: str,
    value: float,
    unit: str,
    measured_at: str | None = None,
    source: str = "manual",
    source_ref: int | None = None,
    notes: str | None = None,
) -> int:
    """Insert a stat row. Returns the row id."""
    cur = conn.execute(
        """
        INSERT INTO stats(measured_at, metric, value, unit, source, source_ref, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (measured_at or _now(), metric, value, unit, source, source_ref, notes),
    )
    return int(cur.lastrowid)


def list_stats(
    conn: sqlite3.Connection,
    *,
    metric: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Stat]:
    clauses: list[str] = []
    params: list[Any] = []
    if metric:
        clauses.append("metric = ?")
        params.append(metric)
    if since:
        clauses.append("measured_at >= ?")
        params.append(since)
    if until:
        clauses.append("measured_at <= ?")
        params.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM stats {where} ORDER BY measured_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return [_row_to_stat(r) for r in rows]


def latest_stats(conn: sqlite3.Connection) -> dict[str, Stat]:
    """Latest stat per metric. Maps metric → :class:`Stat`."""
    rows = conn.execute(
        """
        SELECT * FROM stats
        WHERE id IN (
            SELECT MAX(id) FROM stats GROUP BY metric
        )
        """
    ).fetchall()
    return {r["metric"]: _row_to_stat(r) for r in rows}


def delete_stat(conn: sqlite3.Connection, stat_id: int) -> int:
    cur = conn.execute("DELETE FROM stats WHERE id = ?", (stat_id,))
    return cur.rowcount


def delete_stats_for_panel(conn: sqlite3.Connection, panel_id: int) -> int:
    """Delete stats rows derived from a panel (source='lab_panel')."""
    cur = conn.execute(
        "DELETE FROM stats WHERE source = 'lab_panel' AND source_ref = ?",
        (panel_id,),
    )
    return cur.rowcount


# -- panels ------------------------------------------------------------------


def _row_to_panel(row: sqlite3.Row) -> Panel:
    # Older rows (pre content_hash migration) won't have the column on a
    # row factory built before the ALTER. Guarded so the migration window
    # doesn't crash readers.
    try:
        content_hash = row["content_hash"]
    except (IndexError, KeyError):
        content_hash = None
    return Panel(
        id=row["id"],
        drawn_at=row["drawn_at"],
        lab_name=row["lab_name"],
        panel_type=row["panel_type"],
        source_file=row["source_file"],
        source_mime=row["source_mime"],
        ocr_text=row["ocr_text"],
        draft=bool(row["draft"]),
        notes=row["notes"],
        created_at=row["created_at"],
        content_hash=content_hash,
    )


def compute_content_hash(biomarkers: Iterable[Mapping[str, Any]]) -> str:
    """SHA-256 hex digest of a biomarker set's normalized content.

    Two panels with the same canonical biomarker rows produce the same
    hash regardless of insertion order, case, or trivial unit casing.
    Values are rounded to 6 significant figures so a CSV roundtrip
    (which formats with ``%g``) reproduces an identical hash.

    Returns the first 16 hex chars of the digest — collision-free for
    realistic per-user panel counts and short enough to be readable in
    logs.
    """
    rows: list[tuple[str, str, str]] = []
    for b in biomarkers:
        name = str(b.get("name") or "").strip().lower()
        unit = str(b.get("unit") or "").strip().lower()
        try:
            value = float(b.get("value") or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        rows.append((name, f"{value:.6g}", unit))
    rows.sort()
    blob = json.dumps(rows, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def set_panel_content_hash(
    conn: sqlite3.Connection, panel_id: int, content_hash: str | None,
) -> None:
    conn.execute(
        "UPDATE panels SET content_hash = ? WHERE id = ?",
        (content_hash, panel_id),
    )


def find_panel_by_content_hash(
    conn: sqlite3.Connection, content_hash: str,
) -> Panel | None:
    """Return any panel (draft or confirmed) matching this content hash."""
    row = conn.execute(
        "SELECT * FROM panels WHERE content_hash = ? ORDER BY id ASC LIMIT 1",
        (content_hash,),
    ).fetchone()
    if not row:
        return None
    return _row_to_panel(row)


def recompute_panel_content_hash(
    conn: sqlite3.Connection, panel_id: int,
) -> str | None:
    """Recompute and store the content_hash for a panel from its biomarkers.

    Returns the computed hash, or ``None`` if the panel has no biomarkers.
    """
    rows = conn.execute(
        "SELECT name, value, unit FROM biomarkers WHERE panel_id = ?",
        (panel_id,),
    ).fetchall()
    if not rows:
        set_panel_content_hash(conn, panel_id, None)
        return None
    h = compute_content_hash([dict(r) for r in rows])
    set_panel_content_hash(conn, panel_id, h)
    return h


def backfill_panel_content_hashes(conn: sqlite3.Connection) -> int:
    """Populate ``content_hash`` for every panel that doesn't have one.

    One-shot helper used by the migration in
    :func:`istota.health._migrate.ensure_initialised`. Returns the number
    of panels updated.
    """
    rows = conn.execute(
        "SELECT id FROM panels WHERE content_hash IS NULL",
    ).fetchall()
    n = 0
    for r in rows:
        if recompute_panel_content_hash(conn, int(r["id"])) is not None:
            n += 1
    return n


def insert_panel(
    conn: sqlite3.Connection,
    *,
    drawn_at: str,
    lab_name: str | None = None,
    panel_type: str | None = None,
    source_file: str | None = None,
    source_mime: str | None = None,
    ocr_text: str | None = None,
    draft: bool = False,
    notes: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO panels(
            drawn_at, lab_name, panel_type, source_file, source_mime,
            ocr_text, draft, notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            drawn_at, lab_name, panel_type, source_file, source_mime,
            ocr_text, 1 if draft else 0, notes,
        ),
    )
    return int(cur.lastrowid)


def get_panel(conn: sqlite3.Connection, panel_id: int) -> Panel | None:
    row = conn.execute("SELECT * FROM panels WHERE id = ?", (panel_id,)).fetchone()
    if not row:
        return None
    return _row_to_panel(row)


def list_panels(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    include_drafts: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> list[Panel]:
    clauses: list[str] = []
    params: list[Any] = []
    if since:
        clauses.append("drawn_at >= ?")
        params.append(since)
    if until:
        clauses.append("drawn_at <= ?")
        params.append(until)
    if not include_drafts:
        clauses.append("draft = 0")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM panels {where} ORDER BY drawn_at DESC, id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return [_row_to_panel(r) for r in rows]


def panel_counts(conn: sqlite3.Connection, panel_id: int) -> tuple[int, int]:
    """Return (biomarker_count, flagged_count) for a panel."""
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN flag IS NOT NULL AND flag != '' THEN 1 ELSE 0 END) AS flagged
        FROM biomarkers
        WHERE panel_id = ?
        """,
        (panel_id,),
    ).fetchone()
    return int(row["total"] or 0), int(row["flagged"] or 0)


def find_panel_collision(
    conn: sqlite3.Connection, *, drawn_at: str, lab_name: str | None,
) -> Panel | None:
    """Find an existing panel matching (drawn_at, lab_name).

    Used by the upload flow's collision check. Treats NULL lab_name and
    empty string as equal.
    """
    lab = lab_name or ""
    row = conn.execute(
        """
        SELECT * FROM panels
        WHERE drawn_at = ? AND COALESCE(lab_name, '') = ?
        ORDER BY id DESC LIMIT 1
        """,
        (drawn_at, lab),
    ).fetchone()
    if not row:
        return None
    return _row_to_panel(row)


def update_panel(
    conn: sqlite3.Connection,
    panel_id: int,
    *,
    drawn_at: str | None = None,
    lab_name: str | None = None,
    panel_type: str | None = None,
    notes: str | None = None,
    draft: bool | None = None,
) -> int:
    fields: list[str] = []
    params: list[Any] = []
    if drawn_at is not None:
        fields.append("drawn_at = ?")
        params.append(drawn_at)
    if lab_name is not None:
        fields.append("lab_name = ?")
        params.append(lab_name)
    if panel_type is not None:
        fields.append("panel_type = ?")
        params.append(panel_type)
    if notes is not None:
        fields.append("notes = ?")
        params.append(notes)
    if draft is not None:
        fields.append("draft = ?")
        params.append(1 if draft else 0)
    if not fields:
        return 0
    params.append(panel_id)
    cur = conn.execute(
        f"UPDATE panels SET {', '.join(fields)} WHERE id = ?", params,
    )
    return cur.rowcount


def delete_panel(conn: sqlite3.Connection, panel_id: int) -> int:
    cur = conn.execute("DELETE FROM panels WHERE id = ?", (panel_id,))
    return cur.rowcount


# -- biomarkers --------------------------------------------------------------


def _row_to_biomarker(row: sqlite3.Row) -> Biomarker:
    return Biomarker(
        id=row["id"],
        panel_id=row["panel_id"],
        name=row["name"],
        display_name=row["display_name"],
        value=float(row["value"]),
        unit=row["unit"],
        ref_range_low=row["ref_range_low"],
        ref_range_high=row["ref_range_high"],
        flag=row["flag"],
        created_at=row["created_at"],
    )


def insert_biomarker(
    conn: sqlite3.Connection,
    *,
    panel_id: int,
    name: str,
    value: float,
    unit: str,
    display_name: str | None = None,
    ref_range_low: float | None = None,
    ref_range_high: float | None = None,
    flag: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO biomarkers(
            panel_id, name, display_name, value, unit,
            ref_range_low, ref_range_high, flag
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            panel_id, name, display_name, value, unit,
            ref_range_low, ref_range_high, flag,
        ),
    )
    return int(cur.lastrowid)


def replace_biomarkers(
    conn: sqlite3.Connection,
    panel_id: int,
    biomarkers: list[dict],
) -> int:
    """Delete all biomarkers for a panel and insert the new list.

    Used by the OCR review flow when the user confirms / edits the extracted
    table. Also refreshes the panel's ``content_hash`` so dedup catches
    later imports of the same content. Returns the number of rows
    inserted.
    """
    conn.execute("DELETE FROM biomarkers WHERE panel_id = ?", (panel_id,))
    inserted = 0
    for b in biomarkers:
        insert_biomarker(
            conn,
            panel_id=panel_id,
            name=str(b["name"]),
            value=float(b["value"]),
            unit=str(b["unit"]),
            display_name=b.get("display_name"),
            ref_range_low=b.get("ref_range_low"),
            ref_range_high=b.get("ref_range_high"),
            flag=b.get("flag"),
        )
        inserted += 1
    recompute_panel_content_hash(conn, panel_id)
    return inserted


def list_biomarkers_for_panel(
    conn: sqlite3.Connection, panel_id: int,
) -> list[Biomarker]:
    rows = conn.execute(
        "SELECT * FROM biomarkers WHERE panel_id = ? ORDER BY name COLLATE NOCASE",
        (panel_id,),
    ).fetchall()
    return [_row_to_biomarker(r) for r in rows]


def biomarker_trend(
    conn: sqlite3.Connection,
    *,
    name: str,
    since: str | None = None,
    until: str | None = None,
) -> list[tuple[Biomarker, str]]:
    """Time series for a biomarker.

    Excludes draft panels so unreviewed extractions don't pollute the
    trend. Returns ``[(biomarker, drawn_at), …]`` sorted ascending.
    """
    clauses = ["b.name = ?", "p.draft = 0"]
    params: list[Any] = [name]
    if since:
        clauses.append("p.drawn_at >= ?")
        params.append(since)
    if until:
        clauses.append("p.drawn_at <= ?")
        params.append(until)
    rows = conn.execute(
        f"""
        SELECT b.*, p.drawn_at AS panel_drawn_at
        FROM biomarkers b
        JOIN panels p ON p.id = b.panel_id
        WHERE {' AND '.join(clauses)}
        ORDER BY p.drawn_at ASC, b.id ASC
        """,
        params,
    ).fetchall()
    out: list[tuple[Biomarker, str]] = []
    for r in rows:
        out.append((_row_to_biomarker(r), r["panel_drawn_at"]))
    return out


def flagged_biomarkers_latest(
    conn: sqlite3.Connection, limit: int = 50,
) -> list[tuple[Biomarker, Panel]]:
    """Most recent flagged biomarker per name (across confirmed panels)."""
    rows = conn.execute(
        """
        SELECT b.*, p.drawn_at AS panel_drawn_at, p.lab_name AS panel_lab,
               p.panel_type AS panel_type, p.id AS panel_id_full
        FROM biomarkers b
        JOIN panels p ON p.id = b.panel_id
        WHERE p.draft = 0
          AND b.flag IS NOT NULL AND b.flag != ''
          AND b.id IN (
            SELECT MAX(b2.id) FROM biomarkers b2
            JOIN panels p2 ON p2.id = b2.panel_id
            WHERE p2.draft = 0
              AND b2.flag IS NOT NULL AND b2.flag != ''
            GROUP BY b2.name
          )
        ORDER BY p.drawn_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[tuple[Biomarker, Panel]] = []
    for r in rows:
        b = _row_to_biomarker(r)
        p = Panel(
            id=r["panel_id_full"],
            drawn_at=r["panel_drawn_at"],
            lab_name=r["panel_lab"],
            panel_type=r["panel_type"],
            source_file=None,
            source_mime=None,
            ocr_text=None,
            draft=False,
            notes=None,
        )
        out.append((b, p))
    return out


# -- biomarker_refs ----------------------------------------------------------


def _row_to_ref(row: sqlite3.Row) -> BiomarkerRef:
    aliases_raw = row["aliases"] or ""
    try:
        aliases = json.loads(aliases_raw) if aliases_raw else []
    except (ValueError, TypeError):
        aliases = []
    return BiomarkerRef(
        name=row["name"],
        display_name=row["display_name"],
        category=row["category"],
        default_unit=row["default_unit"],
        ref_range_low=row["ref_range_low"],
        ref_range_high=row["ref_range_high"],
        ref_range_low_m=row["ref_range_low_m"],
        ref_range_high_m=row["ref_range_high_m"],
        ref_range_low_f=row["ref_range_low_f"],
        ref_range_high_f=row["ref_range_high_f"],
        aliases=list(aliases),
        description=row["description"],
    )


def upsert_biomarker_ref(conn: sqlite3.Connection, ref: dict) -> None:
    aliases_json = json.dumps(ref.get("aliases") or [])
    conn.execute(
        """
        INSERT INTO biomarker_refs(
            name, display_name, category, default_unit,
            ref_range_low, ref_range_high,
            ref_range_low_m, ref_range_high_m,
            ref_range_low_f, ref_range_high_f,
            aliases, description
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            display_name = excluded.display_name,
            category = excluded.category,
            default_unit = excluded.default_unit,
            ref_range_low = excluded.ref_range_low,
            ref_range_high = excluded.ref_range_high,
            ref_range_low_m = excluded.ref_range_low_m,
            ref_range_high_m = excluded.ref_range_high_m,
            ref_range_low_f = excluded.ref_range_low_f,
            ref_range_high_f = excluded.ref_range_high_f,
            aliases = excluded.aliases,
            description = excluded.description
        """,
        (
            ref["name"], ref["display_name"], ref["category"], ref["default_unit"],
            ref.get("ref_range_low"), ref.get("ref_range_high"),
            ref.get("ref_range_low_m"), ref.get("ref_range_high_m"),
            ref.get("ref_range_low_f"), ref.get("ref_range_high_f"),
            aliases_json, ref.get("description"),
        ),
    )


def list_biomarker_refs(conn: sqlite3.Connection) -> list[BiomarkerRef]:
    rows = conn.execute(
        "SELECT * FROM biomarker_refs ORDER BY category, display_name COLLATE NOCASE",
    ).fetchall()
    return [_row_to_ref(r) for r in rows]


def get_biomarker_ref(
    conn: sqlite3.Connection, name: str,
) -> BiomarkerRef | None:
    row = conn.execute(
        "SELECT * FROM biomarker_refs WHERE name = ?", (name,),
    ).fetchone()
    if not row:
        return None
    return _row_to_ref(row)


def find_biomarker_ref_by_alias(
    conn: sqlite3.Connection, candidate: str,
) -> BiomarkerRef | None:
    """Match by canonical name first, then by any alias (case-insensitive)."""
    direct = get_biomarker_ref(conn, candidate)
    if direct:
        return direct
    needle = candidate.strip().lower()
    if not needle:
        return None
    for ref in list_biomarker_refs(conn):
        if ref.name.lower() == needle:
            return ref
        for a in ref.aliases:
            if a.lower() == needle:
                return ref
    return None


# -- health_settings ---------------------------------------------------------


# Whitelisted keys with the JSON shape they expect. The API copies values
# verbatim; validation/coercion stays in the route layer.
SETTINGS_KEYS = ("dob", "height_cm", "sex", "display_units")


def get_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT key, value FROM health_settings",
    ).fetchall()
    out: dict[str, Any] = {}
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except (ValueError, TypeError):
            out[r["key"]] = r["value"]
    return out


def set_setting(
    conn: sqlite3.Connection, key: str, value: Any,
) -> None:
    """Upsert a single setting. Value is JSON-encoded."""
    conn.execute(
        """
        INSERT INTO health_settings(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, json.dumps(value)),
    )


def delete_setting(conn: sqlite3.Connection, key: str) -> int:
    cur = conn.execute("DELETE FROM health_settings WHERE key = ?", (key,))
    return cur.rowcount


# -- biomarker_explainers ----------------------------------------------------


def get_biomarker_explainer(
    conn: sqlite3.Connection, name: str, direction: str,
) -> dict | None:
    """Return the cached explainer for a biomarker × direction, or None."""
    row = conn.execute(
        """
        SELECT name, direction, summary, causes_json, mitigations_json, generated_at
        FROM biomarker_explainers WHERE name = ? AND direction = ?
        """,
        (name, direction),
    ).fetchone()
    if not row:
        return None
    try:
        causes = json.loads(row["causes_json"] or "[]")
    except (ValueError, TypeError):
        causes = []
    try:
        mitigations = json.loads(row["mitigations_json"] or "[]")
    except (ValueError, TypeError):
        mitigations = []
    return {
        "name": row["name"],
        "direction": row["direction"],
        "summary": row["summary"],
        "causes": list(causes) if isinstance(causes, list) else [],
        "mitigations": list(mitigations) if isinstance(mitigations, list) else [],
        "generated_at": row["generated_at"],
    }


def save_biomarker_explainer(
    conn: sqlite3.Connection,
    *,
    name: str,
    direction: str,
    summary: str,
    causes: list[str],
    mitigations: list[str],
) -> None:
    """Upsert an explainer payload. Callers commit."""
    conn.execute(
        """
        INSERT INTO biomarker_explainers(
            name, direction, summary, causes_json, mitigations_json
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(name, direction) DO UPDATE SET
            summary = excluded.summary,
            causes_json = excluded.causes_json,
            mitigations_json = excluded.mitigations_json,
            generated_at = datetime('now')
        """,
        (
            name, direction, summary,
            json.dumps(causes or []),
            json.dumps(mitigations or []),
        ),
    )
