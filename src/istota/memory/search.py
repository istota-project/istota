"""Semantic memory search — hybrid BM25 + vector search over conversations and memory files.

Gracefully degrades to BM25-only if sqlite-vec or sentence-transformers is unavailable.
"""

import hashlib
import json
import logging
import re
import sqlite3
import struct
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("istota.memory_search")

# Lazy-loaded embedding model singleton
_model = None
_vec_available = None


@dataclass
class MemoryChunk:
    id: int
    user_id: str
    source_type: str
    source_id: str
    chunk_index: int
    content: str
    content_hash: str
    metadata: dict = field(default_factory=dict)
    created_at: str = ""


@dataclass
class SearchResult:
    chunk_id: int
    content: str
    score: float
    source_type: str
    source_id: str
    metadata: dict = field(default_factory=dict)
    bm25_rank: int | None = None
    vec_rank: int | None = None


# ---------------------------------------------------------------------------
# Embedding helpers (lazy-loaded)
# ---------------------------------------------------------------------------

def _get_model():
    """Load sentence-transformers model on first call. Returns None if unavailable."""
    global _model
    if _model is not None:
        return _model
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("Loaded embedding model: all-MiniLM-L6-v2")
        return _model
    except ImportError:
        logger.warning("sentence-transformers not installed, vector search unavailable")
        return None
    except Exception as e:
        logger.warning("Failed to load embedding model: %s", e)
        return None


def embed_text(text: str) -> list[float] | None:
    """Embed a single text string. Returns None if model unavailable."""
    model = _get_model()
    if model is None:
        return None
    embedding = model.encode(text, normalize_embeddings=True)
    return embedding.tolist()


def embed_batch(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch of texts. Returns None if model unavailable."""
    if not texts:
        return []
    model = _get_model()
    if model is None:
        return None
    embeddings = model.encode(texts, normalize_embeddings=True)
    return [e.tolist() for e in embeddings]


def _serialize_embedding(embedding: list[float]) -> bytes:
    """Serialize embedding to bytes for sqlite-vec storage."""
    return struct.pack(f"{len(embedding)}f", *embedding)


# ---------------------------------------------------------------------------
# sqlite-vec helpers
# ---------------------------------------------------------------------------

def enable_vec_extension(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec extension. Returns True if successful."""
    global _vec_available
    if _vec_available is not None:
        if not _vec_available:
            return False
        # Extension was previously available, but this is a new connection
        # so we need to load it again.

    try:
        import sqlite_vec
        sqlite_vec.load(conn)
        _vec_available = True
        return True
    except ImportError:
        logger.debug("sqlite-vec not installed, vector search unavailable")
        _vec_available = False
        return False
    except Exception as e:
        logger.debug("Failed to load sqlite-vec: %s", e)
        _vec_available = False
        return False


def ensure_vec_table(conn: sqlite3.Connection) -> bool:
    """Create vec0 virtual table if missing. Returns True if table exists after call."""
    if not enable_vec_extension(conn):
        return False

    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_vec "
            "USING vec0(chunk_id INTEGER PRIMARY KEY, embedding FLOAT[384])"
        )
        conn.commit()
        return True
    except Exception as e:
        logger.warning("Failed to create vec table: %s", e)
        return False


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _content_hash(text: str) -> str:
    """SHA-256 hash of text content for dedup."""
    return hashlib.sha256(text.encode()).hexdigest()


