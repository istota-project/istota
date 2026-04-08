"""Temporal knowledge graph — entity-relationship triples with validity windows.

Stores structured facts extracted from conversations. Each fact has a subject,
predicate, object triple with optional temporal bounds (valid_from, valid_until).

Single-valued predicates (works_at, lives_in, has_role, has_status) automatically
supersede existing facts when a new value is inserted. Multi-valued predicates
(uses_tech, knows, prefers) allow concurrent facts.

Temporary facts (e.g., "staying_in warsaw") coexist with permanent facts and
never trigger supersession.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger("istota.knowledge_graph")

SINGLE_VALUED_PREDICATES = frozenset({
    "works_at",
    "lives_in",
    "has_role",
    "has_status",
})

TEMPORARY_PREDICATES = frozenset({
    "staying_in",
    "visiting",
})


@dataclass
class KnowledgeFact:
    id: int
    user_id: str
    subject: str
    predicate: str
    object: str
    valid_from: str | None = None
    valid_until: str | None = None
    temporary: bool = False
    confidence: float = 1.0
    source_task_id: int | None = None
    source_type: str = "extracted"
    created_at: str = ""
    updated_at: str = ""


def _normalize(text: str) -> str:
    """Normalize entity/predicate text: lowercase, stripped."""
    return text.strip().lower()


def _row_to_fact(row: sqlite3.Row) -> KnowledgeFact:
    return KnowledgeFact(
        id=row["id"],
        user_id=row["user_id"],
        subject=row["subject"],
        predicate=row["predicate"],
        object=row["object"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        temporary=bool(row["temporary"]),
        confidence=row["confidence"],
        source_task_id=row["source_task_id"],
        source_type=row["source_type"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def ensure_table(conn: sqlite3.Connection) -> None:
    """Create the knowledge_facts table if it doesn't exist.

    Uses individual execute() calls instead of executescript() to avoid
    implicitly committing pending transactions on the shared connection.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            valid_from TEXT,
            valid_until TEXT,
            temporary INTEGER DEFAULT 0,
            confidence REAL DEFAULT 1.0,
            source_task_id INTEGER,
            source_type TEXT DEFAULT 'extracted',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_kf_user_subject
            ON knowledge_facts(user_id, subject)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_kf_user_predicate
            ON knowledge_facts(user_id, predicate)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_kf_current
            ON knowledge_facts(user_id, valid_until)
            WHERE valid_until IS NULL
    """)


def add_fact(
    conn: sqlite3.Connection,
    user_id: str,
    subject: str,
    predicate: str,
    object_val: str,
    valid_from: str | None = None,
    valid_until: str | None = None,
    temporary: bool = False,
    confidence: float = 1.0,
    source_task_id: int | None = None,
    source_type: str = "extracted",
) -> int | None:
    """Add a fact with dedup and supersession logic.

    Returns the new fact's ID, or None if it was a duplicate.

    Dedup rules:
    - Same (user_id, subject, predicate, object) with overlapping validity → skip
    - For single-valued predicates: different object with no valid_until on existing
      → supersede (set valid_until on old, insert new)
    - Temporary facts never trigger supersession of permanent facts
    """
    subject = _normalize(subject)
    predicate = _normalize(predicate)
    object_val = _normalize(object_val)

    if predicate in TEMPORARY_PREDICATES:
        temporary = True

    # Check for exact duplicate (same subject+predicate+object, still current)
    today = date.today().isoformat()
    existing = conn.execute(
        "SELECT id FROM knowledge_facts "
        "WHERE user_id = ? AND subject = ? AND predicate = ? AND object = ? "
        "AND (valid_until IS NULL OR valid_until > ?)",
        (user_id, subject, predicate, object_val, today),
    ).fetchone()

    if existing:
        return None  # Duplicate

    # Supersession for single-valued predicates (only permanent facts supersede)
    if predicate in SINGLE_VALUED_PREDICATES and not temporary:
        today = valid_from or date.today().isoformat()
        conn.execute(
            "UPDATE knowledge_facts SET valid_until = ?, updated_at = datetime('now') "
            "WHERE user_id = ? AND subject = ? AND predicate = ? "
            "AND valid_until IS NULL AND temporary = 0",
            (today, user_id, subject, predicate),
        )

    cursor = conn.execute(
        "INSERT INTO knowledge_facts "
        "(user_id, subject, predicate, object, valid_from, valid_until, "
        "temporary, confidence, source_task_id, source_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, subject, predicate, object_val, valid_from, valid_until,
         int(temporary), confidence, source_task_id, source_type),
    )
    return cursor.lastrowid


def invalidate_fact(
    conn: sqlite3.Connection,
    fact_id: int,
    ended: str | None = None,
) -> bool:
    """Mark a fact as no longer valid. Returns True if updated."""
    ended = ended or date.today().isoformat()
    cursor = conn.execute(
        "UPDATE knowledge_facts SET valid_until = ?, updated_at = datetime('now') "
        "WHERE id = ? AND valid_until IS NULL",
        (ended, fact_id),
    )
    return cursor.rowcount > 0


def delete_fact(conn: sqlite3.Connection, fact_id: int) -> bool:
    """Hard delete a fact. Returns True if deleted."""
    cursor = conn.execute("DELETE FROM knowledge_facts WHERE id = ?", (fact_id,))
    return cursor.rowcount > 0


def get_current_facts(
    conn: sqlite3.Connection,
    user_id: str,
    subject: str | None = None,
    predicate: str | None = None,
) -> list[KnowledgeFact]:
    """Get all currently valid facts, optionally filtered."""
    today = date.today().isoformat()
    sql = (
        "SELECT * FROM knowledge_facts WHERE user_id = ? "
        "AND (valid_until IS NULL OR valid_until > ?)"
    )
    params: list = [user_id, today]

    if subject:
        sql += " AND subject = ?"
        params.append(_normalize(subject))
    if predicate:
        sql += " AND predicate = ?"
        params.append(_normalize(predicate))

    sql += " ORDER BY subject, predicate, created_at"
    return [_row_to_fact(row) for row in conn.execute(sql, params)]


def get_facts_as_of(
    conn: sqlite3.Connection,
    user_id: str,
    as_of: str,
    subject: str | None = None,
) -> list[KnowledgeFact]:
    """Get facts valid at a specific date."""
    sql = (
        "SELECT * FROM knowledge_facts WHERE user_id = ? "
        "AND (valid_from IS NULL OR valid_from <= ?) "
        "AND (valid_until IS NULL OR valid_until > ?)"
    )
    params: list = [user_id, as_of, as_of]

    if subject:
        sql += " AND subject = ?"
        params.append(_normalize(subject))

    sql += " ORDER BY subject, predicate, created_at"
    return [_row_to_fact(row) for row in conn.execute(sql, params)]


def get_entity_timeline(
    conn: sqlite3.Connection,
    user_id: str,
    subject: str,
) -> list[KnowledgeFact]:
    """Get all facts (current + historical) for an entity, chronological."""
    subject = _normalize(subject)
    rows = conn.execute(
        "SELECT * FROM knowledge_facts "
        "WHERE user_id = ? AND subject = ? "
        "ORDER BY COALESCE(valid_from, created_at), created_at",
        (user_id, subject),
    )
    return [_row_to_fact(row) for row in rows]


def get_fact(conn: sqlite3.Connection, fact_id: int) -> KnowledgeFact | None:
    """Get a single fact by ID."""
    row = conn.execute(
        "SELECT * FROM knowledge_facts WHERE id = ?", (fact_id,)
    ).fetchone()
    return _row_to_fact(row) if row else None


def get_fact_count(conn: sqlite3.Connection, user_id: str) -> dict:
    """Get fact counts for a user."""
    total = conn.execute(
        "SELECT COUNT(*) FROM knowledge_facts WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    current = conn.execute(
        "SELECT COUNT(*) FROM knowledge_facts WHERE user_id = ? AND valid_until IS NULL",
        (user_id,),
    ).fetchone()[0]
    return {"total": total, "current": current, "historical": total - current}


def format_facts_for_prompt(facts: list[KnowledgeFact]) -> str:
    """Format facts as a prompt section."""
    if not facts:
        return ""
    lines = []
    for f in facts:
        line = f"- {f.subject} {f.predicate} {f.object}"
        if f.valid_from:
            line += f" (since {f.valid_from})"
        if f.temporary:
            line += " [temporary]"
        lines.append(line)
    return "\n".join(lines)
