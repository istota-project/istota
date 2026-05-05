"""Temporal knowledge graph — entity-relationship triples with validity windows.

Stores structured facts extracted from conversations. Each fact has a subject,
predicate, object triple with optional temporal bounds (valid_from, valid_until).

Single-valued predicates (works_at, lives_in, has_role, has_status) automatically
supersede existing facts when a new value is inserted. Multi-valued predicates
(uses_tech, knows, prefers) allow concurrent facts.

Temporary facts (e.g., "staying_in warsaw") coexist with permanent facts and
never trigger supersession.

Mutations are also written to `knowledge_facts_audit` so users can inspect
why a fact ended up in (or got out of) the graph. Audit ops:

  - insert: a new fact landed in the table.
  - fuzzy_dedup_skip: a near-duplicate of an existing fact was dropped before
    it ever made it into the table; useful for tuning the dedup threshold.
  - supersede: a single-valued predicate's previous value got `valid_until`
    set as part of inserting a new value.
  - invalidate: an existing fact got `valid_until` set explicitly via
    invalidate_fact.
  - delete: a row was hard-deleted.

`before_json` is null for `insert` and `fuzzy_dedup_skip` (no prior row);
for `supersede`/`invalidate`/`delete` it captures the row's full state
before the mutation. `after_json` is the post-mutation state, or null when
the op was a skip/delete.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

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


def _fact_similarity(text_a: str, text_b: str) -> float:
    """Word-level Jaccard similarity between two whitespace-tokenized strings.

    Used for object-only comparison after a predicate-equality gate. The
    predicate must NOT be folded into the input — that produces false
    positives between opposing-verb facts on the same object (e.g.
    `acquired X` vs `disposed_of X` would collide on the shared object
    tokens alone if compared together with the verb).
    """
    words_a = set(text_a.split())
    words_b = set(text_b.split())
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

    # Audit trail. Wired into the unified retention sweep (see
    # cleanup_old_audit_rows) — the audit defaults to 4× the user retention
    # window, on the theory that "I want to know why my fact disappeared two
    # weeks ago" outweighs the table-size cost.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_facts_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            fact_id INTEGER,
            op TEXT NOT NULL,
            before_json TEXT,
            after_json TEXT,
            source_task_id INTEGER,
            source_type TEXT,
            ts TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_kfa_user_ts
            ON knowledge_facts_audit(user_id, ts)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_kfa_fact_id
            ON knowledge_facts_audit(fact_id)
    """)


def _fact_to_json(fact: KnowledgeFact | None) -> str | None:
    """JSON-serialize a KnowledgeFact for audit storage."""
    if fact is None:
        return None
    try:
        return json.dumps(asdict(fact), default=str)
    except Exception:
        return None


def _audit_dict_json(d: dict | None) -> str | None:
    """JSON-serialize an arbitrary fact-shaped dict for audit storage."""
    if d is None:
        return None
    try:
        return json.dumps(d, default=str)
    except Exception:
        return None