def chunk_text(text: str, max_tokens: int = 512, overlap_tokens: int = 50) -> list[str]:
    """Split text into chunks respecting paragraph and sentence boundaries.

    Token approximation: 1 token ~ 0.75 words.
    """
    if not text or not text.strip():
        return []

    max_words = int(max_tokens * 0.75)
    overlap_words = int(overlap_tokens * 0.75)

    words = text.split()
    if len(words) <= max_words:
        return [text.strip()]

    # Split on paragraph boundaries first
    paragraphs = re.split(r"\n\s*\n", text)

    chunks = []
    current_words = []

    for para in paragraphs:
        para_words = para.split()
        if not para_words:
            continue

        # If adding this paragraph exceeds limit, finalize current chunk
        if current_words and len(current_words) + len(para_words) > max_words:
            chunks.append(" ".join(current_words))
            # Overlap: keep last N words
            current_words = current_words[-overlap_words:] if overlap_words else []

        # If a single paragraph exceeds limit, split by sentences then words
        if len(para_words) > max_words:
            sentences = re.split(r"(?<=[.!?])\s+", para)
            for sentence in sentences:
                sent_words = sentence.split()
                # If a single sentence exceeds limit, split by words
                while len(sent_words) > max_words:
                    space = max_words - len(current_words) if current_words else max_words
                    if space <= 0:
                        chunks.append(" ".join(current_words))
                        current_words = current_words[-overlap_words:] if overlap_words else []
                        space = max_words
                    current_words.extend(sent_words[:space])
                    sent_words = sent_words[space:]
                if current_words and len(current_words) + len(sent_words) > max_words:
                    chunks.append(" ".join(current_words))
                    current_words = current_words[-overlap_words:] if overlap_words else []
                current_words.extend(sent_words)
        else:
            current_words.extend(para_words)

    if current_words:
        chunks.append(" ".join(current_words))

    return [c.strip() for c in chunks if c.strip()]


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def _insert_chunks(
    conn: sqlite3.Connection,
    user_id: str,
    source_type: str,
    source_id: str,
    chunks: list[str],
    metadata: dict | None = None,
    topic: str | None = None,
    entities: list[str] | None = None,
) -> int:
    """Insert chunks with embeddings. Returns number of chunks inserted."""
    if not chunks:
        return 0

    metadata_json = json.dumps(metadata) if metadata else None
    entities_json = json.dumps(entities) if entities else None
    has_vec = ensure_vec_table(conn)

    # Batch embed all chunks
    embeddings = None
    if has_vec:
        embeddings = embed_batch(chunks)

    inserted = 0
    for i, chunk in enumerate(chunks):
        ch = _content_hash(chunk)
        try:
            cursor = conn.execute(
                "INSERT INTO memory_chunks "
                "(user_id, source_type, source_id, chunk_index, content, content_hash, metadata_json, topic, entities) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, content_hash) DO NOTHING",
                (user_id, source_type, source_id, i, chunk, ch, metadata_json, topic, entities_json),
            )
            if cursor.rowcount > 0:
                inserted += 1
                # Insert vector embedding
                if has_vec and embeddings and embeddings[i]:
                    row_id = cursor.lastrowid
                    conn.execute(
                        "INSERT INTO memory_chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                        (row_id, _serialize_embedding(embeddings[i])),
                    )
        except Exception as e:
            logger.debug("Failed to insert chunk %d for %s/%s: %s", i, source_type, source_id, e)

    conn.commit()
    return inserted


def _delete_source_chunks(
    conn: sqlite3.Connection,
    user_id: str,
    source_type: str,
    source_id: str,
) -> int:
    """Delete all chunks for a source. Returns count deleted."""
    # Get chunk IDs first (for vec cleanup)
    rows = conn.execute(
        "SELECT id FROM memory_chunks WHERE user_id = ? AND source_type = ? AND source_id = ?",
        (user_id, source_type, source_id),
    ).fetchall()

    if not rows:
        return 0

    chunk_ids = [r[0] for r in rows]

    # Delete from vec table if available
    if enable_vec_extension(conn):
        try:
            for cid in chunk_ids:
                conn.execute("DELETE FROM memory_chunks_vec WHERE chunk_id = ?", (cid,))
        except Exception:
            pass  # vec table might not exist

    # Delete from main table (triggers handle FTS5)
    conn.execute(
        "DELETE FROM memory_chunks WHERE user_id = ? AND source_type = ? AND source_id = ?",
        (user_id, source_type, source_id),
    )
    conn.commit()
    return len(chunk_ids)


# Default ephemeral source types swept by cleanup_old_chunks. user_memory and
# any future channel-durable type are intentionally excluded — those refresh
# on file edit, not by age.
EPHEMERAL_SOURCE_TYPES: tuple[str, ...] = ("conversation", "memory_file", "channel_memory")


