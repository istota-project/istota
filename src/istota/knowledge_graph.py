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


def _fact_similarity(pred_obj_a: str, pred_obj_b: str) -> float:
    """Word-level Jaccard similarity between two 'predicate object' strings."""
    words_a = set(pred_obj_a.split())
    words_b = set(pred_obj_b.split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


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
    try:
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_kf_unique_current
                ON knowledge_facts(user_id, subject, predicate, object)
                WHERE valid_until IS NULL
        """)
    except sqlite3.IntegrityError:
        logger.warning(
            "Cannot create unique index on knowledge_facts — "
            "duplicate current facts exist. Run dedup migration first."
        )


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

    # Fuzzy dedup: catch near-duplicate predicate+object for same subject
    # (e.g., "allergic_to tree_nuts" vs "allergic_to tree nuts")
    FUZZY_DEDUP_THRESHOLD = 0.7
    new_sig = f"{predicate} {object_val}"
    near_matches = conn.execute(
        "SELECT predicate, object FROM knowledge_facts "
        "WHERE user_id = ? AND subject = ? "
        "AND (valid_until IS NULL OR valid_until > ?)",
        (user_id, subject, today),
    ).fetchall()
    for row in near_matches:
        existing_sig = f"{row[0]} {row[1]}"
        if _fact_similarity(new_sig, existing_sig) >= FUZZY_DEDUP_THRESHOLD:
            logger.debug(
                "Skipping near-duplicate fact: '%s' ≈ '%s'", new_sig, existing_sig
            )
            return None

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
        "INSERT OR IGNORE INTO knowledge_facts "
        "(user_id, subject, predicate, object, valid_from, valid_until, "
        "temporary, confidence, source_task_id, source_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, subject, predicate, object_val, valid_from, valid_until,
         int(temporary), confidence, source_task_id, source_type),
    )
    if cursor.rowcount == 0:
        return None  # Unique index caught a race-condition duplicate
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


def _tokenize(text: str) -> set[str]:
    """Extract lowercase word tokens from text, splitting on non-alphanumeric."""
    import re
    return set(re.findall(r"[a-z0-9]+(?:_[a-z0-9]+)*", text.lower()))


def select_relevant_facts(
    facts: list[KnowledgeFact],
    prompt: str,
    user_id: str,
    max_facts: int = 0,
) -> list[KnowledgeFact]:
    """Filter facts to those relevant to the prompt.

    Always includes facts where the subject matches user_id (identity facts).
    Remaining facts are included if their subject or object appears as a token
    in the prompt text. When max_facts > 0, caps total after filtering
    (identity facts prioritized, then by creation date descending).
    """
    if not facts:
        return []

    prompt_tokens = _tokenize(prompt)
    user_id_lower = user_id.lower()

    identity: list[KnowledgeFact] = []
    matched: list[KnowledgeFact] = []

    for fact in facts:
        if fact.subject == user_id_lower:
            identity.append(fact)
            continue

        # Check if subject or object tokens appear in the prompt
        subject_tokens = _tokenize(fact.subject)
        object_tokens = _tokenize(fact.object)

        if subject_tokens & prompt_tokens or object_tokens & prompt_tokens:
            matched.append(fact)

    result = identity + matched

    if max_facts > 0 and len(result) > max_facts:
        # Keep identity facts first, then most recent matched
        matched.sort(key=lambda f: f.created_at, reverse=True)
        remaining = max_facts - len(identity)
        if remaining > 0:
            result = identity + matched[:remaining]
        else:
            # Even identity alone exceeds cap — truncate identity by recency
            identity.sort(key=lambda f: f.created_at, reverse=True)
            result = identity[:max_facts]

    return result


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