def _write_audit_row(
    conn: sqlite3.Connection,
    user_id: str,
    fact_id: int | None,
    op: str,
    before_json: str | None,
    after_json: str | None,
    source_task_id: int | None,
    source_type: str | None,
) -> None:
    """Append a row to knowledge_facts_audit. Best-effort — never raises."""
    try:
        conn.execute(
            "INSERT INTO knowledge_facts_audit "
            "(user_id, fact_id, op, before_json, after_json, source_task_id, source_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, fact_id, op, before_json, after_json, source_task_id, source_type),
        )
    except sqlite3.OperationalError as e:
        # Table may not exist on a freshly imported module before ensure_table.
        logger.debug("audit insert failed: %s", e)


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

    # Fuzzy dedup: catch near-duplicate facts for the same subject, but
    # ONLY when the new and existing facts share a predicate. Predicates
    # carry meaning; comparing across predicates conflates opposing
    # statements about the same object (e.g. `acquired pilot prera` vs
    # `disposed_of pilot prera`) and silently drops the second.
    #
    # Two-stage check, both scoped to identical predicates and operating
    # on object tokens only:
    #   1. Token-subset fast-path — one object's tokens are a subset of
    #      the other's. Catches "python" ⊂ "python 3" and "acme" ⊂
    #      "acme corp"; rejects substring-only collisions like "tech_1"
    #      inside "tech_10" (different tokens, neither a subset).
    #   2. Word-level Jaccard on objects ≥ 0.6 (paired with audit
    #      logging via op="fuzzy_dedup_skip" so dedup quality is
    #      observable and tunable, not silent).
    FUZZY_DEDUP_THRESHOLD = 0.6
    near_matches = conn.execute(
        "SELECT id, predicate, object FROM knowledge_facts "
        "WHERE user_id = ? AND subject = ? AND predicate = ? "
        "AND (valid_until IS NULL OR valid_until > ?)",
        (user_id, subject, predicate, today),
    ).fetchall()
    new_obj_tokens = set(object_val.split())
    for row in near_matches:
        # Predicates are guaranteed equal by the WHERE clause above.
        is_token_subset = False
        if object_val and row[2]:
            existing_obj_tokens = set(row[2].split())
            if new_obj_tokens and existing_obj_tokens:
                is_token_subset = (
                    new_obj_tokens <= existing_obj_tokens
                    or existing_obj_tokens <= new_obj_tokens
                )
        if is_token_subset or _fact_similarity(object_val, row[2]) >= FUZZY_DEDUP_THRESHOLD:
            new_sig = f"{predicate} {object_val}".strip()
            existing_sig = f"{row[1]} {row[2]}".strip()
            logger.debug(
                "Skipping near-duplicate fact: '%s' ≈ '%s'", new_sig, existing_sig
            )
            # Surface the skip so users can inspect dedup quality. `fact_id`
            # is the existing match the candidate collided with.
            _write_audit_row(
                conn, user_id, row[0], "fuzzy_dedup_skip",
                before_json=None,
                after_json=_audit_dict_json({
                    "subject": subject,
                    "predicate": predicate,
                    "object": object_val,
                    "valid_from": valid_from,
                    "valid_until": valid_until,
                    "temporary": int(temporary),
                    "matched_predicate": row[1],
                    "matched_object": row[2],
                    "matched_fact_id": row[0],
                    "match_type": "token_subset" if is_token_subset else "jaccard",
                }),
                source_task_id=source_task_id,
                source_type=source_type,
            )
            return None

    # Supersession for single-valued predicates (only permanent facts supersede)
    if predicate in SINGLE_VALUED_PREDICATES and not temporary:
        today = valid_from or date.today().isoformat()
        # Capture pre-update state for audit before mutating.
        superseded_rows = conn.execute(
            "SELECT * FROM knowledge_facts "
            "WHERE user_id = ? AND subject = ? AND predicate = ? "
            "AND valid_until IS NULL AND temporary = 0",
            (user_id, subject, predicate),
        ).fetchall()
        conn.execute(
            "UPDATE knowledge_facts SET valid_until = ?, updated_at = datetime('now') "
            "WHERE user_id = ? AND subject = ? AND predicate = ? "
            "AND valid_until IS NULL AND temporary = 0",
            (today, user_id, subject, predicate),
        )
        for prev_row in superseded_rows:
            prev_fact = _row_to_fact(prev_row)
            after_fact_row = conn.execute(
                "SELECT * FROM knowledge_facts WHERE id = ?", (prev_fact.id,)
            ).fetchone()
            after_fact = _row_to_fact(after_fact_row) if after_fact_row else None
            _write_audit_row(
                conn, user_id, prev_fact.id, "supersede",
                before_json=_fact_to_json(prev_fact),
                after_json=_fact_to_json(after_fact),
                source_task_id=source_task_id,
                source_type=source_type,
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

    new_id = cursor.lastrowid
    new_row = conn.execute(
        "SELECT * FROM knowledge_facts WHERE id = ?", (new_id,)
    ).fetchone()
    new_fact = _row_to_fact(new_row) if new_row else None
    _write_audit_row(
        conn, user_id, new_id, "insert",
        before_json=None,
        after_json=_fact_to_json(new_fact),
        source_task_id=source_task_id,
        source_type=source_type,
    )
    return new_id


def invalidate_fact(
    conn: sqlite3.Connection,
    fact_id: int,
    ended: str | None = None,
) -> bool:
    """Mark a fact as no longer valid. Returns True if updated."""
    ended = ended or date.today().isoformat()
    before_row = conn.execute(
        "SELECT * FROM knowledge_facts WHERE id = ? AND valid_until IS NULL",
        (fact_id,),
    ).fetchone()
    cursor = conn.execute(
        "UPDATE knowledge_facts SET valid_until = ?, updated_at = datetime('now') "
        "WHERE id = ? AND valid_until IS NULL",
        (ended, fact_id),
    )
    if cursor.rowcount > 0 and before_row is not None:
        before_fact = _row_to_fact(before_row)
        after_row = conn.execute(
            "SELECT * FROM knowledge_facts WHERE id = ?", (fact_id,)
        ).fetchone()
        after_fact = _row_to_fact(after_row) if after_row else None
        _write_audit_row(
            conn, before_fact.user_id, fact_id, "invalidate",
            before_json=_fact_to_json(before_fact),
            after_json=_fact_to_json(after_fact),
            source_task_id=None,
            source_type=None,
        )
    return cursor.rowcount > 0


def delete_fact(conn: sqlite3.Connection, fact_id: int) -> bool:
    """Hard delete a fact. Returns True if deleted."""
    before_row = conn.execute(
        "SELECT * FROM knowledge_facts WHERE id = ?", (fact_id,)
    ).fetchone()
    cursor = conn.execute("DELETE FROM knowledge_facts WHERE id = ?", (fact_id,))
    if cursor.rowcount > 0 and before_row is not None:
        before_fact = _row_to_fact(before_row)
        _write_audit_row(
            conn, before_fact.user_id, fact_id, "delete",
            before_json=_fact_to_json(before_fact),
            after_json=None,
            source_task_id=None,
            source_type=None,
        )
    return cursor.rowcount > 0


@dataclass
class AuditRow:
    id: int
    user_id: str
    fact_id: int | None
    op: str
    before_json: str | None
    after_json: str | None
    source_task_id: int | None
    source_type: str | None
    ts: str


def get_fact_history(
    conn: sqlite3.Connection,
    user_id: str,
    entity: str | None = None,
    since: str | None = None,
    limit: int = 200,
) -> list[AuditRow]:
    """Return audit rows for a user, newest first.

    `entity` filters to rows whose pre/post snapshot mentions the entity as
    subject or object (substring match on the JSON column — coarse but
    cheap, matches CLI ergonomics).
    `since` is an ISO date/datetime; only rows with `ts >= since` are returned.
    """
    sql = "SELECT * FROM knowledge_facts_audit WHERE user_id = ?"
    params: list = [user_id]
    if entity:
        ent = entity.strip().lower()
        sql += " AND (before_json LIKE ? OR after_json LIKE ?)"
        # Match `"subject": "X"` or `"object": "X"` and also tolerate the
        # JSON containing the bare token.
        like = f"%{ent}%"
        params.extend([like, like])
    if since:
        sql += " AND ts >= ?"
        params.append(since)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        AuditRow(
            id=r["id"], user_id=r["user_id"], fact_id=r["fact_id"], op=r["op"],
            before_json=r["before_json"], after_json=r["after_json"],
            source_task_id=r["source_task_id"], source_type=r["source_type"],
            ts=r["ts"],
        )
        for r in rows
    ]