def cleanup_old_chunks(
    conn: sqlite3.Connection,
    user_id: str,
    retention_days: int,
    source_types: tuple[str, ...] | None = None,
) -> int:
    """Delete ephemeral memory_chunks rows older than `retention_days`.

    `retention_days <= 0` is a no-op (matches the existing convention used by
    `cleanup_old_memory_files`). FTS5 rows are cleared via the existing
    AFTER DELETE trigger on `memory_chunks`. Vec rows are cleared manually
    because the vec table has no trigger.
    """
    from datetime import datetime, timedelta, timezone

    if retention_days <= 0:
        return 0
    if source_types is None:
        source_types = EPHEMERAL_SOURCE_TYPES
    if not source_types:
        return 0

    # SQLite's `datetime('now')` (used by the `created_at` column default)
    # produces `'YYYY-MM-DD HH:MM:SS'` — SPACE separator, second precision.
    # Python's `isoformat()` would use a `'T'` separator with microseconds,
    # which lex-compares as GREATER than the space form for any same-date row
    # (`' '` < `'T'`), so up to 24 hours of rows on the cutoff day get
    # incorrectly classified as "old". Use `strftime` to match SQLite exactly.
    cutoff = (
        (datetime.now(timezone.utc) - timedelta(days=retention_days))
        .replace(tzinfo=None)
        .strftime("%Y-%m-%d %H:%M:%S")
    )

    placeholders = ",".join("?" * len(source_types))
    rows = conn.execute(
        f"SELECT id FROM memory_chunks "
        f"WHERE user_id = ? AND source_type IN ({placeholders}) AND created_at < ?",
        (user_id, *source_types, cutoff),
    ).fetchall()
    chunk_ids = [r[0] for r in rows]
    if not chunk_ids:
        return 0

    # Cascade to vec table (no trigger exists for memory_chunks_vec). Catch
    # per-row so a single failure doesn't silently skip the rest.
    if enable_vec_extension(conn):
        for cid in chunk_ids:
            try:
                conn.execute("DELETE FROM memory_chunks_vec WHERE chunk_id = ?", (cid,))
            except Exception:
                pass  # vec table may not exist on older deployments

    id_placeholders = ",".join("?" * len(chunk_ids))
    conn.execute(
        f"DELETE FROM memory_chunks WHERE id IN ({id_placeholders})",
        chunk_ids,
    )
    conn.commit()
    return len(chunk_ids)


def index_conversation(
    conn: sqlite3.Connection,
    user_id: str,
    task_id: int | str,
    prompt: str,
    result: str,
    metadata: dict | None = None,
    topic: str | None = None,
    entities: list[str] | None = None,
) -> int:
    """Index a conversation (prompt + result) into memory chunks.

    Returns number of chunks inserted.
    """
    source_id = str(task_id)

    # Combine prompt and result into indexable text
    parts = []
    if prompt:
        parts.append(f"User: {prompt}")
    if result:
        parts.append(f"Bot: {result}")
    text = "\n\n".join(parts)

    chunks = chunk_text(text)
    meta = metadata or {}
    meta["task_id"] = source_id
    return _insert_chunks(conn, user_id, "conversation", source_id, chunks, meta,
                          topic=topic, entities=entities)


def index_file(
    conn: sqlite3.Connection,
    user_id: str,
    file_path: str,
    content: str,
    source_type: str = "memory_file",
    topic: str | None = None,
    entities: list[str] | None = None,
) -> int:
    """Index a file's content, replacing any existing chunks for that source.

    Returns number of chunks inserted.
    """
    # Delete existing chunks for this source
    _delete_source_chunks(conn, user_id, source_type, file_path)

    chunks = chunk_text(content)
    meta = {"file_path": file_path}
    return _insert_chunks(conn, user_id, source_type, file_path, chunks, meta,
                          topic=topic, entities=entities)


