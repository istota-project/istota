"""Semantic memory search — hybrid BM25 + vector search over conversations and memory files.

Gracefully degrades to BM25-only if sqlite-vec or sentence-transformers is unavailable.
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import struct
import threading
from dataclasses import dataclass, field
from datetime import date, datetime

logger = logging.getLogger("istota.memory_search")

# Lazy-loaded embedding model singleton, and the lock that keeps concurrent
# worker threads from each building their own copy of it (ISSUE-273).
_model = None
_model_lock = threading.Lock()
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
    created_at: str = ""


# ---------------------------------------------------------------------------
# Embedding helpers (lazy-loaded)
# ---------------------------------------------------------------------------

def _get_model():
    """Load sentence-transformers model on first call. Returns None if unavailable.

    Serialized, because `WorkerPool` workers are threads in the daemon and
    several tasks routinely finish memory-indexing in the same second. Without
    the lock each of them saw `_model is None` and built its own
    `SentenceTransformer`; the last assignment won and the rest stayed resident
    with nothing referencing them. Measured at 80 MB for three concurrent loads
    against 34 MB for one, 46 MB of which survived both gc and `malloc_trim`
    (ISSUE-273).

    The unlocked read in front of the lock is the fast path — every call after
    the first takes it, and the assignment below happens only once the model is
    fully constructed. A load that fails still returns None *without* caching
    the failure, so a transient error doesn't disable vector search until the
    next restart.

    Serializing does mean the cold load blocks the other callers, and on a host
    with no cached copy of the weights that load includes a download. That is
    the intended trade: concurrent loads are the defect, not a fast path worth
    keeping, and the alternative — build outside the lock and compare-and-set —
    reintroduces exactly the duplicate resident copies this exists to remove.
    The log line below is what makes the resulting stall legible, since a
    blocked worker thread otherwise looks like a hung task.
    """
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        logger.info("Loading embedding model (first use; other callers wait here)")
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
    topic_per_chunk: list[str | None] | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    valid_until_per_chunk: list[str | None] | None = None,
) -> int:
    """Insert chunks with embeddings. Returns number of chunks inserted.

    `topic_per_chunk`, when provided, overrides `topic` per-chunk. The list
    must have the same length as `chunks`. Entries may be None for chunks
    whose topic could not be determined; NULL-topic chunks are still
    returned in topic-filtered searches by design.

    `valid_from`/`valid_until` set an episode window on every chunk (ISSUE-109
    #2); a chunk whose `valid_until` has passed is suppressed from recall.
    `valid_until_per_chunk`, when provided, overrides the scalar `valid_until`
    per-chunk (same length contract as `topic_per_chunk`); `valid_from` stays
    scalar.
    """
    if not chunks:
        return 0
    if topic_per_chunk is not None and len(topic_per_chunk) != len(chunks):
        raise ValueError(
            f"topic_per_chunk length {len(topic_per_chunk)} != chunks length {len(chunks)}"
        )
    if valid_until_per_chunk is not None and len(valid_until_per_chunk) != len(chunks):
        raise ValueError(
            f"valid_until_per_chunk length {len(valid_until_per_chunk)} != chunks length {len(chunks)}"
        )

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
        chunk_topic = topic_per_chunk[i] if topic_per_chunk is not None else topic
        chunk_valid_until = (
            valid_until_per_chunk[i] if valid_until_per_chunk is not None else valid_until
        )
        try:
            cursor = conn.execute(
                "INSERT INTO memory_chunks "
                "(user_id, source_type, source_id, chunk_index, content, content_hash, "
                "metadata_json, topic, entities, valid_from, valid_until) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, content_hash) DO NOTHING",
                (user_id, source_type, source_id, i, chunk, ch, metadata_json,
                 chunk_topic, entities_json, valid_from, chunk_valid_until),
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


# Default ephemeral source types swept by cleanup_old_chunks. user_memory,
# skill_overlay and any future channel-durable type are intentionally excluded
# — those refresh on file edit, not by age. Ageing a skill overlay out would
# be worse than ageing out USER.md: an overlay only reaches a prompt on a task
# that selected its skill, so search is the one surface that can find a rule
# for the skills a user rarely uses, which is exactly where a stale index bites.
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
    valid_until: str | None = None,
    speaker: str = "User",
) -> int:
    """Index a conversation (prompt + result) into memory chunks.

    Returns number of chunks inserted.

    `valid_until` sets an episode window on the conversation's chunks
    (ISSUE-109 #2) — pass it when the exchange is about a time-boxed episode.

    `speaker` labels the prompt half. It defaults to the user because that is
    what a prompt normally is, but an email turn may have been written by an
    external contact — an indexed chunk is recalled straight back into a later
    prompt, so a wrong label here is the ISSUE-226 defect with a longer half-life.
    """
    source_id = str(task_id)

    # Combine prompt and result into indexable text
    parts = []
    if prompt:
        parts.append(f"{speaker}: {prompt}")
    if result:
        parts.append(f"Bot: {result}")
    text = "\n\n".join(parts)

    chunks = chunk_text(text)
    meta = metadata or {}
    meta["task_id"] = source_id
    return _insert_chunks(conn, user_id, "conversation", source_id, chunks, meta,
                          topic=topic, entities=entities, valid_until=valid_until)


def index_file(
    conn: sqlite3.Connection,
    user_id: str,
    file_path: str,
    content: str,
    source_type: str = "memory_file",
    topic: str | None = None,
    entities: list[str] | None = None,
    topic_per_chunk: list[str | None] | None = None,
    valid_until_per_chunk: list[str | None] | None = None,
) -> int:
    """Index a file's content, replacing any existing chunks for that source.

    Returns number of chunks inserted.

    `topic_per_chunk` (when provided) overrides `topic` per-chunk. Length
    must match the chunk count produced by `chunk_text(content)` — callers
    that build per-chunk topics should chunk first, then pass the resulting
    aligned list. Used by the sleep cycle to attach per-task topics derived
    from `ref:N` markers to their containing chunks.

    `valid_until_per_chunk` (same length contract) sets a per-chunk episode
    window (ISSUE-109 #2) so a chunk whose episode has closed self-suppresses
    from recall. Used by the sleep cycle to propagate episodic facts' close
    dates to the bullets they were extracted from.
    """
    # Delete existing chunks for this source
    _delete_source_chunks(conn, user_id, source_type, file_path)

    chunks = chunk_text(content)
    meta = {"file_path": file_path}
    return _insert_chunks(
        conn, user_id, source_type, file_path, chunks, meta,
        topic=topic, entities=entities, topic_per_chunk=topic_per_chunk,
        valid_until_per_chunk=valid_until_per_chunk,
    )


def reindex_skill_overlays(
    conn: sqlite3.Connection, config, user_id: str
) -> tuple[int, int]:
    """Reindex a user's per-skill overlays. Returns `(files, chunks)`.

    A rule that moved out of USER.md and into `config/skills/<name>.md` is in
    the prompt only on a task that selected that skill, so without these rows
    "why does the bot always do X" returns nothing and the rule is effectively
    lost. Durable, like `user_memory` — deliberately outside
    EPHEMERAL_SOURCE_TYPES, since it refreshes on edit rather than aging out.

    Three properties this has to keep, each of which was a defect found in
    review of the first version.

    **Containment.** The directory sits under `{mount}/Users/{user_id}`, which
    `build_bwrap_cmd` binds read-write into that user's own sandbox, so
    `config` and `skills` are both entries a task can replace with a symlink.
    `read_overlay_bytes`' `O_NOFOLLOW` covers only the overlay file, and the
    files at the far end of a redirected directory are ordinary regular files
    that pass it — measured indexing a file from outside the mount, which
    `!search` then reads straight back. `contained_overlay_dir` is the same
    rule the loader and the memory CLI apply, and the resolved path is what is
    walked.

    **Only what binds is indexed — with one deliberate exception.** A file
    named for a skill that does not exist, one for a denylisted skill, or one
    past the loading cap reaches no prompt at all, so indexing it would have
    search return a rule that is not in any prompt — the failure the
    delete-on-empty path exists to prevent, arrived at from the other side.
    `doctor`'s `config.skill_overlays` is what reports such a file; search
    declines to pretend it is live.

    The exception is `effective_disabled_skills`, which is **not** passed to
    `inspect_overlay` here, so an overlay for a skill the operator or the user
    switched off stays indexed while `skills overlays` reports it
    `binds: false, reason: skill_disabled`. That is ISSUE-341 item 1 and it is
    left standing on purpose: a disabled skill is a reversible state, and a
    user who asks "why does the bot always do X" about a rule they wrote should
    find it rather than be told nothing exists. The three gates above are
    permanent properties of the file; this one is a setting.

    **This is the only automatic indexing an overlay gets** (ISSUE-343). The
    memory CLI used to re-index on each of its own writes, and that seam went
    with the write verbs — it never covered the authoring mode the file is
    actually for, since a user editing `config/skills/<name>.md` in a text
    editor called no CLI. A full directory pass covers both by construction,
    which is why the two callers are `reindex_all` and the nightly sleep cycle
    rather than anything on a write path.

    **Rows for a vanished file go.** Unlike USER.md, an overlay is a file the
    workflow deletes — the memory CLI removes one whose last bullet goes, and a
    user can delete or rename one over Nextcloud, where nothing calls the CLI.
    `skill_overlay` is outside EPHEMERAL_SOURCE_TYPES, so `cleanup_old_chunks`
    will never reclaim those rows and they would stay searchable forever.

    The import is function-local because `istota.skills` star-imports every
    skill on the way to `_loader`, and this module is on the executor's recall
    path.
    """
    from istota.skills._loader import (
        OVERLAY_MAX_BYTES,
        OVERLAY_UNREADABLE,
        contained_overlay_dir,
        inspect_overlay,
        load_skill_index,
        open_overlay_dir,
        read_overlay_bytes,
    )

    # `use_mount` before the join, not after: `nextcloud_mount_path` is None on
    # an rclone-remote deployment and `None / "Users/…"` is a TypeError. This
    # was survivable while the only caller was a hand-run `memory_search
    # reindex`; it is now on a scheduler cadence, where the raise would be
    # swallowed by the caller's own `except Exception` and reported nowhere.
    if not getattr(config, "use_mount", False):
        return 0, 0
    bot_dir = getattr(config, "bot_dir_name", "")
    if not bot_dir:
        return 0, 0
    user_root = config.nextcloud_mount_path / f"Users/{user_id}"
    overlay_dir = contained_overlay_dir(
        user_root / bot_dir / "config" / "skills", user_root
    )
    if overlay_dir is None or not overlay_dir.is_dir():
        return 0, 0

    try:
        known = load_skill_index(
            config.skills_dir, bundled_dir=getattr(config, "bundled_skills_dir", None)
        )
    except Exception as e:  # noqa: BLE001 - a reindex is best-effort per source
        logger.warning("skill overlay reindex skipped, no skill index: %s", e)
        return 0, 0

    # The containment answer above is a comparison of resolved paths, and it
    # stops being true the moment anything moves. Everything from here reads
    # through an fd instead, so the directory that passed is the directory that
    # is walked (ISSUE-341 item 3). `overlay_dir` stays in use below and is no
    # longer the security gate: it is the *path spelling* the index keys on.
    # It is the `realpath`. There is no per-write path left to agree with —
    # ISSUE-343 retired the overlay write verbs and this pass is now the only
    # writer of `skill_overlay` rows — but the spelling still matters across
    # runs of this function, since a row keyed on the raw configured path
    # would be reaped as stale by the next pass on a symlinked mount.
    dir_fd = open_overlay_dir(user_root, bot_dir, "config", "skills")
    if dir_fd is None:
        # `contained_overlay_dir` just passed and this did not, so the two
        # disagree — which they can, deliberately: it resolves symlinks and
        # accepts one landing inside the user root, while the fd walk refuses a
        # symlink at any component. The prompt loader takes the permissive
        # answer, so on that layout an overlay reaches every prompt for its
        # skill and is invisible to `!search` for good, and the reap below is
        # skipped so old rows persist too. Say so once rather than going quiet.
        logger.warning(
            "skill overlay reindex skipped for %s: the overlay directory passed "
            "containment but could not be opened without following a symlink",
            user_id,
        )
        return 0, 0

    files = chunks = 0
    live: set[str] = set()
    try:
        try:
            # No dotfile filter: `Path.glob("*.md")`, which this replaced and
            # which `doctor` and `skills overlays` still use, matches them, and
            # three listings of one directory disagreeing is the drift this
            # module keeps being bitten by. A dotfile cannot bind anyway
            # (`.developer.md` is the skill `.developer`), so it changes
            # nothing beyond keeping the three the same.
            with os.scandir(dir_fd) as entries:
                names = sorted(e.name for e in entries if e.name.endswith(".md"))
        except OSError:
            return 0, 0
        for name in names:
            path = overlay_dir / name
            found = inspect_overlay(
                path, known_skills=known, max_read_bytes=OVERLAY_MAX_BYTES,
                dir_fd=dir_fd,
            )
            if found.reason == OVERLAY_UNREADABLE:
                # "This pass could not read it", which is not "it is gone".
                # An EACCES or an EIO here would otherwise drop the file out
                # of `live` and have the reap below delete rules the user can
                # still see on disk, permanently — the file is not rewritten,
                # so nothing indexes it again. Held rather than reindexed:
                # there are no bytes to index either.
                live.add(str(path))
                continue
            if not found.binds:
                continue
            raw, refusal, _size = read_overlay_bytes(
                path, max_bytes=OVERLAY_MAX_BYTES, dir_fd=dir_fd
            )
            if refusal is not None or not raw:
                continue
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            live.add(str(path))
            n = index_file(conn, user_id, str(path), content, "skill_overlay")
            if n > 0:
                files += 1
                chunks += n
    finally:
        os.close(dir_fd)

    for stale in _stale_overlay_sources(conn, user_id, live):
        _delete_source_chunks(conn, user_id, "skill_overlay", stale)
    return files, chunks


def _stale_overlay_sources(
    conn: sqlite3.Connection, user_id: str, live: set[str]
) -> list[str]:
    """Indexed `skill_overlay` sources for `user_id` that no longer bind.

    Scoped by ``source_type`` and ``user_id`` and compared against the set just
    walked, so a row is dropped only when this pass positively established that
    its file is gone, or is there and reaches no prompt. A pass that walked
    nothing — no mount, no directory, an unreadable skill index — returns
    before reaching here rather than arriving with an empty ``live`` set, which
    would read as "every overlay was deleted".

    "Positively established" is doing real work and the caller keeps its half:
    a file whose *read* failed this pass is added to ``live`` anyway, because
    an ``EACCES`` or an ``EIO`` is a fact about the pass and not about the
    file, and reaping on one deletes rules the user can still see on disk with
    nothing left to index them again.
    """
    rows = conn.execute(
        "SELECT DISTINCT source_id FROM memory_chunks "
        "WHERE user_id = ? AND source_type = 'skill_overlay'",
        (user_id,),
    ).fetchall()
    return [r[0] for r in rows if r[0] not in live]


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

        n_files, n_chunks = reindex_skill_overlays(conn, config, user_id)
        stats["skill_overlays"] = n_files
        stats["chunks"] += n_chunks

    # Reindex channel memory files (dated + durable CHANNEL.md)
    if config.nextcloud_mount_path:
        channels_dir = config.nextcloud_mount_path / "Channels"
        if channels_dir.is_dir():
            stats["channel_memories"] = 0
            stats["channel_durable"] = 0
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
                # Durable CHANNEL.md — durable like USER.md, refreshed on edit.
                channel_md = token_dir / "CHANNEL.md"
                if channel_md.is_file():
                    content = channel_md.read_text()
                    if content.strip():
                        n = index_file(
                            conn,
                            channel_user_id,
                            str(channel_md),
                            content,
                            "channel_memory_durable",
                        )
                        if n > 0:
                            stats["channel_durable"] += 1
                            stats["chunks"] += n

    return stats


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _escape_fts5_query(query: str, *, prefix: bool = False, match_mode: str = "and") -> str:
    """Escape a user query for safe FTS5 MATCH usage.

    Quotes each term to neutralize FTS5 operators (AND, OR, NOT, NEAR, etc.).

    ``prefix`` appends ``*`` to each quoted term (``"falcon"*``) so a term
    matches longer words (``falcon`` → ``falcons``). ``match_mode`` joins the
    terms as an implicit AND (default — every term must match) or an explicit
    ``OR`` (any term matches, the interactive-search forgiveness path).
    """
    # Split into words and quote each one
    terms = query.split()
    if not terms:
        return '""'
    suffix = "*" if prefix else ""
    quoted = [f'"{t}"{suffix}' for t in terms]
    joiner = " OR " if match_mode == "or" else " "
    return joiner.join(quoted)


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
    now: str | None = None,
    include_expired: bool = False,
    match_mode: str = "and",
    prefix: bool = False,
) -> list[SearchResult]:
    """Full-text BM25 search via FTS5."""
    escaped = _escape_fts5_query(query, prefix=prefix, match_mode=match_mode)

    user_filter, user_params = _build_user_filter(user_id, include_user_ids)

    sql = (
        "SELECT mc.id, mc.content, mc.source_type, mc.source_id, mc.metadata_json, "
        "rank AS score, mc.created_at "
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

    if not include_expired:
        sql += " AND (mc.valid_until IS NULL OR mc.valid_until > ?)"
        params.append(now or date.today().isoformat())

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
                created_at=row[6] or "",
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
    now: str | None = None,
    include_expired: bool = False,
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

    has_post_filter = bool(source_types or since or topics or entities or not include_expired)
    # Post-filter narrows the pool — start wider so the first pass has room.
    base_multiplier = 10 if has_post_filter else 5
    k = max(limit * base_multiplier, 10)

    base_sql = (
        "SELECT v.chunk_id, v.distance, mc.content, mc.source_type, mc.source_id, "
        "mc.metadata_json, mc.created_at "
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

    if not include_expired:
        filter_sql += " AND (mc.valid_until IS NULL OR mc.valid_until > ?)"
        filter_params.append(now or date.today().isoformat())

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
                    created_at=row[6] or "",
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


def _parse_day(value: str | None) -> date | None:
    """Parse the date prefix of a timestamp string (``YYYY-MM-DD...``).

    Tolerates the space- and ``T``-separated forms and bare dates; returns
    None for empty/malformed input so callers can no-op gracefully.
    """
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _apply_recency_decay(
    results: list[SearchResult],
    half_life_days: float,
    now: str | None = None,
) -> list[SearchResult]:
    """Down-weight results by chunk age and re-sort (ISSUE-109 #1).

    Each score is multiplied by ``0.5 ** (age_days / half_life_days)`` so a
    chunk one half-life old counts for half its raw relevance. This counters
    the "frequency != relevance" gravity well where a dense old cluster
    outranks current context on sheer mass. ``half_life_days <= 0`` is a no-op.
    Chunks with a missing/malformed or future timestamp get no penalty.
    """
    if half_life_days <= 0:
        return results
    now_date = _parse_day(now) or date.today()
    for r in results:
        chunk_date = _parse_day(r.created_at)
        if chunk_date is None:
            continue
        age_days = (now_date - chunk_date).days
        if age_days <= 0:
            continue
        r.score *= 0.5 ** (age_days / half_life_days)
    results.sort(key=lambda r: r.score, reverse=True)
    return results


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
    exclude_conversation_task_ids: set[int] | None = None,
    recency_half_life_days: float = 0.0,
    now: str | None = None,
    include_expired: bool = False,
    match_mode: str = "and",
    allow_or_fallback: bool = False,
    prefix: bool = False,
) -> list[SearchResult]:
    """Hybrid search: BM25 + vector with RRF fusion.

    Falls back to BM25-only if vector search is unavailable.

    Args:
        include_user_ids: Additional user_ids to include in search (e.g., channel IDs).
            The primary user_id is always included.
        since: ISO date string (e.g., "2026-03-25"). Only return chunks created on or after this date.
        topics: Filter to chunks with these topics (NULL-topic chunks always included).
        entities: Filter to chunks mentioning these entities (JSON array containment).
        exclude_conversation_task_ids: Task IDs already injected as conversation
            history. Chunks with source_type="conversation" whose source_id matches
            one of these are dropped from the results so recall doesn't duplicate
            the context selection. source_id is stored as a string; ints are
            cast to str for comparison.
        recency_half_life_days: When > 0, multiply fused scores by a time-decay
            factor (ISSUE-109 #1) so old dense clusters can't dominate on mass.
            0 (default) = no decay; callers opt in (the recall path passes the
            configured value).
        now: Reference date (``YYYY-MM-DD``) for episode-window suppression and
            recency decay; defaults to today. Injectable for tests.
        include_expired: When True, skip the episode-window filter and return
            chunks whose ``valid_until`` has passed (ISSUE-109 #2). Default False.
        match_mode: FTS join for the BM25 pass — "and" (implicit AND of every
            term, the precise default) or "or" (any term). Vector search is
            unaffected (it's semantic, not lexical).
        prefix: When True, BM25 terms become prefix queries (``"falcon"*``) so a
            near-miss token still matches. Interactive search opts in.
        allow_or_fallback: When True and the strict (AND) BM25 pass returns no
            rows, retry the BM25 pass once in OR mode before fusing. The recall
            path leaves this False — a whole-prompt OR query floods recall with
            low-precision noise injected into every task.
    """
    # Fetch more from each source for fusion
    fetch_limit = limit * 3

    bm25_results = _search_bm25(conn, user_id, query, fetch_limit, source_types,
                                 include_user_ids, since=since, topics=topics, entities=entities,
                                 now=now, include_expired=include_expired,
                                 match_mode=match_mode, prefix=prefix)
    if not bm25_results and allow_or_fallback and match_mode != "or":
        # Strict AND found nothing — relax to OR so a partial-term match still
        # returns something (interactive-search forgiveness).
        bm25_results = _search_bm25(conn, user_id, query, fetch_limit, source_types,
                                     include_user_ids, since=since, topics=topics, entities=entities,
                                     now=now, include_expired=include_expired,
                                     match_mode="or", prefix=prefix)
    vec_results = _search_vec(conn, user_id, query, fetch_limit, source_types,
                               include_user_ids, since=since, topics=topics, entities=entities,
                               now=now, include_expired=include_expired)

    if vec_results:
        fused = _rrf_fusion(bm25_results, vec_results, k=rrf_k)
        results = fused
    else:
        # BM25-only fallback. Convert the raw FTS5 `rank` (negative, lower =
        # better) into a positive rank-based score (higher = better) so `score`
        # semantics match the RRF path — required for recency decay to compose
        # correctly rather than invert the order.
        for rank, r in enumerate(bm25_results, 1):
            r.bm25_rank = rank
            r.score = 1.0 / (rrf_k + rank)
        results = bm25_results

    if recency_half_life_days and recency_half_life_days > 0:
        results = _apply_recency_decay(results, recency_half_life_days, now=now)

    if exclude_conversation_task_ids:
        excluded = {str(tid) for tid in exclude_conversation_task_ids}
        results = [
            r for r in results
            if not (r.source_type == "conversation" and r.source_id in excluded)
        ]

    return results[:limit]


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