def cleanup_old_audit_rows(
    conn: sqlite3.Connection,
    user_id: str,
    retention_days: int,
) -> int:
    """Delete audit rows older than `retention_days` for `user_id`.

    `retention_days <= 0` is a no-op (matches the cleanup_old_chunks
    convention). Wired into the unified retention sweep at 4× the
    user-level memory retention, so users can audit longer than they
    can recall.
    """
    if retention_days <= 0:
        return 0
    cutoff = (
        (datetime.now(timezone.utc) - timedelta(days=retention_days))
        .replace(tzinfo=None)
        .strftime("%Y-%m-%d %H:%M:%S")
    )
    cursor = conn.execute(
        "DELETE FROM knowledge_facts_audit WHERE user_id = ? AND ts < ?",
        (user_id, cutoff),
    )
    return cursor.rowcount or 0


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
    in the prompt text.

    When `max_facts > 0`, identity facts are kept whole — they are anchors and
    truncating them silently is the worst outcome. Up to `max_facts // 2`
    slots are reserved for identity facts when filling alongside matched
    facts; if identity alone exceeds the cap, identity wins and matched
    facts are dropped. Within matched, sort by `updated_at` desc so the most
    recently changed facts surface first; ties broken by `created_at` desc.
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

    if max_facts <= 0:
        return identity + matched

    # Identity is sacred — never truncate it. If identity alone exceeds the
    # cap, return identity in full and drop everything else; the user's KG
    # state is over-budget and should be audited rather than silently clipped.
    if len(identity) >= max_facts:
        if matched:
            logger.debug(
                "KG facts truncated from prompt: %d matched dropped (identity at cap)",
                len(matched),
            )
        return identity

    # Otherwise, reserve up to half the cap for identity but use what's
    # actually there, and fill the rest with matched facts sorted by recency.
    matched.sort(
        key=lambda f: (f.updated_at or "", f.created_at or ""),
        reverse=True,
    )
    remaining = max_facts - len(identity)
    if len(matched) > remaining:
        logger.debug(
            "KG facts truncated from prompt: %d matched dropped (cap=%d)",
            len(matched) - remaining, max_facts,
        )
    return identity + matched[:remaining]


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