def reindex_all(
    conn: sqlite3.Connection,
    config,
    user_id: str,
    lookback_days: int = 90,
) -> dict:
    """Reindex completed tasks and memory files for a user.

    Returns stats dict with counts.
    """
    from datetime import datetime, timedelta, timezone

    stats = {"conversations": 0, "memory_files": 0, "chunks": 0}

    # Reindex completed tasks
    since = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        "SELECT id, prompt, result FROM tasks "
        "WHERE user_id = ? AND status = 'completed' AND created_at >= ? "
        "ORDER BY id",
        (user_id, since),
    ).fetchall()

    for row in rows:
        task_id, prompt, result = row[0], row[1], row[2]
        if prompt or result:
            n = index_conversation(conn, user_id, task_id, prompt or "", result or "")
            if n > 0:
                stats["conversations"] += 1
                stats["chunks"] += n

    # Reindex memory files if mount available
    if config.nextcloud_mount_path:
        memories_dir = config.nextcloud_mount_path / f"Users/{user_id}/memories"
        if memories_dir.is_dir():
            for path in sorted(memories_dir.glob("*.md")):
                content = path.read_text()
                if content.strip():
                    n = index_file(conn, user_id, str(path), content, "memory_file")
                    if n > 0:
                        stats["memory_files"] += 1
                        stats["chunks"] += n

        # Index USER.md
        user_md = config.nextcloud_mount_path / f"Users/{user_id}/{config.bot_dir_name}/config/USER.md"
        if user_md.is_file():
            content = user_md.read_text()
            if content.strip():
                n = index_file(conn, user_id, str(user_md), content, "user_memory")
                if n > 0:
                    stats["chunks"] += n

    # Reindex channel memory files
    if config.nextcloud_mount_path:
        channels_dir = config.nextcloud_mount_path / "Channels"
        if channels_dir.is_dir():
            stats["channel_memories"] = 0
            for token_dir in sorted(channels_dir.iterdir()):
                if not token_dir.is_dir():
                    continue
                token = token_dir.name
                channel_user_id = f"channel:{token}"
                memories_dir = token_dir / "memories"
                if memories_dir.is_dir():
                    for path in sorted(memories_dir.glob("*.md")):
                        content = path.read_text()
                        if content.strip():
                            n = index_file(
                                conn,
                                channel_user_id,
                                str(path),
                                content,
                                "channel_memory",
                            )
                            if n > 0:
                                stats["channel_memories"] += 1
                                stats["chunks"] += n

    return stats


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _escape_fts5_query(query: str) -> str:
    """Escape a user query for safe FTS5 MATCH usage.

    Quotes each term to neutralize FTS5 operators (AND, OR, NOT, NEAR, etc.).
    """
    # Split into words and quote each one
    terms = query.split()
    if not terms:
        return '""'
    return " ".join(f'"{t}"' for t in terms)


def _build_user_filter(user_id: str, include_user_ids: list[str] | None = None) -> tuple[str, list[str]]:
    """Build SQL user_id filter clause and params.

    Returns (sql_fragment, params) where sql_fragment is like 'mc.user_id IN (?, ?)'.
    """
    all_ids = [user_id]
    if include_user_ids:
        for uid in include_user_ids:
            if uid not in all_ids:
                all_ids.append(uid)

    if len(all_ids) == 1:
        return "mc.user_id = ?", all_ids
    else:
        placeholders = ",".join("?" for _ in all_ids)
        return f"mc.user_id IN ({placeholders})", all_ids


def _search_bm25(
    conn: sqlite3.Connection,
    user_id: str,
    query: str,
    limit: int,
    source_types: list[str] | None = None,
    include_user_ids: list[str] | None = None,
    since: str | None = None,
    topics: list[str] | None = None,
    entities: list[str] | None = None,
) -> list[SearchResult]:
    """Full-text BM25 search via FTS5."""
    escaped = _escape_fts5_query(query)

    user_filter, user_params = _build_user_filter(user_id, include_user_ids)

    sql = (
        "SELECT mc.id, mc.content, mc.source_type, mc.source_id, mc.metadata_json, "
        "rank AS score "
        "FROM memory_chunks_fts fts "
        "JOIN memory_chunks mc ON mc.id = fts.rowid "
        f"WHERE fts.content MATCH ? AND {user_filter}"
    )
    params: list = [escaped, *user_params]

    if source_types:
        placeholders = ",".join("?" for _ in source_types)
        sql += f" AND mc.source_type IN ({placeholders})"
        params.extend(source_types)

    if since:
        sql += " AND mc.created_at >= ?"
        params.append(since)

    if topics:
        placeholders = ",".join("?" for _ in topics)
        sql += f" AND (mc.topic IS NULL OR mc.topic IN ({placeholders}))"
        params.extend(topics)

    if entities:
        entity_clauses = " OR ".join(
            "EXISTS (SELECT 1 FROM json_each(mc.entities) WHERE json_each.value = ?)"
            for _ in entities
        )
        sql += f" AND ({entity_clauses})"
        params.extend(e.lower() for e in entities)

    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    results = []
    try:
        for row in conn.execute(sql, params):
            meta = json.loads(row[4]) if row[4] else {}
            results.append(SearchResult(
                chunk_id=row[0],
                content=row[1],
                score=row[5],
                source_type=row[2],
                source_id=row[3],
                metadata=meta,
            ))
    except Exception as e:
        logger.debug("BM25 search failed: %s", e)

    return results


_VEC_MAX_K = 1000


def _search_vec(
    conn: sqlite3.Connection,
    user_id: str,
    query: str,
    limit: int,
    source_types: list[str] | None = None,
    include_user_ids: list[str] | None = None,
    since: str | None = None,
    topics: list[str] | None = None,
    entities: list[str] | None = None,
) -> list[SearchResult]:
    """Vector similarity search via sqlite-vec with adaptive k.

    KNN returns the k nearest candidates, then SQL post-filters by
    user_id/source_type/since/topic/entity. When filters eliminate most
    candidates, a fixed k can yield fewer than `limit` results (or zero).
    This runs the query with a growing k until enough rows survive the
    post-filter, the candidate pool is exhausted, or `k` hits `_VEC_MAX_K`.
    """
    if not enable_vec_extension(conn):
        return []

    embedding = embed_text(query)
    if embedding is None:
        return []

    serialized = _serialize_embedding(embedding)

    user_filter, user_params = _build_user_filter(user_id, include_user_ids)

    has_post_filter = bool(source_types or since or topics or entities)
    # Post-filter narrows the pool — start wider so the first pass has room.
    base_multiplier = 10 if has_post_filter else 5
    k = max(limit * base_multiplier, 10)

    base_sql = (
        "SELECT v.chunk_id, v.distance, mc.content, mc.source_type, mc.source_id, mc.metadata_json "
        "FROM memory_chunks_vec v "
        "JOIN memory_chunks mc ON mc.id = v.chunk_id "
        f"WHERE v.embedding MATCH ? AND k = ? "
        f"AND {user_filter}"
    )
    filter_sql = ""
    filter_params: list = []

    if source_types:
        placeholders = ",".join("?" for _ in source_types)
        filter_sql += f" AND mc.source_type IN ({placeholders})"
        filter_params.extend(source_types)

    if since:
        filter_sql += " AND mc.created_at >= ?"
        filter_params.append(since)

    if topics:
        placeholders = ",".join("?" for _ in topics)
        filter_sql += f" AND (mc.topic IS NULL OR mc.topic IN ({placeholders}))"
        filter_params.extend(topics)

    if entities:
        entity_clauses = " OR ".join(
            "EXISTS (SELECT 1 FROM json_each(mc.entities) WHERE json_each.value = ?)"
            for _ in entities
        )
        filter_sql += f" AND ({entity_clauses})"
        filter_params.extend(e.lower() for e in entities)

    sql = base_sql + filter_sql

    results: list[SearchResult] = []
    seen_chunk_ids: set[int] = set()

    while True:
        effective_k = min(k, _VEC_MAX_K)
        params: list = [serialized, effective_k, *user_params, *filter_params]

        new_rows_seen = 0
        try:
            for row in conn.execute(sql, params):
                chunk_id = row[0]
                if chunk_id in seen_chunk_ids:
                    continue
                seen_chunk_ids.add(chunk_id)
                new_rows_seen += 1
                meta = json.loads(row[5]) if row[5] else {}
                results.append(SearchResult(
                    chunk_id=chunk_id,
                    content=row[2],
                    score=1.0 - row[1],  # Convert distance to similarity
                    source_type=row[3],
                    source_id=row[4],
                    metadata=meta,
                ))
        except Exception as e:
            logger.debug("Vector search failed: %s", e)
            break

        if len(results) >= limit:
            break
        if effective_k >= _VEC_MAX_K:
            break
        if new_rows_seen == 0:
            # Enlarging k produced no new candidates — pool exhausted.
            break

        k *= 2

    return results[:limit]


def _rrf_fusion(
    bm25_results: list[SearchResult],
    vec_results: list[SearchResult],
    k: int = 60,
) -> list[SearchResult]:
    """Reciprocal Rank Fusion to combine BM25 and vector results."""
    scores: dict[int, float] = {}
    results_by_id: dict[int, SearchResult] = {}
    bm25_ranks: dict[int, int] = {}
    vec_ranks: dict[int, int] = {}

    for rank, r in enumerate(bm25_results, 1):
        scores[r.chunk_id] = scores.get(r.chunk_id, 0) + 1.0 / (k + rank)
        results_by_id[r.chunk_id] = r
        bm25_ranks[r.chunk_id] = rank

    for rank, r in enumerate(vec_results, 1):
        scores[r.chunk_id] = scores.get(r.chunk_id, 0) + 1.0 / (k + rank)
        if r.chunk_id not in results_by_id:
            results_by_id[r.chunk_id] = r
        vec_ranks[r.chunk_id] = rank

    # Sort by fused score descending
    sorted_ids = sorted(scores, key=lambda cid: scores[cid], reverse=True)

    fused = []
    for cid in sorted_ids:
        r = results_by_id[cid]
        r.score = scores[cid]
        r.bm25_rank = bm25_ranks.get(cid)
        r.vec_rank = vec_ranks.get(cid)
        fused.append(r)

    return fused


def search(
    conn: sqlite3.Connection,
    user_id: str,
    query: str,
    limit: int = 10,
    source_types: list[str] | None = None,
    rrf_k: int = 60,
    include_user_ids: list[str] | None = None,
    since: str | None = None,
    topics: list[str] | None = None,
    entities: list[str] | None = None,
) -> list[SearchResult]:
    """Hybrid search: BM25 + vector with RRF fusion.

    Falls back to BM25-only if vector search is unavailable.

    Args:
        include_user_ids: Additional user_ids to include in search (e.g., channel IDs).
            The primary user_id is always included.
        since: ISO date string (e.g., "2026-03-25"). Only return chunks created on or after this date.
        topics: Filter to chunks with these topics (NULL-topic chunks always included).
        entities: Filter to chunks mentioning these entities (JSON array containment).
    """
    # Fetch more from each source for fusion
    fetch_limit = limit * 3

    bm25_results = _search_bm25(conn, user_id, query, fetch_limit, source_types,
                                 include_user_ids, since=since, topics=topics, entities=entities)
    vec_results = _search_vec(conn, user_id, query, fetch_limit, source_types,
                               include_user_ids, since=since, topics=topics, entities=entities)

    if vec_results:
        fused = _rrf_fusion(bm25_results, vec_results, k=rrf_k)
        return fused[:limit]
    else:
        # BM25-only fallback
        for rank, r in enumerate(bm25_results, 1):
            r.bm25_rank = rank
        return bm25_results[:limit]


def get_stats(
    conn: sqlite3.Connection,
    user_id: str,
    include_user_ids: list[str] | None = None,
) -> dict:
    """Get chunk counts by source_type and vec count for a user."""
    user_filter, user_params = _build_user_filter(user_id, include_user_ids)

    rows = conn.execute(
        f"SELECT mc.source_type, COUNT(*) FROM memory_chunks mc WHERE {user_filter} GROUP BY mc.source_type",
        user_params,
    ).fetchall()

    by_type = {row[0]: row[1] for row in rows}
    total = sum(by_type.values())

    vec_count = 0
    if enable_vec_extension(conn):
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_chunks_vec v "
                f"JOIN memory_chunks mc ON mc.id = v.chunk_id "
                f"WHERE {user_filter}",
                user_params,
            ).fetchone()
            vec_count = row[0] if row else 0
        except Exception:
            pass

    return {
        "user_id": user_id,
        "total_chunks": total,
        "by_source_type": by_type,
        "vec_chunks": vec_count,
        "vec_available": _vec_available is True,
    }
