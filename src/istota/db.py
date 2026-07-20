"""Database operations for istota task queue."""

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("istota.db")


@dataclass
class Task:
    id: int
    status: str
    source_type: str
    user_id: str
    prompt: str
    command: str | None = None
    conversation_token: str | None = None
    parent_task_id: int | None = None
    is_group_chat: bool = False
    attachments: list[str] | None = None
    result: str | None = None
    actions_taken: str | None = None
    execution_trace: str | None = None
    error: str | None = None
    confirmation_prompt: str | None = None
    priority: int = 5
    attempt_count: int = 0
    max_attempts: int = 3
    created_at: str | None = None
    scheduled_for: str | None = None
    output_target: str | None = None
    talk_message_id: int | None = None
    talk_response_id: int | None = None
    reply_to_talk_id: int | None = None
    reply_to_content: str | None = None
    heartbeat_silent: bool = False
    skip_log_channel: bool = False
    scheduled_job_id: int | None = None
    # Briefing identity for deferred-prompt briefing tasks (ISSUE-143). When
    # set, the executor builds the full briefing prompt (slow network I/O) at
    # worker-pickup time instead of on the scheduler dispatch thread.
    briefing_name: str | None = None
    queue: str = "foreground"
    confirmed_at: str | None = None
    selected_skills: str | None = None  # JSON array of skill names
    model: str | None = None  # Per-task model override; empty/None = use config default
    effort: str | None = None  # Per-task effort override; empty/None = use config default
    model_used: str | None = None  # Model the brain actually ran (resolved canonical ID), set post-run
    talk_delivery_token: str | None = None  # Real Talk room for this task's notifications; NULL falls back to conversation_token
    # Phase 1.3 (unified credential resolution refactor): skill-task
    # dispatch. Set when the task should run a single CLI skill (e.g.
    # auto-seeded `_module.feeds.run_scheduled`) without going through
    # Claude. ``skill_args`` is a JSON-encoded ``list[str]`` of argv to
    # the skill module. When ``skill`` is non-NULL the scheduler routes
    # the task through ``_execute_skill_task`` instead of the prompt /
    # command paths.
    skill: str | None = None
    skill_args: str | None = None


@dataclass
class UserResource:
    id: int
    user_id: str
    resource_type: str
    resource_path: str
    display_name: str | None
    permissions: str
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProcessedEmail:
    id: int
    email_id: str
    sender_email: str
    subject: str | None
    thread_id: str | None
    message_id: str | None  # RFC 5322 Message-ID for reply threading
    references: str | None  # RFC 5322 References header for thread chain
    user_id: str | None
    task_id: int | None
    processed_at: str
    routing_method: str | None = None  # plus_address, sender_match, thread_match, discarded


@dataclass
class SentEmail:
    """Outbound email tracked for emissary thread matching."""
    id: int
    user_id: str
    task_id: int | None
    message_id: str
    to_addr: str
    subject: str | None
    thread_id: str | None
    in_reply_to: str | None
    references: str | None
    conversation_token: str | None
    sent_at: str
    talk_delivery_token: str | None = None  # Originating task's resolved Talk room
    origin_target: str | None = None  # output_target descriptor of the originating surface


@dataclass
class IstotaFileTask:
    """Task tracked from a user's TASKS.md file."""
    id: int
    user_id: str
    content_hash: str
    original_line: str
    normalized_content: str
    status: str
    task_id: int | None
    result_summary: str | None
    error_message: str | None
    attempt_count: int
    max_attempts: int
    file_path: str
    created_at: str | None
    started_at: str | None
    completed_at: str | None


@dataclass
class ScheduledJob:
    id: int
    user_id: str
    name: str
    cron_expression: str
    prompt: str
    conversation_token: str | None
    output_target: str | None
    enabled: bool
    last_run_at: str | None
    created_at: str | None
    command: str | None = None
    silent_unless_action: bool = False
    skip_log_channel: bool = False
    consecutive_failures: int = 0
    last_error: str | None = None
    last_success_at: str | None = None
    once: bool = False
    model: str | None = None  # Per-job model override; empty/None = use config default
    effort: str | None = None  # Per-job effort override; empty/None = use config default
    # Phase 1.3 — skill-task dispatch (mirrors ``Task.skill`` / ``skill_args``).
    skill: str | None = None
    skill_args: str | None = None


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Run ALTER TABLE migrations before schema to avoid index failures on new columns."""
    # Tasks table migrations
    for col, col_type in [
        ("talk_message_id", "INTEGER"),
        ("talk_response_id", "INTEGER"),
        ("reply_to_talk_id", "INTEGER"),
        ("reply_to_content", "TEXT"),
        ("cancel_requested", "INTEGER DEFAULT 0"),
        ("worker_pid", "INTEGER"),
        ("last_heartbeat", "TEXT"),
        ("heartbeat_silent", "INTEGER DEFAULT 0"),
        ("skip_log_channel", "INTEGER DEFAULT 0"),
        ("scheduled_job_id", "INTEGER"),
        ("briefing_name", "TEXT"),
        ("command", "TEXT"),
        ("queue", "TEXT DEFAULT 'foreground'"),
        ("actions_taken", "TEXT"),
        ("execution_trace", "TEXT"),
        ("selected_skills", "TEXT"),
        ("model", "TEXT"),
        ("effort", "TEXT"),
        # The model the brain actually used (resolved canonical ID), recorded
        # post-run. Distinct from `model` (the per-task override): `model` stays
        # empty for default-model tasks so retries re-resolve the current
        # default, while `model_used` records what ran for display/audit.
        ("model_used", "TEXT"),
        ("talk_delivery_token", "TEXT"),
        # Phase 1.3 — skill-task dispatch
        ("skill", "TEXT"),
        ("skill_args", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # Column already exists or table doesn't exist yet

    # Sent emails: carry the originating task's resolved Talk room so
    # thread-match follow-ups can deliver to the right channel without re-resolving.
    for col, col_type in [
        ("talk_delivery_token", "TEXT"),
        ("origin_target", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE sent_emails ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass

    # Scheduled jobs table migrations
    for col, col_type in [
        ("silent_unless_action", "INTEGER DEFAULT 0"),
        ("command", "TEXT"),
        ("consecutive_failures", "INTEGER DEFAULT 0"),
        ("last_error", "TEXT"),
        ("last_success_at", "TEXT"),
        ("once", "INTEGER DEFAULT 0"),
        ("skip_log_channel", "INTEGER DEFAULT 0"),
        ("model", "TEXT"),
        ("effort", "TEXT"),
        # Phase 1.3 — skill-task dispatch
        ("skill", "TEXT"),
        ("skill_args", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE scheduled_jobs ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass

    # Places table: drop source column (moved to config)
    try:
        conn.execute("ALTER TABLE places DROP COLUMN source")
    except sqlite3.OperationalError:
        pass  # Column already dropped or doesn't exist

    # Processed emails migrations
    for col, col_type in [
        ("routing_method", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE processed_emails ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass

    # Memory chunks metadata columns
    for col, col_type in [
        ("topic", "TEXT"),
        ("entities", "TEXT"),
        # ISSUE-109 #2 — episode window for retrieval-time suppression of
        # closed episodic memories.
        ("valid_from", "TEXT"),
        ("valid_until", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE memory_chunks ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass

    # Monarch synced transactions migrations (for reconciliation tracking)
    for col, col_type in [
        ("tags_json", "TEXT"),
        ("amount", "REAL"),
        ("merchant", "TEXT"),
        ("posted_account", "TEXT"),
        ("txn_date", "TEXT"),
        ("recategorized_at", "TEXT"),
        ("content_hash", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE monarch_synced_transactions ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass

    # User resources: JSON extras for resource-type-specific config
    # (overland ingest_token, money config_path/data_dir, feeds tumblr_api_key, etc.).
    # Lets DB-managed resources carry the same payload that TOML rows do, so
    # ansible can drive resource provisioning through `istota resource ensure`
    # instead of templating per-user TOML.
    try:
        conn.execute("ALTER TABLE user_resources ADD COLUMN extras TEXT")
    except sqlite3.OperationalError:
        pass

    # User profiles: per-user disabled modules list (Phase 1 of the modules /
    # connected services refactor). Default-on, JSON array of disabled module
    # names from istota.modules.MODULE_NAMES.
    try:
        conn.execute(
            "ALTER TABLE user_profiles ADD COLUMN "
            "disabled_modules TEXT NOT NULL DEFAULT '[]'"
        )
    except sqlite3.OperationalError:
        pass

    # User profiles: drop legacy ntfy_topic column. ntfy is now a per-user
    # connected service stored in the encrypted secrets table; the profile
    # column is unused.
    try:
        conn.execute("ALTER TABLE user_profiles DROP COLUMN ntfy_topic")
    except sqlite3.OperationalError:
        pass  # Column already dropped or never existed.

    # User profiles: purpose-keyed delivery routing. `routing` is a JSON object
    # {purpose -> output_target descriptor}; `default_destination` is the
    # fallback descriptor. Defaults reproduce current behaviour (everything →
    # Talk).
    try:
        conn.execute(
            "ALTER TABLE user_profiles ADD COLUMN "
            "routing TEXT NOT NULL DEFAULT '{}'"
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "ALTER TABLE user_profiles ADD COLUMN "
            "default_destination TEXT NOT NULL DEFAULT 'talk'"
        )
    except sqlite3.OperationalError:
        pass
    # Email-reply mirror policy: origin+thread (default) | origin | thread.
    try:
        conn.execute(
            "ALTER TABLE user_profiles ADD COLUMN "
            "email_reply_routing TEXT NOT NULL DEFAULT 'origin+thread'"
        )
    except sqlite3.OperationalError:
        pass

    # Quiet email senders: fnmatch patterns whose mail is filed silently (no
    # task, no session). Mirrors trusted_email_senders. JSON array.
    try:
        conn.execute(
            "ALTER TABLE user_profiles ADD COLUMN "
            "quiet_email_senders TEXT NOT NULL DEFAULT '[]'"
        )
    except sqlite3.OperationalError:
        pass

    # Knowledge facts dedup: invalidate older duplicate current facts so the
    # partial unique index in schema.sql can be created without IntegrityError
    # on legacy DBs written before ISSUE-042's fix landed. Keeps the newest id
    # per (user_id, subject, predicate, object) group as current; older rows
    # get valid_until = today so they stay in the historical record.
    try:
        conn.execute("""
            UPDATE knowledge_facts
            SET valid_until = date('now'), updated_at = datetime('now')
            WHERE valid_until IS NULL
              AND id NOT IN (
                  SELECT MAX(id) FROM knowledge_facts
                  WHERE valid_until IS NULL
                  GROUP BY user_id, subject, predicate, object
              )
        """)
    except sqlite3.OperationalError:
        pass  # Table doesn't exist yet (fresh install before schema.sql runs)

    # Task event stream table (task-event-streaming spec). Created here for
    # existing DBs; schema.sql also has it for fresh installs. The cascade
    # clause is decorative (PRAGMA foreign_keys is unset) — events are
    # hand-deleted in cleanup_old_tasks and delete_task_events.
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_events (
                id          INTEGER PRIMARY KEY,
                task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                seq         INTEGER NOT NULL,
                kind        TEXT NOT NULL,
                payload     TEXT NOT NULL DEFAULT '{}',
                created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE (task_id, seq)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_events_task_seq "
            "ON task_events (task_id, seq)"
        )
    except sqlite3.OperationalError:
        pass

    # Web chat rooms (web chat surface). Created here for existing DBs;
    # schema.sql also has it for fresh installs. Each room's token is the
    # conversation_token used by its tasks, so each room gets its own
    # CHANNEL.md + sleep-cycle handling.
    try:
        # `token` is NOT globally unique: a shared Talk room (one Nextcloud
        # conversation) has one handle row per participant so it can surface in
        # each member's web room list (ISSUE-134). Uniqueness is per (user, token).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS web_chat_rooms (
                id          INTEGER PRIMARY KEY,
                user_id     TEXT NOT NULL,
                token       TEXT NOT NULL,
                name        TEXT NOT NULL,
                archived    INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (user_id, token)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_chat_rooms_user "
            "ON web_chat_rooms (user_id, archived, id)"
        )
        # Unsolicited (bot-delivered) messages posted into a web chat room —
        # alerts, the verbose execution log, and any notification routed to the
        # `web` surface. Distinct from task-backed chat turns (which live in
        # `tasks`): these have no originating user prompt, so they render as a
        # single system message merged into the room transcript by time.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS web_chat_messages (
                id          INTEGER PRIMARY KEY,
                user_id     TEXT NOT NULL,
                token       TEXT NOT NULL,
                role        TEXT NOT NULL DEFAULT 'system',
                title       TEXT,
                text        TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_chat_messages_token "
            "ON web_chat_messages (token, id)"
        )
    except sqlite3.OperationalError:
        pass

    # Unified Talk / web room sync (surface-independent room registry).
    # Created here for existing DBs; schema.sql also has these for fresh
    # installs. The cascade clauses are decorative (PRAGMA foreign_keys unset).
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                token       TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                name        TEXT,
                origin      TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                archived    INTEGER NOT NULL DEFAULT 0,
                model       TEXT,
                effort      TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rooms_user ON rooms (user_id, archived)")
        # Backfill the per-room model/effort columns on an existing rooms table
        # (created by an earlier build without them). Placed here, after the
        # CREATE, so the table exists before the ALTER.
        for _room_col in ("model", "effort"):
            try:
                conn.execute(f"ALTER TABLE rooms ADD COLUMN {_room_col} TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists
        # Per-user room membership (ISSUE-134). A room is shared (one token, one
        # transcript) but each participant has a membership row; web visibility is
        # resolved through this, not the single-owner `rooms.user_id`.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS room_members (
                room_token  TEXT NOT NULL REFERENCES rooms(token) ON DELETE CASCADE,
                user_id     TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (room_token, user_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_room_members_user "
            "ON room_members (user_id)"
        )
        # Per-user "hide this room" tombstone. The web hide-an-imported-room
        # action drops the `room_members` row, but the poll-time Talk-room
        # registration backfill re-adds membership for every participant — so
        # the dropped row alone is no longer a durable hide. This tombstone is:
        # written on hide, consulted by `list_member_rooms` (excluded from the
        # web list even while a member), and cleared on the user's own next
        # inbound (`record_inbound`) — "re-engagement un-hides".
        conn.execute("""
            CREATE TABLE IF NOT EXISTS room_dismissals (
                room_token   TEXT NOT NULL REFERENCES rooms(token) ON DELETE CASCADE,
                user_id      TEXT NOT NULL,
                dismissed_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (room_token, user_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS room_bindings (
                room_token   TEXT NOT NULL REFERENCES rooms(token) ON DELETE CASCADE,
                surface      TEXT NOT NULL,
                surface_ref  TEXT NOT NULL,
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (room_token, surface)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_room_bindings_ref "
            "ON room_bindings (surface, surface_ref)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id            INTEGER PRIMARY KEY,
                room_token    TEXT NOT NULL REFERENCES rooms(token) ON DELETE CASCADE,
                role          TEXT NOT NULL,
                body          TEXT NOT NULL,
                title         TEXT,
                task_id       INTEGER,
                origin_surface TEXT NOT NULL,
                external_ids  TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_room ON messages (room_token, id)")
        # Correct an existing deploy's looser index in place. The original keyed
        # on (room_token, origin_surface, role, task_id); drop it so the tighter
        # (room_token, role, task_id) form below replaces it (CREATE IF NOT
        # EXISTS alone would keep the stale definition).
        # Index by position, not name: _run_migrations also runs under init_db's
        # plain (non-Row-factory) connection, where row["sql"] would raise.
        _old_ext = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_messages_ext'"
        ).fetchone()
        if _old_ext and _old_ext[0] and "origin_surface" in _old_ext[0]:
            conn.execute("DROP INDEX idx_messages_ext")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_ext "
            "ON messages (room_token, role, task_id) "
            "WHERE task_id IS NOT NULL"
        )
        # Read cursors are per (room, surface, user) — an unread badge in one
        # member's web view isn't cleared by another member reading (ISSUE-134).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS room_read_state (
                room_token  TEXT NOT NULL REFERENCES rooms(token) ON DELETE CASCADE,
                surface     TEXT NOT NULL,
                user_id     TEXT NOT NULL DEFAULT '',
                last_read_message_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (room_token, surface, user_id)
            )
        """)
        # Per-user message bookmarks ("stars", web UI). Rooms are shared, so
        # stars are keyed per (message, user) — one member's star never shows
        # for another. The FK cascade is decorative (PRAGMA foreign_keys
        # unset); delete_web_chat_room hand-deletes matching rows.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS message_stars (
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                user_id    TEXT NOT NULL,
                starred_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (message_id, user_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_stars_user "
            "ON message_stars (user_id, message_id)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS _migration_state (
                name        TEXT PRIMARY KEY,
                applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # User-scoped Nextcloud OAuth pair, encrypted with the *web-only* key
        # (ISTOTA_WEB_TOKEN_KEY — not the shared ISTOTA_SECRET_KEY). Written and
        # decrypted only by the web process (istota.web_tokens); the scheduler
        # reads nothing here. expires_at is plaintext ISO UTC so refresh checks
        # don't need a decrypt.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS web_user_tokens (
                user_id       TEXT PRIMARY KEY,
                access_token  TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at    TEXT NOT NULL,
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Mid-flight steering channel (`!steer`). Created here for existing DBs;
        # schema.sql also has it for fresh installs. The cascade clause is
        # decorative (PRAGMA foreign_keys unset).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_steers (
                id           INTEGER PRIMARY KEY,
                task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                seq          INTEGER NOT NULL,
                text         TEXT NOT NULL,
                user_id      TEXT NOT NULL,
                source       TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                consumed_at  TEXT,
                UNIQUE (task_id, seq)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_steers_pending "
            "ON task_steers (task_id, status, seq)"
        )
    except sqlite3.OperationalError:
        pass

    _migrate_unified_rooms(conn)
    _migrate_scheduled_transcript_cleanup(conn)
    _migrate_nonconversational_transcript_cleanup(conn)
    _migrate_web_chat_rooms_peruser(conn)
    _migrate_room_read_state_peruser(conn)
    _migrate_room_members(conn)

    # Encrypt any plaintext Google OAuth tokens at rest. Idempotent --
    # rows already in Fernet form (the new write path) are detected via
    # decrypt-or-fail and skipped. No-op on fresh installs (table not
    # created until schema.sql runs below) and on deployments without
    # $ISTOTA_SECRET_KEY (the read path will fail loudly so operators
    # notice and the user re-auths).
    _migrate_google_oauth_encryption(conn)


def _resolve_schema_path() -> Path:
    """Locate schema.sql for both a source checkout and an installed wheel.

    In a source checkout db.py lives at ``<repo>/src/istota/db.py`` and the
    schema is ``<repo>/schema.sql`` (``parent.parent.parent``). In a non-editable
    install (``uv tool install`` / pip wheel) there is no repo root — the schema
    is force-included into the package as ``istota/schema.sql`` (``parent``).
    Prefer the packaged copy, fall back to the source-tree copy.
    """
    here = Path(__file__).resolve().parent
    candidates = [
        here / "schema.sql",              # packaged wheel (force-include)
        here.parent.parent / "schema.sql",  # source checkout: <repo>/schema.sql
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # Nothing found — return the packaged path so the FileNotFoundError names the
    # location an installed user would expect.
    return candidates[0]


def init_db(db_path: Path) -> None:
    """Initialize database with schema."""
    schema_path = _resolve_schema_path()
    with sqlite3.connect(db_path) as conn:
        # WAL is set ONCE here, not on every get_db open. journal_mode is
        # persistent in the SQLite file header, so re-issuing it per
        # connection only buys a needless write-lock acquisition that races
        # sibling readers (the dispatch-loop stall root cause). istota.db is
        # on local disk, so WAL's mmap'd -shm is safe (unlike the per-user
        # module DBs, which historically lived on the FUSE mount).
        conn.execute("PRAGMA journal_mode=WAL")
        # Migrations read rows by column name (e.g. the unified-rooms backfill),
        # so this connection needs the same Row factory the runtime get_db path
        # uses — a raw connection yields tuples and name-indexing raises
        # TypeError mid-migration (crashed init on upgrade DBs that already held
        # completed tasks). Row supports both name and positional access, so it's
        # a safe superset for every migration step.
        conn.row_factory = sqlite3.Row
        # Run migrations first so new columns exist before schema creates indexes on them
        _run_migrations(conn)
        conn.executescript(schema_path.read_text())


@contextmanager
def get_db(
    db_path: Path, *, busy_timeout_ms: int | None = None
) -> Iterator[sqlite3.Connection]:
    """Get database connection with row factory.

    ``busy_timeout_ms`` overrides the default 30s lock wait — pass a small
    value (e.g. 2000) for the main dispatch loop's read-only scans so a lock
    held past that budget raises ``OperationalError`` (caller skips the tick)
    instead of blocking the loop for 30s and tripping the stall watchdog.
    """
    # timeout=30.0 waits up to 30s for locks instead of failing immediately
    conn = sqlite3.connect(db_path, timeout=30.0)
    # journal_mode is NOT re-issued here — it is persistent in the file header
    # and set once by init_db. Re-issuing WAL per open takes a write lock that
    # races sibling readers (dispatch-loop stall root cause). synchronous is a
    # per-connection setting (not stored in the header), so it is set each open;
    # NORMAL is the safe, faster choice under WAL.
    conn.execute("PRAGMA synchronous=NORMAL")
    if busy_timeout_ms is not None:
        # Overrides the 30s connect timeout for this connection.
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_task(
    conn: sqlite3.Connection,
    prompt: str = "",
    user_id: str = "",
    source_type: str = "cli",
    conversation_token: str | None = None,
    parent_task_id: int | None = None,
    is_group_chat: bool = False,
    attachments: list[str] | None = None,
    priority: int = 5,
    scheduled_for: str | None = None,
    output_target: str | None = None,
    talk_message_id: int | None = None,
    reply_to_talk_id: int | None = None,
    reply_to_content: str | None = None,
    heartbeat_silent: bool = False,
    skip_log_channel: bool = False,
    scheduled_job_id: int | None = None,
    briefing_name: str | None = None,
    command: str | None = None,
    queue: str = "foreground",
    model: str | None = None,
    effort: str | None = None,
    talk_delivery_token: str | None = None,
    skill: str | None = None,
    skill_args: str | None = None,
) -> int:
    """Create a new task and return its ID."""
    # Guard against duplicate Talk messages (race between overlapping poll cycles)
    if talk_message_id is not None:
        existing = conn.execute(
            "SELECT id FROM tasks WHERE talk_message_id = ? AND conversation_token = ?",
            (talk_message_id, conversation_token),
        ).fetchone()
        if existing:
            logger.warning(
                "Duplicate talk_message_id %d in conversation %s — "
                "task %d already exists, skipping",
                talk_message_id, conversation_token, existing[0],
            )
            return existing[0]

    cursor = conn.execute(
        """
        INSERT INTO tasks (
            prompt, command, user_id, source_type, conversation_token,
            parent_task_id, is_group_chat, attachments, priority, scheduled_for,
            output_target, talk_message_id, reply_to_talk_id, reply_to_content,
            heartbeat_silent, skip_log_channel, scheduled_job_id, briefing_name,
            queue, model, effort,
            talk_delivery_token, skill, skill_args
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            prompt,
            command,
            user_id,
            source_type,
            conversation_token,
            parent_task_id,
            1 if is_group_chat else 0,
            json.dumps(attachments) if attachments else None,
            priority,
            scheduled_for,
            output_target,
            talk_message_id,
            reply_to_talk_id,
            reply_to_content,
            1 if heartbeat_silent else 0,
            1 if skip_log_channel else 0,
            scheduled_job_id,
            briefing_name,
            queue,
            model or None,
            effort or None,
            talk_delivery_token,
            skill,
            skill_args,
        ),
    )
    task_id = cursor.fetchone()[0]
    logger.debug("Created task %d for user %s (source: %s)", task_id, user_id, source_type)
    return task_id


# Canonical SELECT/RETURNING column list for any query that reconstructs a
# `Task` via `_row_to_task`. Update this when adding a column to `tasks`;
# `_row_to_task` will then trip an `IndexError` from any SELECT that forgot
# to include the column, surfacing the omission as a test failure rather
# than a silent `None` (see commit 027eb1a — a missed `skill, skill_args`
# in `claim_task`'s RETURNING caused a 5-minute-loop production bug).
_TASK_COLUMNS = (
    "id, status, source_type, user_id, prompt, command, "
    "conversation_token, parent_task_id, is_group_chat, attachments, "
    "result, actions_taken, execution_trace, error, confirmation_prompt, "
    "priority, attempt_count, max_attempts, created_at, scheduled_for, "
    "output_target, talk_message_id, talk_response_id, reply_to_talk_id, "
    "reply_to_content, heartbeat_silent, skip_log_channel, scheduled_job_id, "
    "briefing_name, queue, confirmed_at, selected_skills, model, effort, model_used, "
    "talk_delivery_token, skill, skill_args"
)


def _row_to_task(row: sqlite3.Row) -> Task:
    """Convert a database row to a Task object.

    The row must include every column in `_TASK_COLUMNS`. Callers should
    use `_TASK_COLUMNS` in their SELECT/RETURNING clause; missing columns
    raise `IndexError` from `sqlite3.Row` rather than producing a silent
    `None`.
    """
    return Task(
        id=row["id"],
        status=row["status"],
        source_type=row["source_type"],
        user_id=row["user_id"],
        prompt=row["prompt"],
        command=row["command"],
        conversation_token=row["conversation_token"],
        parent_task_id=row["parent_task_id"],
        is_group_chat=bool(row["is_group_chat"]),
        attachments=json.loads(row["attachments"]) if row["attachments"] else None,
        result=row["result"],
        actions_taken=row["actions_taken"],
        execution_trace=row["execution_trace"],
        error=row["error"],
        confirmation_prompt=row["confirmation_prompt"],
        priority=row["priority"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        created_at=row["created_at"],
        scheduled_for=row["scheduled_for"],
        output_target=row["output_target"],
        talk_message_id=row["talk_message_id"],
        talk_response_id=row["talk_response_id"],
        reply_to_talk_id=row["reply_to_talk_id"],
        reply_to_content=row["reply_to_content"],
        heartbeat_silent=bool(row["heartbeat_silent"]),
        skip_log_channel=bool(row["skip_log_channel"]),
        scheduled_job_id=row["scheduled_job_id"],
        briefing_name=row["briefing_name"],
        queue=row["queue"],
        confirmed_at=row["confirmed_at"],
        selected_skills=row["selected_skills"],
        model=row["model"],
        effort=row["effort"],
        model_used=row["model_used"],
        talk_delivery_token=row["talk_delivery_token"],
        skill=row["skill"],
        skill_args=row["skill_args"],
    )


# A 'running' task counts as stuck (its worker presumed dead) when its liveness
# ping has gone silent — last_heartbeat older than ``heartbeat_stuck_minutes`` —
# or, when the worker never recorded a heartbeat (legacy rows, or a worker whose
# pinger never started), when it has simply been running past
# ``stuck_running_minutes``. A live worker refreshes last_heartbeat every cycle,
# so a healthy long task is never reclaimed regardless of how long it runs
# (ISSUE-112). The fragment binds two params, in this order: heartbeat window
# then started_at window — build them with ``_stuck_running_params``.
_STUCK_RUNNING_PREDICATE = (
    "((last_heartbeat IS NOT NULL "
    "AND last_heartbeat < datetime('now', ? || ' minutes')) "
    "OR (last_heartbeat IS NULL "
    "AND started_at < datetime('now', ? || ' minutes')))"
)

# Source types whose tasks are executed inline by their creator
# (`scheduler.run_task_inline`), never by a daemon worker. The daemon must not
# claim or dispatch these — a REPL turn creates a `pending` row and runs it
# in-process, so a concurrently-running daemon worker for the same user would
# otherwise claim and double-execute it (second brain run + second deferred-op
# drain). `claim_task` is the enforcement boundary; the discovery helpers below
# exclude them too so the daemon never spawns an idle worker for an inline task.
INLINE_ONLY_SOURCE_TYPES = ("repl",)
_INLINE_ONLY_IN = ", ".join(f"'{s}'" for s in INLINE_ONLY_SOURCE_TYPES)

# Per-channel gate: one active foreground task per conversation_token.
# A pending fg task is unclaimable while another fg task in the same channel is
# locked/running/pending_confirmation (the latter parks the room awaiting the
# user's confirmation, so the next queued message must wait rather than barge
# ahead — web chat's single-active-per-room + queue). Talk is unaffected: it
# cancels pending confirmations in the same poll transaction before creating the
# new task. Tasks with no conversation_token (cron, email) and background-queue
# tasks are unaffected. References the outer query's `tasks` alias.
#
# Shared verbatim between claim_task (what it will actually claim) and
# count_claimable_tasks_for_user_queue (what dispatch / the idle pre-check use
# to decide whether to spawn or poll a worker) so the count can never disagree
# with claimability — otherwise a worker spun up for a gated task busy-polls
# claim_task (and its stale-lock maintenance UPDATEs) until the gate clears.
_CLAIM_CHANNEL_GATE_SQL = """
            NOT (
                tasks.queue = 'foreground'
                AND tasks.conversation_token IS NOT NULL
                AND tasks.conversation_token != ''
                AND EXISTS (
                    SELECT 1 FROM tasks t2
                    WHERE t2.conversation_token = tasks.conversation_token
                    AND t2.queue = 'foreground'
                    AND t2.status IN ('locked', 'running', 'pending_confirmation')
                    AND t2.cancel_requested = 0
                    AND t2.id != tasks.id
                )
            )
            """


def _stuck_running_params(heartbeat_stuck_minutes: int, stuck_running_minutes: int) -> tuple:
    return (f"-{heartbeat_stuck_minutes}", f"-{stuck_running_minutes}")


def claim_task(
    conn: sqlite3.Connection,
    worker_id: str,
    max_retry_age_minutes: int = 60,
    user_id: str | None = None,
    queue: str | None = None,
    stuck_running_minutes: int = 15,
    heartbeat_stuck_minutes: int = 5,
) -> Task | None:
    """Atomically claim the next available task. Returns None if no tasks available.

    Args:
        worker_id: Unique identifier for the claiming worker.
        max_retry_age_minutes: Tasks older than this are failed instead of retried.
        user_id: If provided, only claim tasks for this user.
        queue: If provided, only claim tasks in this queue ('foreground' or 'background').
        stuck_running_minutes: Fallback stuck threshold for a 'running' task that
            never recorded a heartbeat (legacy rows). Must exceed the task
            timeout, or a healthy still-running worker — especially the in-process
            native brain, which has no killable PID — gets reclaimed and a second
            worker runs a duplicate (ISSUE-112). Callers pass
            ``task_timeout_minutes`` + a grace margin.
        heartbeat_stuck_minutes: Stuck threshold once the worker has recorded a
            heartbeat — how long last_heartbeat may go silent before the worker is
            presumed dead. Small (a few missed pings); independent of the timeout.
    """
    # First, fail old stale locks (created too long ago to be worth retrying)
    conn.execute(
        """
        UPDATE tasks
        SET status = 'failed', error = 'Task too old to retry (stale lock)',
            locked_at = NULL, locked_by = NULL
        WHERE status = 'locked'
        AND locked_at < datetime('now', '-30 minutes')
        AND created_at < datetime('now', ? || ' minutes')
        """,
        (f"-{max_retry_age_minutes}",),
    )

    # Release recent stale locks (younger tasks get retried)
    conn.execute(
        """
        UPDATE tasks
        SET status = 'pending', locked_at = NULL, locked_by = NULL
        WHERE status = 'locked'
        AND locked_at < datetime('now', '-30 minutes')
        AND created_at >= datetime('now', ? || ' minutes')
        """,
        (f"-{max_retry_age_minutes}",),
    )

    # Fail old stuck 'running' tasks (too old to be worth retrying)
    conn.execute(
        f"""
        UPDATE tasks
        SET status = 'failed', error = 'Task too old to retry (stuck running)'
        WHERE status = 'running'
        AND {_STUCK_RUNNING_PREDICATE}
        AND created_at < datetime('now', ? || ' minutes')
        """,
        (*_stuck_running_params(heartbeat_stuck_minutes, stuck_running_minutes),
         f"-{max_retry_age_minutes}"),
    )

    # Release recent stuck 'running' tasks for retry. Clear last_heartbeat too:
    # leaving the dead worker's stale heartbeat on the row would keep the
    # _STUCK_RUNNING_PREDICATE firing after the next worker re-claims and re-runs
    # it, letting a second concurrent claimer re-steal it (duplicate execution).
    conn.execute(
        f"""
        UPDATE tasks
        SET status = 'pending', started_at = NULL, locked_at = NULL, locked_by = NULL,
            last_heartbeat = NULL, attempt_count = attempt_count + 1
        WHERE status = 'running'
        AND {_STUCK_RUNNING_PREDICATE}
        AND created_at >= datetime('now', ? || ' minutes')
        AND attempt_count < max_attempts
        """,
        (*_stuck_running_params(heartbeat_stuck_minutes, stuck_running_minutes),
         f"-{max_retry_age_minutes}"),
    )

    # Mark stuck 'running' tasks as failed if they've exhausted retries
    conn.execute(
        f"""
        UPDATE tasks
        SET status = 'failed', error = 'Task stuck in running state - worker may have crashed'
        WHERE status = 'running'
        AND {_STUCK_RUNNING_PREDICATE}
        AND attempt_count >= max_attempts
        """,
        _stuck_running_params(heartbeat_stuck_minutes, stuck_running_minutes),
    )

    # Atomically claim a task (optionally filtered by user_id and/or queue).
    # Inline-only source types (REPL) are never claimed here — their creator
    # runs them in-process via run_task_inline.
    filters = [
        "status = 'pending'",
        f"source_type NOT IN ({_INLINE_ONLY_IN})",
        "(scheduled_for IS NULL OR scheduled_for <= datetime('now'))",
    ]
    params: list = [worker_id]
    if user_id is not None:
        filters.append("user_id = ?")
        params.append(user_id)
    if queue is not None:
        filters.append("queue = ?")
        params.append(queue)

    # Per-channel single-active-foreground gate (see _CLAIM_CHANNEL_GATE_SQL).
    if queue == "foreground" or queue is None:
        filters.append(_CLAIM_CHANNEL_GATE_SQL)

    where_clause = " AND ".join(filters)

    # Reset liveness (last_heartbeat + started_at) on claim so the new owner
    # starts with a clean slate. Without this, a task re-claimed from the
    # stuck-running path carries the dead worker's stale heartbeat into its
    # running window — until the new worker's first ping lands — and a second
    # worker calling claim_task in that window re-reclaims and re-runs it
    # (the duplicate-execution race; ISSUE-112). update_task_status('running')
    # sets started_at=now immediately after, before the row can look stuck.
    cursor = conn.execute(
        f"""
        UPDATE tasks
        SET status = 'locked', locked_at = datetime('now'), locked_by = ?,
            last_heartbeat = NULL, started_at = NULL
        WHERE id = (
            SELECT id FROM tasks
            WHERE {where_clause}
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
        )
        RETURNING {_TASK_COLUMNS}
        """,
        params,
    )
    row = cursor.fetchone()
    if not row:
        return None

    return _row_to_task(row)


def get_users_with_pending_tasks(conn: sqlite3.Connection) -> list[str]:
    """Get distinct user IDs that have pending tasks ready to run."""
    cursor = conn.execute(
        f"""
        SELECT DISTINCT user_id FROM tasks
        WHERE status = 'pending'
        AND source_type NOT IN ({_INLINE_ONLY_IN})
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        """
    )
    return [row[0] for row in cursor.fetchall()]


def get_task(conn: sqlite3.Connection, task_id: int) -> Task | None:
    """Get a task by ID."""
    cursor = conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?",
        (task_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None

    return _row_to_task(row)


_SUBTASK_DEPTH_HARD_CAP = 100


def get_subtask_depth(conn: sqlite3.Connection, task_id: int) -> int:
    """Walk parent_task_id chain and return how deep `task_id` sits.

    A user-initiated task (no parent) returns 0; its first child returns 1, etc.
    Capped at _SUBTASK_DEPTH_HARD_CAP to terminate on pathological chains;
    callers should treat the cap as "very deep, refuse further work."
    """
    depth = 0
    current = task_id
    while depth < _SUBTASK_DEPTH_HARD_CAP:
        row = conn.execute(
            "SELECT parent_task_id FROM tasks WHERE id = ?", (current,),
        ).fetchone()
        if row is None or row["parent_task_id"] is None:
            return depth
        current = row["parent_task_id"]
        depth += 1
    return depth


def update_task_status(
    conn: sqlite3.Connection,
    task_id: int,
    status: str,
    result: str | None = None,
    error: str | None = None,
    actions_taken: str | None = None,
    execution_trace: str | None = None,
) -> None:
    """Update task status and optionally result/error."""
    if status == "running":
        conn.execute(
            "UPDATE tasks SET status = ?, started_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
            (status, task_id),
        )
    elif status == "completed":
        conn.execute(
            "UPDATE tasks SET status = ?, completed_at = datetime('now'), result = ?, actions_taken = ?, execution_trace = ?, updated_at = datetime('now') WHERE id = ?",
            (status, result, actions_taken, execution_trace, task_id),
        )
    elif status == "failed":
        conn.execute(
            "UPDATE tasks SET status = ?, completed_at = datetime('now'), error = ?, updated_at = datetime('now') WHERE id = ?",
            (status, error, task_id),
        )
    else:
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, task_id),
        )


def set_task_pending_retry(
    conn: sqlite3.Connection,
    task_id: int,
    error: str,
    retry_delay_minutes: int,
) -> None:
    """Mark task for retry after a delay.

    Clears last_heartbeat/started_at so the retried row doesn't carry the prior
    attempt's liveness into the next claim — the claim itself also resets these
    (defense in depth), but a pending row shouldn't advertise a dead worker's
    heartbeat in the meantime.
    """
    conn.execute(
        """
        UPDATE tasks
        SET status = 'pending',
            attempt_count = attempt_count + 1,
            error = ?,
            scheduled_for = datetime('now', '+' || ? || ' minutes'),
            locked_at = NULL,
            locked_by = NULL,
            last_heartbeat = NULL,
            started_at = NULL,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (error, retry_delay_minutes, task_id),
    )


def set_task_confirmation(
    conn: sqlite3.Connection,
    task_id: int,
    confirmation_prompt: str,
) -> None:
    """Set task to pending confirmation status."""
    conn.execute(
        """
        UPDATE tasks
        SET status = 'pending_confirmation',
            confirmation_prompt = ?,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (confirmation_prompt, task_id),
    )


def confirm_task(conn: sqlite3.Connection, task_id: int) -> None:
    """Confirm a task that was pending confirmation."""
    conn.execute(
        """
        UPDATE tasks
        SET status = 'pending',
            confirmed_at = datetime('now'),
            updated_at = datetime('now')
        WHERE id = ? AND status = 'pending_confirmation'
        """,
        (task_id,),
    )


def cancel_task(conn: sqlite3.Connection, task_id: int) -> None:
    """Cancel a task (sets status to 'cancelled')."""
    conn.execute(
        """
        UPDATE tasks
        SET status = 'cancelled',
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (task_id,),
    )


def cancel_pending_confirmations(
    conn: sqlite3.Connection,
    conversation_token: str,
    user_id: str,
) -> int:
    """Cancel all pending_confirmation tasks for a user in a conversation.

    Called when a new task is created in the same conversation, indicating the
    user has moved on from the pending confirmation.
    """
    cursor = conn.execute(
        """
        UPDATE tasks
        SET status = 'cancelled',
            updated_at = datetime('now')
        WHERE conversation_token = ?
          AND user_id = ?
          AND status = 'pending_confirmation'
        """,
        (conversation_token, user_id),
    )
    return cursor.rowcount


def get_pending_confirmation(
    conn: sqlite3.Connection,
    conversation_token: str,
) -> Task | None:
    """
    Get a task that is pending confirmation for a conversation.

    Returns the most recent task awaiting confirmation, or None if none found.
    """
    cursor = conn.execute(
        f"""
        SELECT {_TASK_COLUMNS}
        FROM tasks
        WHERE conversation_token = ?
        AND status = 'pending_confirmation'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (conversation_token,),
    )
    row = cursor.fetchone()
    if not row:
        return None

    return _row_to_task(row)


def get_pending_confirmation_for_user(
    conn: sqlite3.Connection,
    user_id: str,
) -> Task | None:
    """Get the newest pending_confirmation task for a user, any conversation."""
    cursor = conn.execute(
        f"""
        SELECT {_TASK_COLUMNS}
        FROM tasks
        WHERE user_id = ? AND status = 'pending_confirmation'
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return _row_to_task(row)


def get_pending_confirmation_by_response_id(
    conn: sqlite3.Connection,
    talk_response_id: int,
) -> Task | None:
    """Get a pending_confirmation task by its Talk confirmation message ID."""
    cursor = conn.execute(
        f"""
        SELECT {_TASK_COLUMNS}
        FROM tasks
        WHERE talk_response_id = ? AND status = 'pending_confirmation'
        LIMIT 1
        """,
        (talk_response_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return _row_to_task(row)


def _decode_resource_extras(raw: object) -> dict[str, Any]:
    """Parse the extras JSON column. Falls back to {} on missing or corrupt data."""
    if raw is None or raw == "":
        return {}
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("user_resources.extras contained invalid JSON; defaulting to {}")
        return {}
    return decoded if isinstance(decoded, dict) else {}


def get_user_resources(
    conn: sqlite3.Connection,
    user_id: str,
    resource_type: str | None = None,
) -> list[UserResource]:
    """Get resources accessible to a user."""
    if resource_type:
        cursor = conn.execute(
            """
            SELECT id, user_id, resource_type, resource_path, display_name, permissions, extras
            FROM user_resources
            WHERE user_id = ? AND resource_type = ?
            """,
            (user_id, resource_type),
        )
    else:
        cursor = conn.execute(
            """
            SELECT id, user_id, resource_type, resource_path, display_name, permissions, extras
            FROM user_resources
            WHERE user_id = ?
            """,
            (user_id,),
        )

    return [
        UserResource(
            id=row["id"],
            user_id=row["user_id"],
            resource_type=row["resource_type"],
            resource_path=row["resource_path"],
            display_name=row["display_name"],
            permissions=row["permissions"],
            extras=_decode_resource_extras(row["extras"]),
        )
        for row in cursor.fetchall()
    ]


# Sentinel for add_user_resource: distinguishes "caller didn't pass extras"
# (preserve existing column on update) from "caller passed an explicit value
# including {}" (overwrite). Operators clearing extras through the CLI / web
# UI must produce a write, not a no-op.
_EXTRAS_UNCHANGED = object()


def add_user_resource(
    conn: sqlite3.Connection,
    user_id: str,
    resource_type: str,
    resource_path: str,
    display_name: str | None = None,
    permissions: str = "read",
    extras: "dict[str, Any] | object" = _EXTRAS_UNCHANGED,
) -> int:
    """Upsert a resource for a user.

    On conflict (user_id, resource_type, resource_path) the row's
    display_name + permissions are overwritten. ``extras`` follows
    partial-update semantics matching ``istota user ensure``: when the caller
    omits the kwarg, the existing column value is preserved; passing an
    explicit dict (including ``{}``) overwrites.
    """
    if extras is _EXTRAS_UNCHANGED:
        cursor = conn.execute(
            """
            INSERT INTO user_resources (user_id, resource_type, resource_path, display_name, permissions, extras)
            VALUES (?, ?, ?, ?, ?, NULL)
            ON CONFLICT (user_id, resource_type, resource_path) DO UPDATE SET
                display_name = excluded.display_name,
                permissions = excluded.permissions
            RETURNING id
            """,
            (user_id, resource_type, resource_path, display_name, permissions),
        )
    else:
        extras_json = json.dumps(extras) if extras else None
        cursor = conn.execute(
            """
            INSERT INTO user_resources (user_id, resource_type, resource_path, display_name, permissions, extras)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id, resource_type, resource_path) DO UPDATE SET
                display_name = excluded.display_name,
                permissions = excluded.permissions,
                extras = excluded.extras
            RETURNING id
            """,
            (user_id, resource_type, resource_path, display_name, permissions, extras_json),
        )
    return cursor.fetchone()[0]


def upsert_user_resource(
    conn: sqlite3.Connection,
    user_id: str,
    resource_type: str,
    resource_path: str,
    *,
    display_name: str | None = None,
    permissions: str = "read",
    extras: "dict[str, Any] | object" = _EXTRAS_UNCHANGED,
) -> "tuple[int, str]":
    """Idempotent resource upsert. Returns ``(resource_id, state)``.

    ``state`` is one of ``"created"``, ``"updated"``, ``"noop"`` — same
    contract as ``user_briefings.ensure_briefing`` and
    ``secrets_store.upsert_secret``. ``extras`` follows the same partial-
    update sentinel as :func:`add_user_resource`: omitting the kwarg
    preserves the existing value; passing an explicit dict (including
    ``{}``) overwrites.
    """
    existing = next(
        (r for r in get_user_resources(conn, user_id)
         if r.resource_type == resource_type and r.resource_path == resource_path),
        None,
    )

    if existing is None:
        state = "created"
    else:
        # Compute would-be value so omitted extras = preserve.
        next_extras = existing.extras if extras is _EXTRAS_UNCHANGED else extras
        same = (
            (existing.display_name or "") == (display_name or "")
            and (existing.permissions or "read") == permissions
            and (existing.extras or {}) == (next_extras or {})
        )
        state = "noop" if same else "updated"

    resource_id = add_user_resource(
        conn,
        user_id=user_id,
        resource_type=resource_type,
        resource_path=resource_path,
        display_name=display_name,
        permissions=permissions,
        extras=extras,
    )
    return resource_id, state


def delete_user_resource(
    conn: sqlite3.Connection,
    user_id: str,
    resource_id: int,
) -> bool:
    """Delete a resource by id, scoped to user_id (web UI safety).

    Returns True if a row was removed. The user_id scope prevents one user
    from deleting another user's resource by guessing IDs from the URL.
    """
    cur = conn.execute(
        "DELETE FROM user_resources WHERE id = ? AND user_id = ?",
        (resource_id, user_id),
    )
    return cur.rowcount > 0


# Resource types retired by the modules / connected services refactor.
# Their data flows through is_module_enabled (feeds, money, location) or the
# encrypted secrets table (karakeep, monarch). Cleaning them out of
# user_resources keeps stale rows from leaking into the executor / web UI.
_OBSOLETE_RESOURCE_TYPES = (
    "feeds", "money", "monarch", "moneyman", "karakeep", "overland",
)


def cleanup_obsolete_resources(db_path: Path) -> int:
    """Delete rows from ``user_resources`` whose type is no longer recognized.

    Idempotent: a missing DB or table is treated as a no-op. Returns the
    number of rows removed; intended to run once at scheduler startup after
    the secrets-store import has absorbed the credentials.
    """
    if db_path is None or not Path(db_path).exists():
        return 0
    placeholders = ",".join("?" * len(_OBSOLETE_RESOURCE_TYPES))
    try:
        with get_db(db_path) as conn:
            cur = conn.execute(
                f"DELETE FROM user_resources WHERE resource_type IN ({placeholders})",
                _OBSOLETE_RESOURCE_TYPES,
            )
            return cur.rowcount or 0
    except sqlite3.OperationalError:
        return 0


def get_briefing_last_run(conn: sqlite3.Connection, user_id: str, briefing_name: str) -> str | None:
    """Get the last run timestamp for a config-based briefing."""
    cursor = conn.execute(
        "SELECT last_run_at FROM briefing_state WHERE user_id = ? AND briefing_name = ?",
        (user_id, briefing_name),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def set_briefing_last_run(conn: sqlite3.Connection, user_id: str, briefing_name: str) -> None:
    """Set the last run timestamp for a config-based briefing.

    Truncates seconds to :00 so croniter (minute resolution) never computes
    a next-fire time within the same minute, preventing double-fires.
    """
    conn.execute(
        """
        INSERT INTO briefing_state (user_id, briefing_name, last_run_at)
        VALUES (?, ?, strftime('%Y-%m-%d %H:%M:00', 'now'))
        ON CONFLICT (user_id, briefing_name) DO UPDATE SET
            last_run_at = strftime('%Y-%m-%d %H:%M:00', 'now')
        """,
        (user_id, briefing_name),
    )


@dataclass
class ConversationMessage:
    id: int
    prompt: str
    result: str
    created_at: str
    actions_taken: str | None = None
    source_type: str = "talk"
    user_id: str | None = None


@dataclass
class TalkMessage:
    """A message from the Talk API, used for Talk-based conversation context."""
    message_id: int          # Talk message ID
    actor_id: str            # Nextcloud username
    actor_display_name: str  # Display name from API
    is_bot: bool             # actor_id == bot_username
    content: str             # cleaned text (placeholders resolved)
    timestamp: int           # unix timestamp
    actions_taken: str | None  # from DB, only for bot result messages
    message_role: str        # "user" | "bot_result" | "scheduled"
    task_id: int | None      # parsed from referenceId


# Source types whose turns the canonical `messages` store mirrors as
# user+assistant pairs — i.e. genuine room conversations. Scheduled/cron,
# briefing, and heartbeat posts are one-directional bot output (assistant-only,
# no user turn) and don't count toward the unified-read completeness check.
_CONVERSATIONAL_SOURCE_TYPES = ("talk", "web")


def get_conversation_history(
    conn: sqlite3.Connection,
    conversation_token: str,
    exclude_task_id: int | None = None,
    limit: int = 10,
    exclude_source_types: list[str] | None = None,
) -> list[ConversationMessage]:
    """
    Get completed conversation history for a conversation token.

    Returns the most recent N completed tasks (oldest-first order),
    excluding the current task if specified.

    Reads from the canonical `messages` store (unified Talk/web room sync) when
    that store is caught up to the latest completed task for the token,
    otherwise falls back to the legacy `tasks` reconstruction. The dual-read is
    self-healing: until live assistant-message writes land for every surface,
    any token whose newest completed turn isn't yet mirrored into `messages`
    transparently uses the `tasks` path, so context never goes stale.

    Args:
        exclude_source_types: If provided, exclude tasks with these source_types
            from the history (e.g. ["scheduled", "briefing", "heartbeat"]).
    """
    if _messages_caught_up(conn, conversation_token):
        return _conversation_history_from_messages(
            conn, conversation_token, exclude_task_id, limit, exclude_source_types,
        )
    return _conversation_history_from_tasks(
        conn, conversation_token, exclude_task_id, limit, exclude_source_types,
    )


def _conversation_history_from_tasks(
    conn: sqlite3.Connection,
    conversation_token: str,
    exclude_task_id: int | None,
    limit: int,
    exclude_source_types: list[str] | None,
) -> list[ConversationMessage]:
    """Legacy path: reconstruct history from completed `tasks` rows."""
    query = """
        SELECT id, prompt, result, created_at, actions_taken, source_type, user_id
        FROM tasks
        WHERE conversation_token = ?
        AND status = 'completed'
        AND result IS NOT NULL
    """
    params: list = [conversation_token]

    if exclude_task_id is not None:
        query += " AND id != ?"
        params.append(exclude_task_id)

    if exclude_source_types:
        placeholders = ", ".join("?" for _ in exclude_source_types)
        query += f" AND source_type NOT IN ({placeholders})"
        params.extend(exclude_source_types)

    # Get most recent N, then reverse for oldest-first order
    # Use id as tiebreaker for same-second timestamps
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)

    cursor = conn.execute(query, params)
    rows = cursor.fetchall()

    # Return in oldest-first order
    return [
        ConversationMessage(
            id=row["id"],
            prompt=row["prompt"],
            result=row["result"],
            created_at=row["created_at"],
            actions_taken=row["actions_taken"] if "actions_taken" in row.keys() else None,
            source_type=row["source_type"] if "source_type" in row.keys() else "talk",
            user_id=row["user_id"] if "user_id" in row.keys() else None,
        )
        for row in reversed(rows)
    ]


def _conversation_history_from_messages(
    conn: sqlite3.Connection,
    conversation_token: str,
    exclude_task_id: int | None,
    limit: int,
    exclude_source_types: list[str] | None,
) -> list[ConversationMessage]:
    """Unified path: re-pair `messages` user/assistant rows (keyed on task_id)
    back into the (prompt, result) ConversationMessage shape callers expect.

    The user row and assistant row of one turn share a `task_id`; the join to
    `tasks` recovers per-task metadata (source_type, user_id, actions_taken) the
    role/body-only message rows don't carry, and applies the same
    completed/result-present + exclusion filters as the legacy path. An in-flight
    turn (user row, no assistant row yet) is excluded by the inner join, exactly
    as the `result IS NOT NULL` filter excludes it today. `id` stays the task id
    so reply-parent / memory-dedup callers keyed on it are unaffected.
    """
    query = """
        SELECT t.id AS id, mu.body AS prompt, ma.body AS result,
               t.created_at AS created_at, t.actions_taken AS actions_taken,
               t.source_type AS source_type, t.user_id AS user_id
        FROM messages mu
        JOIN messages ma
          ON ma.room_token = mu.room_token AND ma.task_id = mu.task_id
             AND ma.role = 'assistant'
        JOIN tasks t ON t.id = mu.task_id
        WHERE mu.room_token = ? AND mu.role = 'user'
          AND t.status = 'completed'
    """
    params: list = [conversation_token]

    if exclude_task_id is not None:
        query += " AND t.id != ?"
        params.append(exclude_task_id)

    if exclude_source_types:
        placeholders = ", ".join("?" for _ in exclude_source_types)
        query += f" AND t.source_type NOT IN ({placeholders})"
        params.extend(exclude_source_types)

    query += " ORDER BY t.created_at DESC, t.id DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [
        ConversationMessage(
            id=row["id"],
            prompt=row["prompt"],
            result=row["result"],
            created_at=row["created_at"],
            actions_taken=row["actions_taken"] if "actions_taken" in row.keys() else None,
            source_type=row["source_type"] if "source_type" in row.keys() else "talk",
            user_id=row["user_id"] if "user_id" in row.keys() else None,
        )
        for row in reversed(rows)
    ]


def _messages_caught_up(conn: sqlite3.Connection, conversation_token: str) -> bool:
    """True when the canonical `messages` store can authoritatively serve a
    token's history: there is at least one completed turn and *every* completed
    task (with a result) for the token has its assistant row present in
    `messages`.

    This is a completeness check, not a newest-only check. Keying solely on the
    single newest task (the original implementation) made the dual-read
    all-or-nothing: once the latest turn was mirrored, the reader switched
    entirely to `messages` and silently dropped any *older* turn that wasn't yet
    mirrored — the exact state left by a partial migration or a mid-rollout
    window. A single missing assistant row now keeps the reader on the
    always-complete `tasks` path instead of truncating context to the mirrored
    subset.

    Cheap (one scalar + one bounded existence probe). Until live assistant
    writes land for every completed turn, this returns False and the caller
    falls back to `tasks` — no staleness during rollout.

    Scoped to *conversational* source types (talk/web). The dual-read protects
    conversational history from going stale; scheduled/cron and briefing posts
    aren't conversational turns, are never re-paired into history by
    `_conversation_history_from_messages` (they carry no user row), and a silent
    NO_ACTION tick deliberately has no message row at all. Letting them gate the
    caught-up check would peg any room with a cron job to the `tasks` path
    forever (and re-expose the dormant-room history loss once those tasks are
    GC'd). `_CONVERSATIONAL_SOURCE_TYPES` keys the check on the turns the store
    actually mirrors as user+assistant pairs."""
    placeholders = ", ".join("?" for _ in _CONVERSATIONAL_SOURCE_TYPES)
    types = tuple(_CONVERSATIONAL_SOURCE_TYPES)
    row = conn.execute(
        f"SELECT MAX(id) AS mx FROM tasks "
        f"WHERE conversation_token = ? AND status = 'completed' "
        f"AND result IS NOT NULL AND source_type IN ({placeholders})",
        (conversation_token, *types),
    ).fetchone()
    latest = row["mx"] if row else None
    if latest is None:
        return False  # no completed conversational history -> tasks path returns []
    # Any completed conversational turn missing its assistant row -> not caught
    # up -> fall back.
    gap = conn.execute(
        f"SELECT 1 FROM tasks t "
        f"WHERE t.conversation_token = ? AND t.status = 'completed' "
        f"  AND t.result IS NOT NULL AND t.source_type IN ({placeholders}) "
        f"  AND NOT EXISTS ("
        f"    SELECT 1 FROM messages m "
        f"    WHERE m.room_token = t.conversation_token "
        f"      AND m.task_id = t.id AND m.role = 'assistant'"
        f"  ) LIMIT 1",
        (conversation_token, *types),
    ).fetchone()
    return gap is None


def scheduled_assistant_body(heartbeat_silent: bool, result: str) -> str | None:
    """The transcript body for a scheduled (cron) job's assistant turn, or None
    when the turn was never delivered and must be omitted.

    Mirrors the scheduler's silent ACTION/NO_ACTION handling so a transcript
    (live write or backfill) matches what was actually posted: a silent job's
    `NO_ACTION:` tick posted nothing (omit), an `ACTION: X` posted "X" (store
    stripped), anything without a prefix posted as-is (fail-safe). Non-silent
    jobs post their raw result unchanged. Single source of truth — the scheduler
    delivery path (`_strip_action_prefix`) delegates here."""
    if not heartbeat_silent:
        return result
    if result.startswith("ACTION:"):
        return result[len("ACTION:"):].strip()
    idx = result.find("\nACTION:")
    if idx != -1:
        return result[idx + len("\nACTION:"):].strip()
    if "NO_ACTION:" in result:
        return None
    return result


def _backfill_turns_for(conn: sqlite3.Connection, where: str, params: tuple) -> int:
    """Shared transcript backfill: fold completed `tasks` rows matching `where`
    into the canonical `messages` store. One user row (body=prompt) + one
    assistant row (body=result) per conversational turn; a *scheduled* job
    contributes the assistant post only, body-normalized via
    `scheduled_assistant_body` (its synthetic cron prompt was never
    user-authored, so no user row, and a NO_ACTION tick is omitted entirely).
    Idempotent via the partial unique index (room_token, origin_surface, role,
    task_id). Returns rows inserted."""
    rows = conn.execute(
        f"SELECT id, conversation_token, prompt, result, source_type, "
        f"heartbeat_silent, created_at FROM tasks "
        f"WHERE {where} AND status = 'completed' AND result IS NOT NULL",
        params,
    ).fetchall()
    inserted = 0
    for r in rows:
        token = r["conversation_token"]
        st = r["source_type"]
        created = r["created_at"]
        if st == "scheduled":
            body = scheduled_assistant_body(bool(r["heartbeat_silent"]), r["result"])
            if body is None:
                continue  # never-delivered NO_ACTION tick
            inserted += _insert_recovered_message(
                conn, token, "assistant", body, r["id"], created, origin_surface=st,
            )
            continue
        inserted += _insert_recovered_message(
            conn, token, "user", r["prompt"], r["id"], created, origin_surface=st,
        )
        inserted += _insert_recovered_message(
            conn, token, "assistant", r["result"], r["id"], created, origin_surface=st,
        )
    return inserted


def backfill_room_messages_from_tasks(
    conn: sqlite3.Connection, conversation_token: str,
) -> int:
    """Populate the canonical `messages` store from completed `tasks` for one
    room token. See `_backfill_turns_for` for the per-turn shape."""
    return _backfill_turns_for(
        conn, "conversation_token = ?", (conversation_token,),
    )


def backfill_room_messages_from_talk_cache(
    conn: sqlite3.Connection, conversation_token: str,
) -> int:
    """Recover a room's durable transcript from the Talk message cache.

    The web transcript is rebuilt from the canonical `messages` store, but for a
    room whose conversation predates the unified-room-sync migration (or the
    task-retention window) the originating `tasks` rows are gone — the only
    surviving copy of those turns is `talk_messages`. This folds them in: one
    user row (the prompt) + one assistant row (the result) per completed Talk
    turn, keyed on the task id parsed from the bot result's
    `istota:task:<id>:result` reference. Idempotent via the messages unique
    index. Returns the number of message rows inserted.

    A turn is reconstructed from the cache's shape (message_id ascending):

        [human  comment]            -> the prompt
        [bot    :ack    comment]    -> skipped
        [bot    system  "edited"]   -> skipped
        [bot    :result comment]    -> the answer, carries the task id

    The prompt is the nearest preceding human comment before the result; an
    unpaired result (its prompt predates the cache window) yields the assistant
    row alone. Failed/cancelled turns have no `:result` cache row, so this only
    recovers completed turns — which is exactly the set task-retention GCs.
    """
    rows = conn.execute(
        "SELECT message_id, actor_id, message_type, reference_id, message_text, "
        "message_parameters, timestamp FROM talk_messages "
        "WHERE conversation_token = ? AND deleted = 0 "
        "ORDER BY message_id ASC",
        (conversation_token,),
    ).fetchall()
    if not rows:
        return 0
    # The bot is whoever authors the istota:task:* references.
    bot_actor: str | None = None
    for r in rows:
        if (r["reference_id"] or "").startswith("istota:task:"):
            bot_actor = r["actor_id"]
            break
    if bot_actor is None:
        return 0  # no bot turns cached -> nothing to recover

    def _iso(ts) -> str | None:
        try:
            return datetime.fromtimestamp(
                int(ts), tz=timezone.utc,
            ).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError, OSError, OverflowError):
            return None

    # Resolve Talk rich-object placeholders ({file}, {mention-…}, polls, …)
    # against the cached messageParameters before folding text into the durable
    # store — the cache holds the *raw* body, so without this the recovered
    # transcript leaks literal placeholder tokens to the web UI (ISSUE-132). The
    # live inbound path already resolves; only this cache-recovery path didn't.
    from .talk import clean_message_content

    def _resolved(row) -> str:
        params = row["message_parameters"]
        if params:
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, TypeError):
                params = {}
        else:
            params = {}
        return clean_message_content(
            {"message": row["message_text"] or "", "messageParameters": params}
        )

    inserted = 0
    pending_user: tuple[str, object] | None = None  # (text, ts), unconsumed
    for r in rows:
        if r["message_type"] != "comment":
            continue  # system notices ("You edited a message"), etc.
        ref = r["reference_id"] or ""
        is_bot_ref = ref.startswith("istota:task:")
        if r["actor_id"] != bot_actor and not is_bot_ref:
            # A human comment — the candidate prompt for the next bot result.
            pending_user = (_resolved(r), r["timestamp"])
            continue
        if is_bot_ref and ref.endswith(":result"):
            parts = ref.split(":")
            try:
                task_id = int(parts[2])
            except (IndexError, ValueError):
                continue
            # User row first so id/created_at order matches the conversation.
            if pending_user is not None:
                inserted += _insert_recovered_message(
                    conn, conversation_token, "user",
                    pending_user[0], task_id, _iso(pending_user[1]),
                )
                pending_user = None
            inserted += _insert_recovered_message(
                conn, conversation_token, "assistant",
                _resolved(r), task_id, _iso(r["timestamp"]),
            )
        # ack rows (:ack) and any other bot comment fall through (skipped).
    return inserted


def _insert_recovered_message(
    conn: sqlite3.Connection, token: str, role: str, body: str,
    task_id: int, created_at: str | None, origin_surface: str = "talk",
) -> int:
    """INSERT OR IGNORE one recovered turn message with an explicit historical
    `created_at`. Idempotent via the (room_token, origin_surface, role,
    task_id) unique index. Returns 1 if a row was inserted, else 0."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages "
        "(room_token, role, body, task_id, origin_surface, created_at) "
        "VALUES (?, ?, ?, ?, ?, COALESCE(?, datetime('now')))",
        (token, role, body, task_id, origin_surface, created_at),
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def get_previous_tasks(
    conn: sqlite3.Connection,
    conversation_token: str,
    exclude_task_id: int | None = None,
    limit: int = 3,
    exclude_source_types: list[str] | None = None,
) -> list[ConversationMessage]:
    """
    Get the most recent completed tasks in a conversation.

    Deliberately re-surfaces recent tasks whose ``source_type`` the primary
    ``get_conversation_history`` excludes (e.g. ``scheduled`` / ``briefing`` cron
    output the user may reference), so they stay available in the email /
    Talk-API-fallback context builder. ``exclude_source_types`` hard-excludes
    types that must NOT be re-surfaced even here — non-conversational internal
    artifacts (``subtask``'s synthetic orchestration prompt, throwaway
    ``heartbeat`` posts) that would otherwise read back as prior user
    conversation (canonical-room-transcript spec: this is the ``get_previous_tasks``
    half of the LLM-context isolation invariant, complementing
    ``get_conversation_history``'s ``exclude_source_types``). Returns up to
    ``limit`` tasks in oldest-first order.
    """
    query = """
        SELECT id, prompt, result, created_at, actions_taken, source_type, user_id
        FROM tasks
        WHERE conversation_token = ?
        AND status = 'completed'
        AND result IS NOT NULL
    """
    params: list = [conversation_token]

    if exclude_source_types:
        placeholders = ", ".join("?" for _ in exclude_source_types)
        query += f" AND source_type NOT IN ({placeholders})"
        params.extend(exclude_source_types)

    if exclude_task_id is not None:
        query += " AND id != ?"
        params.append(exclude_task_id)

    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)

    cursor = conn.execute(query, params)
    rows = cursor.fetchall()

    results = [
        ConversationMessage(
            id=row["id"],
            prompt=row["prompt"],
            result=row["result"],
            created_at=row["created_at"],
            actions_taken=row["actions_taken"] if "actions_taken" in row.keys() else None,
            source_type=row["source_type"] if "source_type" in row.keys() else "talk",
            user_id=row["user_id"] if "user_id" in row.keys() else None,
        )
        for row in rows
    ]
    # Return in oldest-first order (query fetches newest-first)
    results.reverse()
    return results


def get_task_metadata_for_context(
    conn: sqlite3.Connection,
    task_ids: list[int],
) -> dict[int, dict]:
    """Batch lookup of task metadata for Talk-based context enrichment.

    Given a list of task IDs (parsed from referenceIds in Talk messages),
    returns a dict mapping task_id to {"actions_taken": ..., "source_type": ...}.
    """
    if not task_ids:
        return {}

    placeholders = ", ".join("?" for _ in task_ids)
    query = f"""
        SELECT id, actions_taken, source_type
        FROM tasks
        WHERE id IN ({placeholders})
        AND status = 'completed'
    """
    cursor = conn.execute(query, task_ids)
    return {
        row["id"]: {
            "actions_taken": row["actions_taken"],
            "source_type": row["source_type"],
        }
        for row in cursor.fetchall()
    }


def log_task(
    conn: sqlite3.Connection,
    task_id: int,
    level: str,
    message: str,
) -> None:
    """Add a log entry for a task."""
    conn.execute(
        "INSERT INTO task_logs (task_id, level, message) VALUES (?, ?, ?)",
        (task_id, level, message),
    )


def get_task_logs(
    conn: sqlite3.Connection,
    task_id: int,
    level: str | None = None,
) -> list[dict]:
    """Get logs for a task."""
    if level:
        cursor = conn.execute(
            "SELECT * FROM task_logs WHERE task_id = ? AND level = ? ORDER BY timestamp",
            (task_id, level),
        )
    else:
        cursor = conn.execute(
            "SELECT * FROM task_logs WHERE task_id = ? ORDER BY timestamp",
            (task_id,),
        )
    return [dict(row) for row in cursor.fetchall()]


# ============================================================================
# Task event stream (task-event-streaming spec)
# ============================================================================


def get_task_events(
    conn: sqlite3.Connection,
    task_id: int,
    since_seq: int = 0,
    limit: int | None = None,
) -> list[dict]:
    """Return a task's events with ``seq > since_seq``, oldest first.

    ``payload`` is decoded from JSON into a dict. Used by the web SSE generator
    and the admin task-detail view — a range scan on the ``(task_id, seq)``
    index, fast regardless of table size.
    """
    sql = (
        "SELECT id, task_id, seq, kind, payload, created_at FROM task_events "
        "WHERE task_id = ? AND seq > ? ORDER BY seq"
    )
    params: list = [task_id, since_seq]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cursor = conn.execute(sql, params)
    events = []
    for row in cursor.fetchall():
        d = dict(row)
        try:
            d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
        except (json.JSONDecodeError, TypeError):
            d["payload"] = {}
        events.append(d)
    return events


def get_max_task_event_seq(conn: sqlite3.Connection, task_id: int) -> int:
    """The highest ``seq`` written for a task, or 0 if it has no events.

    Lets a retry's fresh ``EventWriter`` resume the counter instead of restarting
    at 1 — keeping ``seq`` monotonic across attempts so a watching web client's
    resume cursor stays valid (the log is no longer wiped between attempts) and
    UNIQUE(task_id, seq) never collides.
    """
    row = conn.execute(
        "SELECT MAX(seq) FROM task_events WHERE task_id = ?", (task_id,)
    ).fetchone()
    return (row[0] or 0) if row else 0


def delete_task_events(conn: sqlite3.Connection, task_id: int) -> int:
    """Delete all events for a task. Returns the row count.

    Used by retention cleanup. (No longer called on retry — the event log now
    spans all attempts so the live stream survives a retry; ``EventWriter``
    resumes ``seq`` via ``get_max_task_event_seq`` instead of resetting to 1.)
    """
    cursor = conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
    return cursor.rowcount


def delete_task_events_by_kind(
    conn: sqlite3.Connection, task_id: int, kind: str,
) -> int:
    """Delete a task's events of one kind only. Returns the row count.

    Used to prune ephemeral ``text_delta`` rows for stream surfaces once the
    canonical ``result`` has been emitted (web-chat streaming): the deltas were a
    cosmetic live preview, so steady state retains zero of them. Gaps in ``seq``
    are harmless — SSE resume is ``seq > last``. Mirrors ``delete_task_events``'
    connection-handling convention (caller supplies the connection)."""
    cursor = conn.execute(
        "DELETE FROM task_events WHERE task_id = ? AND kind = ?", (task_id, kind),
    )
    return cursor.rowcount


def append_task_event(
    conn: sqlite3.Connection, task_id: int, kind: str, payload: dict | None = None,
) -> int | None:
    """Append a single event to a task's log from *outside* the running worker.

    The worker owns an in-memory ``EventWriter`` that increments ``seq`` per
    emit; a foreign writer (the ``!steer`` command, running in the poller / web
    process) can't share that counter, so it computes the next ``seq`` atomically
    as ``MAX(seq)+1`` in one statement and inserts. A rare collision with the
    live worker's concurrent insert (both picking the same ``seq``) raises
    ``IntegrityError`` on ``UNIQUE(task_id, seq)``; we retry a few times, then
    give up (best-effort, exactly like ``EventWriter._write_to_db``). Returns the
    assigned ``seq`` on success, ``None`` if it couldn't be persisted.
    """
    import json as _json

    payload_json = _json.dumps(payload or {}, default=str)
    for _ in range(5):
        try:
            row = conn.execute(
                "INSERT INTO task_events (task_id, seq, kind, payload) "
                "VALUES (?, "
                "  (SELECT COALESCE(MAX(seq), 0) + 1 FROM task_events WHERE task_id = ?), "
                "  ?, ?) "
                "RETURNING seq",
                (task_id, task_id, kind, payload_json),
            ).fetchone()
            conn.commit()
            return int(row[0]) if row else None
        except sqlite3.IntegrityError:
            # Seq collided with the live worker's concurrent emit — recompute.
            continue
    logger.debug("append_task_event gave up after seq collisions (task=%s)", task_id)
    return None


# ---------------------------------------------------------------------------
# Mid-flight steering (`!steer`)
# ---------------------------------------------------------------------------


@dataclass
class Steer:
    id: int
    task_id: int
    seq: int
    text: str
    user_id: str
    source: str
    status: str
    created_at: str
    consumed_at: str | None


def _row_to_steer(row: sqlite3.Row) -> Steer:
    return Steer(
        id=row["id"],
        task_id=row["task_id"],
        seq=row["seq"],
        text=row["text"],
        user_id=row["user_id"],
        source=row["source"],
        status=row["status"],
        created_at=row["created_at"],
        consumed_at=row["consumed_at"],
    )


def add_task_steer(
    conn: sqlite3.Connection, task_id: int, text: str, user_id: str, source: str,
) -> int:
    """Insert a ``pending`` steer for a running task. Returns the new row id.

    ``seq`` is per-task monotonic (``MAX(seq)+1``), computed atomically in the
    INSERT so concurrent steers on the same task can't collide. Commits in its
    own transaction — the write is a cheap, non-blocking control signal, like
    ``!stop``'s ``cancel_requested`` flip.
    """
    row = conn.execute(
        "INSERT INTO task_steers (task_id, seq, text, user_id, source) "
        "VALUES (?, "
        "  (SELECT COALESCE(MAX(seq), 0) + 1 FROM task_steers WHERE task_id = ?), "
        "  ?, ?, ?) "
        "RETURNING id",
        (task_id, task_id, text, user_id, source),
    ).fetchone()
    conn.commit()
    return int(row["id"])


def claim_pending_steers(conn: sqlite3.Connection, task_id: int) -> list[Steer]:
    """Atomically flip a task's ``pending`` steers to ``consumed`` and return them.

    Ordered by ``seq`` (oldest first). The ``UPDATE ... RETURNING`` makes the
    claim atomic, so a re-poll can't double-deliver the same steer. Commits its
    own transaction. Returns ``[]`` when nothing is pending.
    """
    rows = conn.execute(
        "UPDATE task_steers SET status = 'consumed', consumed_at = datetime('now') "
        "WHERE task_id = ? AND status = 'pending' "
        "RETURNING id, task_id, seq, text, user_id, source, status, created_at, consumed_at",
        (task_id,),
    ).fetchall()
    conn.commit()
    steers = [_row_to_steer(r) for r in rows]
    steers.sort(key=lambda s: s.seq)
    return steers


def drop_pending_steers(conn: sqlite3.Connection, task_id: int) -> int:
    """Mark a task's still-``pending`` steers as ``dropped``. Returns the count.

    Called at task finalization so a steer that never drained (task finished /
    suspended before its next boundary) doesn't leak to a later execution and is
    visible in audit as dropped rather than silently deleted.
    """
    cursor = conn.execute(
        "UPDATE task_steers SET status = 'dropped' "
        "WHERE task_id = ? AND status = 'pending'",
        (task_id,),
    )
    conn.commit()
    return cursor.rowcount


def count_pending_steers(conn: sqlite3.Connection, task_id: int) -> int:
    """Number of ``pending`` steers for a task (backs the per-task depth cap)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM task_steers WHERE task_id = ? AND status = 'pending'",
        (task_id,),
    ).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Web chat rooms (web chat surface)
# ---------------------------------------------------------------------------


@dataclass
class WebChatRoom:
    id: int
    user_id: str
    token: str
    name: str
    archived: bool
    created_at: str
    updated_at: str


def _row_to_web_chat_room(row: sqlite3.Row) -> WebChatRoom:
    return WebChatRoom(
        id=row["id"],
        user_id=row["user_id"],
        token=row["token"],
        name=row["name"],
        archived=bool(row["archived"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _new_web_chat_token(user_id: str) -> str:
    """Per-room channel token. The ``web-`` prefix is informational, not a
    security boundary — handlers always derive ``user_id`` from the session."""
    return f"web-{user_id}-{uuid.uuid4().hex[:12]}"


def list_web_chat_rooms(
    conn: sqlite3.Connection, user_id: str, include_archived: bool = False,
) -> list[WebChatRoom]:
    """Rooms for a user, oldest first (creation order)."""
    sql = "SELECT * FROM web_chat_rooms WHERE user_id = ?"
    params: list = [user_id]
    if not include_archived:
        sql += " AND archived = 0"
    sql += " ORDER BY id ASC"
    return [_row_to_web_chat_room(r) for r in conn.execute(sql, params).fetchall()]


def get_web_chat_room(conn: sqlite3.Connection, room_id: int) -> WebChatRoom | None:
    row = conn.execute(
        "SELECT * FROM web_chat_rooms WHERE id = ?", (room_id,)
    ).fetchone()
    return _row_to_web_chat_room(row) if row else None


def get_web_chat_room_by_token(
    conn: sqlite3.Connection, token: str,
) -> WebChatRoom | None:
    """Return *any one* handle for `token`. A token no longer maps to a unique
    handle — a shared Talk room has one handle per participant (ISSUE-134) — so
    this is only safe for reading token-invariant fields (e.g. the room name).
    Callers needing a specific user's handle must scope by `user_id`."""
    row = conn.execute(
        "SELECT * FROM web_chat_rooms WHERE token = ? LIMIT 1", (token,)
    ).fetchone()
    return _row_to_web_chat_room(row) if row else None


def create_web_chat_room(
    conn: sqlite3.Connection, user_id: str, name: str,
) -> WebChatRoom:
    """Create a room with a freshly generated channel token.

    Also registers the room in the unified `rooms` registry (origin=web) with a
    self-referential `web` binding, so newly-created web rooms appear in the
    cross-surface room list without waiting for the one-time migration.
    """
    token = _new_web_chat_token(user_id)
    display = name.strip() or "general"
    row = conn.execute(
        "INSERT INTO web_chat_rooms (user_id, token, name) VALUES (?, ?, ?) "
        "RETURNING *",
        (user_id, token, display),
    ).fetchone()
    register_room(conn, token, user_id, origin="web", name=display)
    add_room_binding(conn, token, "web", token)
    return _row_to_web_chat_room(row)


def update_web_chat_room(
    conn: sqlite3.Connection,
    room_id: int,
    *,
    name: str | None = None,
    archived: bool | None = None,
) -> WebChatRoom | None:
    """Rename and/or (un)archive a room. Returns the updated row, or None if
    the id is unknown."""
    sets: list[str] = []
    params: list = []
    if name is not None:
        sets.append("name = ?")
        params.append(name.strip() or "general")
    if archived is not None:
        sets.append("archived = ?")
        params.append(1 if archived else 0)
    if not sets:
        return get_web_chat_room(conn, room_id)
    sets.append("updated_at = datetime('now')")
    params.append(room_id)
    row = conn.execute(
        f"UPDATE web_chat_rooms SET {', '.join(sets)} WHERE id = ? RETURNING *",
        params,
    ).fetchone()
    return _row_to_web_chat_room(row) if row else None


def ensure_web_chat_handle(
    conn: sqlite3.Connection, user_id: str, token: str, name: str,
) -> WebChatRoom:
    """Ensure a ``web_chat_rooms`` handle row exists for ``user_id`` against an
    existing registry room ``token`` (used as the frontend's integer room id when
    the room originated on another surface, e.g. a Talk room surfaced in web).
    Idempotent on (user_id, token) — a shared Talk room has one handle per
    participant (ISSUE-134). Returns the requesting user's handle."""
    conn.execute(
        "INSERT OR IGNORE INTO web_chat_rooms (user_id, token, name) "
        "VALUES (?, ?, ?)",
        (user_id, token, name.strip() or "room"),
    )
    row = conn.execute(
        "SELECT * FROM web_chat_rooms WHERE user_id = ? AND token = ?",
        (user_id, token),
    ).fetchone()
    assert row is not None
    return _row_to_web_chat_room(row)


def ensure_default_web_chat_room(
    conn: sqlite3.Connection, user_id: str,
) -> WebChatRoom:
    """Guarantee the user has at least one active room. Returns the first
    active room, creating a ``general`` room when none exist."""
    rooms = list_web_chat_rooms(conn, user_id, include_archived=False)
    if rooms:
        return rooms[0]
    return create_web_chat_room(conn, user_id, "general")


@dataclass
class WebChatMessage:
    """An unsolicited (bot-delivered) message in a web chat room.

    Backs the ``web`` delivery surface: alerts, the verbose execution log, and
    any notification routed to ``web`` post one of these into a room. Rendered
    as a standalone ``role`` message merged into the room transcript by time —
    there is no originating user prompt, so it never produces a user bubble.
    """

    id: int
    user_id: str
    token: str
    role: str
    title: str | None
    text: str
    created_at: str


def _row_to_web_chat_message(row: sqlite3.Row) -> WebChatMessage:
    return WebChatMessage(
        id=row["id"],
        user_id=row["user_id"],
        token=row["token"],
        role=row["role"],
        title=row["title"],
        text=row["text"],
        created_at=row["created_at"],
    )


def add_web_chat_message(
    conn: sqlite3.Connection,
    user_id: str,
    token: str,
    text: str,
    *,
    role: str = "system",
    title: str | None = None,
) -> int:
    """Append a bot-delivered message to a web chat room. Returns the new id."""
    row = conn.execute(
        "INSERT INTO web_chat_messages (user_id, token, role, title, text) "
        "VALUES (?, ?, ?, ?, ?) RETURNING id",
        (user_id, token, role, title, text),
    ).fetchone()
    return int(row["id"])


def list_web_chat_messages(
    conn: sqlite3.Connection, token: str, limit: int = 50,
) -> list[WebChatMessage]:
    """The most recent bot-delivered messages for a room, oldest-first."""
    rows = conn.execute(
        "SELECT * FROM web_chat_messages WHERE token = ? ORDER BY id DESC LIMIT ?",
        (token, limit),
    ).fetchall()
    return [_row_to_web_chat_message(r) for r in reversed(rows)]


def count_recent_web_tasks(
    conn: sqlite3.Connection, user_id: str, window_seconds: int,
) -> int:
    """Count this user's web-chat tasks created within the last
    ``window_seconds`` — backs the per-user rate limit (no extra state)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE user_id = ? AND source_type = 'web' "
        "AND created_at > datetime('now', ?)",
        (user_id, f"-{int(window_seconds)} seconds"),
    ).fetchone()
    return int(row[0]) if row else 0


def count_inflight_tasks_for_scheduled_job(
    conn: sqlite3.Connection, scheduled_job_id: int,
) -> int:
    """Count non-terminal tasks already queued for a scheduled job — backs the
    overlap guard in check_scheduled_jobs. A cron that can't keep up (e.g. a
    ``* * * * *`` job behind a wedged single background worker) must not stack a
    new run each tick; that grew a 130+ deep backlog one row/minute in the
    location-alert incident."""
    row = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE scheduled_job_id = ? "
        "AND status IN ('pending', 'locked', 'running', 'pending_confirmation')",
        (scheduled_job_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def count_active_web_tasks(
    conn: sqlite3.Connection, token: str, user_id: str,
) -> int:
    """Count non-terminal tasks targeting a room's token — backs the busy-room
    guard on delete (won't drop a room a worker is still writing against).

    Counts every source_type, not just ``web``: a foreign task routed into the
    room (e.g. an email reply with ``conversation_token`` set to the room token)
    will also write to it via WebTransport.deliver, so deletion must wait on it
    too."""
    row = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE conversation_token = ? AND user_id = ? "
        "AND status IN ('pending', 'locked', 'running', 'pending_confirmation')",
        (token, user_id),
    ).fetchone()
    return int(row[0]) if row else 0


def delete_web_chat_room(
    conn: sqlite3.Connection, room_id: int, user_id: str,
) -> bool:
    """Hard-delete a room and every row keyed on its token, in one transaction.

    Returns ``False`` (deleting nothing) when the room is unknown or owned by
    another user. Removes, in order: the room's tasks' ``task_events``, those
    tasks, its ``web_chat_messages``, its ``channel_sleep_cycle_state``, and the
    room row itself. The ``CHANNEL.md`` directory and channel ``memory_chunks``
    are not touched here — the caller removes the former best-effort; the latter
    is a documented residual.
    """
    room = get_web_chat_room(conn, room_id)
    if room is None or room.user_id != user_id:
        return False
    token = room.token
    conn.execute(
        "DELETE FROM task_events WHERE task_id IN "
        "(SELECT id FROM tasks WHERE conversation_token = ? AND user_id = ?)",
        (token, user_id),
    )
    conn.execute(
        "DELETE FROM tasks WHERE conversation_token = ? AND user_id = ?",
        (token, user_id),
    )
    conn.execute("DELETE FROM web_chat_messages WHERE token = ?", (token,))
    conn.execute(
        "DELETE FROM channel_sleep_cycle_state WHERE conversation_token = ?",
        (token,),
    )
    # Unified-rooms tables — FK cascades are decorative (foreign_keys unset),
    # so hand-delete every row keyed on the token. Stars first: they key on
    # message ids that are about to disappear.
    conn.execute(
        "DELETE FROM message_stars WHERE message_id IN "
        "(SELECT id FROM messages WHERE room_token = ?)",
        (token,),
    )
    conn.execute("DELETE FROM messages WHERE room_token = ?", (token,))
    conn.execute("DELETE FROM room_bindings WHERE room_token = ?", (token,))
    conn.execute("DELETE FROM room_read_state WHERE room_token = ?", (token,))
    conn.execute("DELETE FROM room_members WHERE room_token = ?", (token,))
    conn.execute("DELETE FROM room_dismissals WHERE room_token = ?", (token,))
    conn.execute("DELETE FROM rooms WHERE token = ?", (token,))
    # Drop every participant's handle for the token, not just the requester's
    # (room_id): a promoted web room can accrue handles for other members, and
    # leaving an orphan handle pointing at a now-deleted room would suppress
    # their default-room creation and yield an empty room list (ISSUE-134).
    conn.execute("DELETE FROM web_chat_rooms WHERE token = ?", (token,))
    return True


# ---------------------------------------------------------------------------
# Unified Talk / web room sync — registry, bindings, canonical messages
# ---------------------------------------------------------------------------


@dataclass
class Room:
    """A surface-independent conversation. `token` is the canonical
    conversation_token; `origin` is the surface it was created on."""

    token: str
    user_id: str
    name: str | None
    origin: str
    created_at: str
    archived: bool
    model: str | None = None
    effort: str | None = None


@dataclass
class RoomBinding:
    """Maps a room's canonical token to one surface's native reference."""

    room_token: str
    surface: str
    surface_ref: str
    created_at: str


@dataclass
class Message:
    """One canonical, surface-neutral message in a room transcript."""

    id: int
    room_token: str
    role: str
    body: str
    title: str | None
    task_id: int | None
    origin_surface: str
    external_ids: dict | None
    created_at: str


def _row_to_room(row: sqlite3.Row) -> Room:
    keys = row.keys()
    return Room(
        token=row["token"],
        user_id=row["user_id"],
        name=row["name"],
        origin=row["origin"],
        created_at=row["created_at"],
        archived=bool(row["archived"]),
        # Older DBs mid-migration may lack these columns; default to None.
        model=row["model"] if "model" in keys else None,
        effort=row["effort"] if "effort" in keys else None,
    )


def _row_to_room_binding(row: sqlite3.Row) -> RoomBinding:
    return RoomBinding(
        room_token=row["room_token"],
        surface=row["surface"],
        surface_ref=row["surface_ref"],
        created_at=row["created_at"],
    )


def _row_to_message(row: sqlite3.Row) -> Message:
    raw = row["external_ids"]
    external = json.loads(raw) if raw else None
    return Message(
        id=row["id"],
        room_token=row["room_token"],
        role=row["role"],
        body=row["body"],
        title=row["title"],
        task_id=row["task_id"],
        origin_surface=row["origin_surface"],
        external_ids=external,
        created_at=row["created_at"],
    )


def register_room(
    conn: sqlite3.Connection,
    token: str,
    user_id: str,
    *,
    origin: str,
    name: str | None = None,
) -> Room:
    """Idempotently register a room. If a row already exists for `token` it is
    returned unchanged (name/origin are not overwritten — first writer wins).

    The registering user is recorded as a member either way: `rooms.user_id` is
    only the creator/origin owner, but visibility is resolved through
    `room_members` (ISSUE-134), so a second participant registering against an
    existing room still becomes a member."""
    conn.execute(
        "INSERT OR IGNORE INTO rooms (token, user_id, name, origin) "
        "VALUES (?, ?, ?, ?)",
        (token, user_id, name, origin),
    )
    add_room_member(conn, token, user_id)
    room = get_room(conn, token)
    assert room is not None  # just inserted or already present
    return room


def add_room_member(conn: sqlite3.Connection, room_token: str, user_id: str) -> None:
    """Idempotently record that `user_id` is a participant in `room_token`."""
    conn.execute(
        "INSERT OR IGNORE INTO room_members (room_token, user_id) VALUES (?, ?)",
        (room_token, user_id),
    )


def remove_room_member(conn: sqlite3.Connection, room_token: str, user_id: str) -> None:
    """Drop `user_id`'s membership — the per-user "hide this room" switch. The
    shared room, its transcript, and other members are untouched."""
    conn.execute(
        "DELETE FROM room_members WHERE room_token = ? AND user_id = ?",
        (room_token, user_id),
    )


def is_room_member(conn: sqlite3.Connection, room_token: str, user_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM room_members WHERE room_token = ? AND user_id = ? LIMIT 1",
        (room_token, user_id),
    ).fetchone()
    return row is not None


def dismiss_room(conn: sqlite3.Connection, room_token: str, user_id: str) -> None:
    """Tombstone a room as hidden for `user_id` (the web hide action). Durable
    against the poll-time membership backfill — `list_member_rooms` excludes a
    dismissed room even while the user is still a member. Cleared by
    `undismiss_room` (the user's own next inbound)."""
    conn.execute(
        "INSERT OR IGNORE INTO room_dismissals (room_token, user_id) VALUES (?, ?)",
        (room_token, user_id),
    )


def undismiss_room(conn: sqlite3.Connection, room_token: str, user_id: str) -> None:
    """Clear a hide tombstone — re-engagement un-hides (called from
    `record_inbound` on the sender's own message)."""
    conn.execute(
        "DELETE FROM room_dismissals WHERE room_token = ? AND user_id = ?",
        (room_token, user_id),
    )


def is_room_dismissed(conn: sqlite3.Connection, room_token: str, user_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM room_dismissals WHERE room_token = ? AND user_id = ? LIMIT 1",
        (room_token, user_id),
    ).fetchone()
    return row is not None


def list_room_members(conn: sqlite3.Connection, room_token: str) -> list[str]:
    rows = conn.execute(
        "SELECT user_id FROM room_members WHERE room_token = ? ORDER BY user_id",
        (room_token,),
    ).fetchall()
    return [r["user_id"] for r in rows]


def list_member_rooms(
    conn: sqlite3.Connection, user_id: str, include_archived: bool = False,
) -> list[Room]:
    """Rooms `user_id` is a member of, oldest-first. This is the visibility query
    for the web room list (ISSUE-134) — it replaces the single-owner
    `list_rooms`, so a shared Talk room surfaces for every participant.

    A room the user has hidden (`room_dismissals` tombstone) is excluded even
    while they remain a member — the poll-time backfill re-adds membership, so
    membership alone can't keep a hidden room hidden."""
    sql = (
        "SELECT r.* FROM rooms r "
        "JOIN room_members m ON m.room_token = r.token "
        "WHERE m.user_id = ? "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM room_dismissals d "
        "  WHERE d.room_token = r.token AND d.user_id = m.user_id"
        ")"
    )
    params: list = [user_id]
    if not include_archived:
        sql += " AND r.archived = 0"
    sql += " ORDER BY r.created_at ASC, r.token ASC"
    return [_row_to_room(r) for r in conn.execute(sql, params).fetchall()]


def get_room(conn: sqlite3.Connection, token: str) -> Room | None:
    row = conn.execute("SELECT * FROM rooms WHERE token = ?", (token,)).fetchone()
    return _row_to_room(row) if row else None


def list_rooms(
    conn: sqlite3.Connection, user_id: str, include_archived: bool = False,
) -> list[Room]:
    """Rooms for a user, oldest-first (creation order)."""
    sql = "SELECT * FROM rooms WHERE user_id = ?"
    params: list = [user_id]
    if not include_archived:
        sql += " AND archived = 0"
    sql += " ORDER BY created_at ASC, token ASC"
    return [_row_to_room(r) for r in conn.execute(sql, params).fetchall()]


def set_room_archived(conn: sqlite3.Connection, token: str, archived: bool) -> None:
    conn.execute(
        "UPDATE rooms SET archived = ? WHERE token = ?",
        (1 if archived else 0, token),
    )


def archive_orphaned_talk_rooms(
    conn: sqlite3.Connection, live_tokens: set[str],
) -> int:
    """Archive Talk-origin registry rooms whose token is no longer among the
    bot's live Talk conversations (`live_tokens`) — i.e. the conversation was
    deleted in Nextcloud, or the bot was removed from it. Without this a deleted
    Talk room keeps surfacing in the web room list forever, because its registry
    row is never reconciled against Nextcloud.

    Archive, not hard-delete: a re-add (or a later reconcile) shouldn't destroy
    mirrored history, and this mirrors the web-side delete of a Talk room.
    Web-origin rooms are never touched. Returns the number archived.

    The caller MUST pass a *complete* live-token set from a successful Talk
    `list_conversations` (not a partial/failed fetch) — an empty set here means
    the bot is genuinely in zero Talk rooms and archives all of them."""
    rows = conn.execute(
        "SELECT token FROM rooms WHERE origin = 'talk' AND archived = 0"
    ).fetchall()
    archived = 0
    for row in rows:
        if row["token"] not in live_tokens:
            conn.execute(
                "UPDATE rooms SET archived = 1 WHERE token = ?", (row["token"],)
            )
            archived += 1
    return archived


def rename_room(conn: sqlite3.Connection, token: str, name: str) -> None:
    conn.execute("UPDATE rooms SET name = ? WHERE token = ?", (name, token))


def set_room_model_effort(
    conn: sqlite3.Connection,
    token: str,
    model: str | None,
    effort: str | None,
) -> None:
    """Set the room's standing model + effort as a pair (both canonical values,
    or None to clear). This is the `!room model <alias>` / room-settings write —
    an alias resolves to a (model, effort) pair, so both columns move together."""
    conn.execute(
        "UPDATE rooms SET model = ?, effort = ? WHERE token = ?",
        (model, effort, token),
    )


def set_room_effort(conn: sqlite3.Connection, token: str, effort: str | None) -> None:
    """Set only the room's effort level (None clears it), leaving the model
    default untouched — the `!room effort <level>` convenience knob."""
    conn.execute(
        "UPDATE rooms SET effort = ? WHERE token = ?", (effort, token)
    )


def set_room_model(conn: sqlite3.Connection, token: str, model: str | None) -> None:
    """Set only the room's model default (None clears it), leaving the effort
    level untouched — so `!room model <alias>` and `!room effort <level>` are
    orthogonal knobs. (An effort-bearing alias like `opus-high` still sets both
    via set_room_model_effort — that's the caller's explicit both-pick.)"""
    conn.execute(
        "UPDATE rooms SET model = ? WHERE token = ?", (model, token)
    )


def add_room_binding(
    conn: sqlite3.Connection, room_token: str, surface: str, surface_ref: str,
) -> None:
    """Idempotently bind a room to a surface (PK (room_token, surface))."""
    conn.execute(
        "INSERT OR IGNORE INTO room_bindings (room_token, surface, surface_ref) "
        "VALUES (?, ?, ?)",
        (room_token, surface, surface_ref),
    )


def get_room_binding(
    conn: sqlite3.Connection, room_token: str, surface: str,
) -> RoomBinding | None:
    row = conn.execute(
        "SELECT * FROM room_bindings WHERE room_token = ? AND surface = ?",
        (room_token, surface),
    ).fetchone()
    return _row_to_room_binding(row) if row else None


def list_room_bindings(
    conn: sqlite3.Connection, room_token: str,
) -> list[RoomBinding]:
    rows = conn.execute(
        "SELECT * FROM room_bindings WHERE room_token = ? ORDER BY surface",
        (room_token,),
    ).fetchall()
    return [_row_to_room_binding(r) for r in rows]


def resolve_room_token(
    conn: sqlite3.Connection, surface: str, surface_ref: str,
) -> str | None:
    """Find the canonical room token for a surface's native reference, or None
    if no binding exists (origin-surface case: caller treats surface_ref as the
    canonical token)."""
    row = conn.execute(
        "SELECT room_token FROM room_bindings WHERE surface = ? AND surface_ref = ?",
        (surface, surface_ref),
    ).fetchone()
    return row["room_token"] if row else None


def add_message(
    conn: sqlite3.Connection,
    room_token: str,
    *,
    role: str,
    body: str,
    origin_surface: str,
    title: str | None = None,
    task_id: int | None = None,
    external_ids: dict | None = None,
) -> int:
    """Append a message to a room's canonical transcript. Returns the new id."""
    row = conn.execute(
        "INSERT INTO messages "
        "(room_token, role, body, title, task_id, origin_surface, external_ids) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (
            room_token,
            role,
            body,
            title,
            task_id,
            origin_surface,
            json.dumps(external_ids) if external_ids else None,
        ),
    ).fetchone()
    return int(row["id"])


def get_messages(
    conn: sqlite3.Connection, room_token: str, limit: int | None = None,
) -> list[Message]:
    """A room's messages, oldest-first (by id). With `limit`, returns the most
    recent `limit` messages, still oldest-first."""
    if limit is None:
        rows = conn.execute(
            "SELECT * FROM messages WHERE room_token = ? ORDER BY id ASC",
            (room_token,),
        ).fetchall()
        return [_row_to_message(r) for r in rows]
    rows = conn.execute(
        "SELECT * FROM messages WHERE room_token = ? ORDER BY id DESC LIMIT ?",
        (room_token, limit),
    ).fetchall()
    return [_row_to_message(r) for r in reversed(rows)]


def store_turn_message(
    conn: sqlite3.Connection,
    room_token: str,
    *,
    role: str,
    body: str,
    task_id: int,
    origin_surface: str,
) -> int | None:
    """Idempotently store a turn's user/assistant message. Returns the new id,
    or None if a row for (room_token, role, task_id) already exists — so a retry
    that re-completes a task, or a duplicate inbound poll, won't duplicate it."""
    existing = conn.execute(
        "SELECT id FROM messages WHERE room_token = ? AND task_id = ? "
        "AND role = ? LIMIT 1",
        (room_token, task_id, role),
    ).fetchone()
    if existing:
        return None
    return add_message(
        conn, room_token, role=role, body=body,
        origin_surface=origin_surface, task_id=task_id,
    )


def get_message_room_for_task(conn: sqlite3.Connection, task_id: int) -> str | None:
    """The canonical room token for a task's stored turn, or None if absent.

    A thin read over the durable `messages` store so a conversation search hit
    keeps its room scope after the `tasks` row ages out of retention (the tasks
    table is a display concern; `messages` holds the durable room↔turn mapping).
    Returns the room of the first message carrying this task_id."""
    row = conn.execute(
        "SELECT room_token FROM messages WHERE task_id = ? LIMIT 1",
        (task_id,),
    ).fetchone()
    return row["room_token"] if row else None


def get_turn_message_id(
    conn: sqlite3.Connection,
    room_token: str,
    task_id: int,
    role: str = "assistant",
) -> int | None:
    """The durable `messages.id` of a task's stored turn, or None if absent.

    The star key the web transcript gates on. Recovers the id when
    `store_turn_message` returned None (a retry that re-completed a task —
    the row already existed) and lets the terminal event / synthetic backstop
    tell a freshly-settled turn its id so it becomes starrable without a
    history refetch (ISSUE-172)."""
    row = conn.execute(
        "SELECT id FROM messages WHERE room_token = ? AND task_id = ? "
        "AND role = ? LIMIT 1",
        (room_token, task_id, role),
    ).fetchone()
    return row["id"] if row else None


def list_system_messages(
    conn: sqlite3.Connection, room_token: str, limit: int = 50,
) -> list[Message]:
    """The most recent bot-delivered system messages for a room (role='system',
    task_id NULL) — alerts / verbose log / web-routed notifications. Oldest-first.
    Replaces the legacy web_chat_messages read path."""
    rows = conn.execute(
        "SELECT * FROM messages WHERE room_token = ? AND role = 'system' "
        "ORDER BY id DESC LIMIT ?",
        (room_token, limit),
    ).fetchall()
    return [_row_to_message(r) for r in reversed(rows)]


def list_system_messages_in_band(
    conn: sqlite3.Connection, room_token: str, *, lo_ts: str, hi_ts: str,
) -> list[Message]:
    """System messages within the half-open band ``lo_ts <= created_at < hi_ts``
    (the web-chat older-page path — ISSUE-131). Oldest-first. ``lo_ts`` / ``hi_ts``
    are *raw* stored `created_at` strings (`YYYY-MM-DD HH:MM:SS`), the same format
    the keyset cursor travels in — not the `_iso_utc` display value."""
    rows = conn.execute(
        "SELECT * FROM messages WHERE room_token = ? AND role = 'system' "
        "AND created_at >= ? AND created_at < ? ORDER BY id DESC",
        (room_token, lo_ts, hi_ts),
    ).fetchall()
    return [_row_to_message(r) for r in reversed(rows)]


def set_message_external_id(
    conn: sqlite3.Connection, message_id: int, surface: str, external_id: str,
) -> None:
    """Record where a message has been materialized on a surface (the
    loop-prevention ledger). Merges into the existing JSON map."""
    row = conn.execute(
        "SELECT external_ids FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None:
        return
    current = json.loads(row["external_ids"]) if row["external_ids"] else {}
    current[surface] = external_id
    conn.execute(
        "UPDATE messages SET external_ids = ? WHERE id = ?",
        (json.dumps(current), message_id),
    )


def user_turn_has_external_id(
    conn: sqlite3.Connection, task_id: int, surface: str,
) -> bool:
    """True when a task's user turn already carries an external id on
    `surface` — the scheduler's signal that the web process already posted the
    turn as the user (post-as-user mirroring), so the legacy attributed repost
    must be suppressed. A pure framework-DB read; the scheduler never touches
    the token itself."""
    row = conn.execute(
        "SELECT external_ids FROM messages "
        "WHERE task_id = ? AND role = 'user' LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None or not row["external_ids"]:
        return False
    try:
        ext = json.loads(row["external_ids"])
    except (ValueError, TypeError):
        return False
    return isinstance(ext, dict) and surface in ext


def room_max_talk_synced_message_id(
    conn: sqlite3.Connection, room_token: str,
) -> int:
    """The highest `messages.id` in a room that has a `"talk"` external id —
    i.e. the newest canonical message that demonstrably exists in Talk. The
    Talk→web read-sync cursor is capped here so Talk read-state can't swallow
    web-only rows (WebTransport system messages) the user never saw in Talk.
    0 when nothing is stamped yet (sync starts working for post-deploy rows)."""
    rows = conn.execute(
        "SELECT id, external_ids FROM messages "
        "WHERE room_token = ? AND external_ids IS NOT NULL ORDER BY id DESC",
        (room_token,),
    ).fetchall()
    for r in rows:
        try:
            ext = json.loads(r["external_ids"])
        except (ValueError, TypeError):
            continue
        if isinstance(ext, dict) and "talk" in ext:
            return int(r["id"])
    return 0


def message_has_external_id(
    conn: sqlite3.Connection,
    room_token: str,
    surface: str,
    external_id: str,
    *,
    exclude_origin: str | None = None,
) -> bool:
    """True if any message in the room already records `external_id` on
    `surface` — used by inbound echo detection.

    ``exclude_origin`` skips rows whose `origin_surface` matches: a row that
    originated on the inbound surface itself isn't a mirror echo — it's the
    same message re-polled (inbound Talk ids are stamped at ingest now), and
    that case must fall through to `create_task`'s duplicate dedup so the
    caller gets the existing task id instead of an echo drop."""
    rows = conn.execute(
        "SELECT origin_surface, external_ids FROM messages "
        "WHERE room_token = ? AND external_ids IS NOT NULL",
        (room_token,),
    ).fetchall()
    for r in rows:
        if exclude_origin is not None and r["origin_surface"] == exclude_origin:
            continue
        try:
            ext = json.loads(r["external_ids"])
        except (ValueError, TypeError):
            continue
        if isinstance(ext, dict) and ext.get(surface) == external_id:
            return True
    return False


def get_room_read_state(
    conn: sqlite3.Connection, room_token: str, surface: str, user_id: str = "",
) -> int:
    row = conn.execute(
        "SELECT last_read_message_id FROM room_read_state "
        "WHERE room_token = ? AND surface = ? AND user_id = ?",
        (room_token, surface, user_id),
    ).fetchone()
    return int(row["last_read_message_id"]) if row else 0


def set_room_read_state(
    conn: sqlite3.Connection,
    room_token: str,
    surface: str,
    last_read_message_id: int,
    user_id: str = "",
) -> None:
    conn.execute(
        "INSERT INTO room_read_state "
        "(room_token, surface, user_id, last_read_message_id) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT (room_token, surface, user_id) DO UPDATE SET "
        "last_read_message_id = excluded.last_read_message_id",
        (room_token, surface, user_id, last_read_message_id),
    )


def room_max_message_id(conn: sqlite3.Connection, room_token: str) -> int:
    """The highest `messages.id` in a room, or 0 when the room is empty. Used to
    seed / advance a read cursor to "everything so far"."""
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS m FROM messages WHERE room_token = ?",
        (room_token,),
    ).fetchone()
    return int(row["m"])


def count_unread_messages(
    conn: sqlite3.Connection, room_token: str, surface: str, user_id: str = "",
) -> int:
    """Number of unread bot/system messages in a room for a user on a surface:
    messages past the surface's read cursor, excluding the user's own turns
    (`role = 'user'`) so a user's input — including Talk turns mirrored into the
    canonical store — never rings their own room as unread."""
    cursor = get_room_read_state(conn, room_token, surface, user_id)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM messages "
        "WHERE room_token = ? AND id > ? AND role != 'user'",
        (room_token, cursor),
    ).fetchone()
    return int(row["n"])


def initialize_room_read_state(
    conn: sqlite3.Connection, room_token: str, surface: str, user_id: str = "",
) -> bool:
    """Seed a read cursor for a room the first time it's surfaced on a surface.

    When no `room_read_state` row yet exists for `(room_token, surface,
    user_id)`, insert one at the room's current `MAX(messages.id)` so a
    pre-existing backlog (e.g. a Talk room newly mirrored into web) reads as
    already-seen instead of flooding the unread indicator. Returns True if it
    seeded a row, False if one already existed (left untouched)."""
    existing = conn.execute(
        "SELECT 1 FROM room_read_state "
        "WHERE room_token = ? AND surface = ? AND user_id = ?",
        (room_token, surface, user_id),
    ).fetchone()
    if existing:
        return False
    conn.execute(
        "INSERT INTO room_read_state "
        "(room_token, surface, user_id, last_read_message_id) VALUES (?, ?, ?, ?)",
        (room_token, surface, user_id, room_max_message_id(conn, room_token)),
    )
    return True


# ---------------------------------------------------------------------------
# Per-message stars + cross-room aggregate views (web chat)
# ---------------------------------------------------------------------------

# Which `messages` rows render as transcript turns. Shared by the per-room
# spine query (web_app._SPINE_SURFACE) and the cross-room aggregate below.
#
# Storage is the real filter now: an assistant row only exists in a room
# because a task delivered a result into that web-visible room (via
# scheduler._store_room_turn, for ANY source type — subtask/scheduled/briefing/
# heartbeat/talk/web), so "render every assistant row" is exactly right. The
# origin_surface guard is retained only on role='user' rows, whose sole job was
# to hide the synthetic prompt of a non-conversational post — and since the
# producer never writes a user row for those, this guard is belt-and-suspenders
# against any future code that does. Expects the messages table aliased as `m`.
TRANSCRIPT_SURFACE_FILTER = (
    "(m.role = 'assistant' "
    "OR (m.origin_surface IN ('web', 'talk') AND m.role = 'user'))"
)


def set_message_starred(
    conn: sqlite3.Connection, message_id: int, user_id: str, starred: bool,
) -> bool:
    """Star/unstar a durable message for one user. Idempotent both ways.
    Returns False only when the message id doesn't exist (the star state then
    matches the request by definition of there being nothing to star)."""
    exists = conn.execute(
        "SELECT 1 FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if exists is None:
        return False
    if starred:
        conn.execute(
            "INSERT OR IGNORE INTO message_stars (message_id, user_id) "
            "VALUES (?, ?)",
            (message_id, user_id),
        )
    else:
        conn.execute(
            "DELETE FROM message_stars WHERE message_id = ? AND user_id = ?",
            (message_id, user_id),
        )
    return True


def get_message_room(conn: sqlite3.Connection, message_id: int) -> str | None:
    """The room token a message belongs to (for server-side membership checks
    on the star endpoint), or None for an unknown id."""
    row = conn.execute(
        "SELECT room_token FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    return row["room_token"] if row else None


def get_starred_message_ids(
    conn: sqlite3.Connection, user_id: str, message_ids: list[int],
) -> set[int]:
    """The subset of `message_ids` this user has starred."""
    ids = [int(i) for i in message_ids]
    if not ids:
        return set()
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT message_id FROM message_stars "
        f"WHERE user_id = ? AND message_id IN ({placeholders})",
        [user_id, *ids],
    ).fetchall()
    return {int(r["message_id"]) for r in rows}


def list_messages_across_rooms(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    view: str = "all",
    limit: int = 50,
    before_ts: str | None = None,
    before_id: int | None = None,
) -> list[sqlite3.Row]:
    """One page of the cross-room message stream for the All / Unread / Starred
    web views, newest-first, keyset-paginated on ``(created_at, id)``.

    Reads the durable `messages` store only (no `tasks` gap-fill, no in-flight
    placeholders — a cross-room reading surface doesn't need the live-room aux
    merge). Visibility = membership minus dismissals minus archived rooms,
    matching `list_member_rooms`. Rows carry the room token/name, the LEFT JOIN
    `tasks` enrichment columns the per-room spine also selects, and a `starred`
    flag for the requesting user.

    Views:
    - ``all``: every transcript-rendered row, own turns included.
    - ``unread``: rows past the user's per-room web read cursor, excluding
      their own turns — the same math as `count_unread_messages`, so the view
      and the sidebar badges always agree. A room with no cursor row yet
      contributes everything (COALESCE to 0); in practice the rooms listing
      seeds cursors on every load.
    - ``starred``: rows the user has starred, still in transcript order.
    """
    if view not in ("all", "unread", "starred"):
        raise ValueError(f"unknown view: {view!r}")
    # System rows (alerts / logs / web-routed notifications) render in the
    # aggregate views too — count_unread_messages counts them, so Unread must
    # show them.
    surface = f"({TRANSCRIPT_SURFACE_FILTER} OR m.role = 'system')"
    sql = (
        "SELECT m.role AS role, m.body AS body, m.title AS title, "
        "  m.task_id AS task_id, m.id AS msg_id, m.created_at AS created_at, "
        "  m.room_token AS room_token, r.name AS room_name, "
        "  t.status AS status, t.actions_taken AS actions_taken, "
        "  t.execution_trace AS execution_trace, t.started_at AS started_at, "
        "  t.completed_at AS completed_at, t.model_used AS model_used, "
        "  (s.message_id IS NOT NULL) AS starred "
        "FROM messages m "
        "JOIN rooms r ON r.token = m.room_token AND r.archived = 0 "
        "JOIN room_members mm ON mm.room_token = m.room_token "
        "  AND mm.user_id = :user "
        "LEFT JOIN message_stars s ON s.message_id = m.id "
        "  AND s.user_id = :user "
        "LEFT JOIN tasks t ON t.id = m.task_id "
    )
    if view == "unread":
        sql += (
            "LEFT JOIN room_read_state rs ON rs.room_token = m.room_token "
            "  AND rs.surface = 'web' AND rs.user_id = :user "
        )
    sql += (
        "WHERE NOT EXISTS (SELECT 1 FROM room_dismissals d "
        "  WHERE d.room_token = m.room_token AND d.user_id = :user) "
        f"AND {surface} "
    )
    if view == "unread":
        sql += (
            "AND m.role != 'user' "
            "AND m.id > COALESCE(rs.last_read_message_id, 0) "
        )
    elif view == "starred":
        sql += "AND s.message_id IS NOT NULL "
    if before_ts is not None:
        sql += "AND (m.created_at, m.id) < (:before_ts, :before_id) "
    sql += "ORDER BY m.created_at DESC, m.id DESC LIMIT :limit"
    return conn.execute(sql, {
        "user": user_id,
        "limit": limit,
        "before_ts": before_ts,
        "before_id": before_id,
    }).fetchall()


def mark_all_rooms_read(conn: sqlite3.Connection, user_id: str) -> int:
    """Advance the user's web read cursor to the newest message in every room
    they can see (same visibility as `list_member_rooms`). Returns the number
    of rooms whose cursor actually moved."""
    return len(mark_all_rooms_read_tokens(conn, user_id))


def mark_all_rooms_read_tokens(conn: sqlite3.Connection, user_id: str) -> list[str]:
    """Same as `mark_all_rooms_read`, returning the tokens of the rooms whose
    cursor actually moved — the web→Talk read-sync push needs the identities,
    not just the count (only actually-advanced rooms get an NC call)."""
    moved: list[str] = []
    for room in list_member_rooms(conn, user_id):
        max_id = room_max_message_id(conn, room.token)
        if max_id > get_room_read_state(conn, room.token, "web", user_id):
            set_room_read_state(conn, room.token, "web", max_id, user_id)
            moved.append(room.token)
    return moved


def _migrate_unified_rooms(conn: sqlite3.Connection) -> None:
    """One-time fold of legacy stores into the unified room model.

    Markered (`unified_rooms_v1`) so the heavier backfills (web_chat_messages
    copy, distinct-Talk-token scan over `tasks`) run once. Each step is also
    structurally idempotent (INSERT OR IGNORE / marker), so a re-run before the
    marker is set is harmless. No-op on fresh installs (legacy tables empty or
    not yet created — wrapped in try/except)."""
    try:
        already = conn.execute(
            "SELECT 1 FROM _migration_state WHERE name = 'unified_rooms_v1'"
        ).fetchone()
    except sqlite3.OperationalError:
        return  # marker table not created yet (very early fresh install)
    if already:
        return

    def _step(fn) -> bool:
        """Run one backfill step. A genuinely-absent legacy table ("no such
        table") is the fresh-install path and is benign — skip it and keep
        going. Any other OperationalError (disk I/O, locked, constraint) is a
        real mid-backfill failure: log it and signal abort so the completion
        marker is *not* written and the next boot retries the whole fold. Every
        step is structurally idempotent (INSERT OR IGNORE / NOT EXISTS), so a
        partial first run replays cleanly."""
        try:
            fn()
            return True
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                return True  # legacy table absent -> nothing to fold, not a failure
            logger.warning("unified_rooms migration step failed, will retry: %s", e)
            return False

    def _fold_web_rooms():
        # web_chat_rooms -> rooms (origin=web) + self-referential web binding.
        conn.execute(
            "INSERT OR IGNORE INTO rooms (token, user_id, name, origin, archived) "
            "SELECT token, user_id, name, 'web', archived FROM web_chat_rooms"
        )
        conn.execute(
            "INSERT OR IGNORE INTO room_bindings (room_token, surface, surface_ref) "
            "SELECT token, 'web', token FROM web_chat_rooms"
        )

    def _fold_talk_rooms():
        # Distinct Talk conversation_tokens -> rooms (origin=talk) + talk binding.
        # Only interactive Talk tasks; scheduled/briefing/etc tokens aren't rooms.
        conn.execute(
            "INSERT OR IGNORE INTO rooms (token, user_id, name, origin) "
            "SELECT conversation_token, user_id, NULL, 'talk' FROM tasks "
            "WHERE source_type = 'talk' AND conversation_token IS NOT NULL "
            "GROUP BY conversation_token"
        )
        conn.execute(
            "INSERT OR IGNORE INTO room_bindings (room_token, surface, surface_ref) "
            "SELECT token, 'talk', token FROM rooms WHERE origin = 'talk'"
        )

    def _fold_web_messages():
        # web_chat_messages -> messages (role=system, task_id NULL). Guarded so
        # this one-time copy isn't duplicated on the rare pre-marker re-run.
        conn.execute(
            "INSERT INTO messages "
            "(room_token, role, body, title, task_id, origin_surface, created_at) "
            "SELECT w.token, w.role, w.text, w.title, NULL, 'web', w.created_at "
            "FROM web_chat_messages w "
            "WHERE w.token IN (SELECT token FROM rooms) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM messages m "
            "  WHERE m.room_token = w.token AND m.origin_surface = 'web' "
            "    AND m.task_id IS NULL AND m.body = w.text "
            "    AND IFNULL(m.title,'') = IFNULL(w.title,'') "
            "    AND m.created_at = w.created_at"
            ")"
        )

    def _backfill_turns():
        # Backfill the canonical messages store (user+assistant turns) from
        # completed tasks for every registered room, so the unified history
        # reader has the historical backlog. Live writes (Stage 3/4) keep it
        # current going forward. Scheduled-job turns are normalized (assistant
        # post only, NO_ACTION ticks omitted) — see `_backfill_turns_for`.
        _backfill_turns_for(
            conn, "conversation_token IN (SELECT token FROM rooms)", (),
        )

    ok = True
    for step in (_fold_web_rooms, _fold_talk_rooms, _fold_web_messages, _backfill_turns):
        if not _step(step):
            ok = False
            break

    # Only mark the fold complete if every step succeeded. A swallowed
    # mid-backfill failure used to set the marker anyway, stranding a partially
    # populated `messages` store that never retried.
    if ok:
        conn.execute(
            "INSERT OR IGNORE INTO _migration_state (name) VALUES ('unified_rooms_v1')"
        )


def _migrate_web_chat_rooms_peruser(conn: sqlite3.Connection) -> None:
    """Rebuild `web_chat_rooms` so `token` is unique per (user, token) rather
    than globally (ISSUE-134), letting every participant of a shared Talk room
    hold their own handle. Self-guarding: inspects the live table DDL and only
    rebuilds the legacy single-token-UNIQUE shape, so it's a no-op on fresh
    installs (already composite) and on re-runs."""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'web_chat_rooms'"
        ).fetchone()
    except sqlite3.OperationalError:
        return
    sql = row[0] if row and row[0] else ""
    if not sql or "UNIQUE (user_id, token)" in sql:
        return  # already migrated (or fresh install with the new DDL)
    try:
        conn.execute("ALTER TABLE web_chat_rooms RENAME TO _web_chat_rooms_old")
        conn.execute("""
            CREATE TABLE web_chat_rooms (
                id          INTEGER PRIMARY KEY,
                user_id     TEXT NOT NULL,
                token       TEXT NOT NULL,
                name        TEXT NOT NULL,
                archived    INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (user_id, token)
            )
        """)
        # Preserve ids so any in-flight frontend room id stays valid.
        conn.execute(
            "INSERT INTO web_chat_rooms "
            "(id, user_id, token, name, archived, created_at, updated_at) "
            "SELECT id, user_id, token, name, archived, created_at, updated_at "
            "FROM _web_chat_rooms_old"
        )
        conn.execute("DROP TABLE _web_chat_rooms_old")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_chat_rooms_user "
            "ON web_chat_rooms (user_id, archived, id)"
        )
    except sqlite3.OperationalError as e:
        logger.warning("web_chat_rooms per-user rebuild failed: %s", e)


def _migrate_room_read_state_peruser(conn: sqlite3.Connection) -> None:
    """Add `user_id` to `room_read_state`'s key (ISSUE-134) so an unread cursor
    is per participant. Read cursors are ephemeral and there are no readers yet,
    so the legacy table is dropped and recreated rather than backfilled.
    Self-guarding on the DDL; no-op once `user_id` is present."""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'room_read_state'"
        ).fetchone()
    except sqlite3.OperationalError:
        return
    sql = row[0] if row and row[0] else ""
    if not sql or "user_id" in sql:
        return
    try:
        conn.execute("DROP TABLE room_read_state")
        conn.execute("""
            CREATE TABLE room_read_state (
                room_token  TEXT NOT NULL REFERENCES rooms(token) ON DELETE CASCADE,
                surface     TEXT NOT NULL,
                user_id     TEXT NOT NULL DEFAULT '',
                last_read_message_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (room_token, surface, user_id)
            )
        """)
    except sqlite3.OperationalError as e:
        logger.warning("room_read_state per-user rebuild failed: %s", e)


def _migrate_room_members(conn: sqlite3.Connection) -> None:
    """Backfill `room_members` for existing deploys (ISSUE-134). Folds in every
    participant of every registered room so a shared Talk room surfaces for all
    of them, not just the arbitrary `rooms.user_id` the unified-rooms fold picked.

    Markered (`room_members_v1`); each insert is `OR IGNORE` so a pre-marker
    re-run is harmless. Sources: the registry owner, every web handle's user, and
    every distinct Talk-task sender for a token that is a room."""
    try:
        already = conn.execute(
            "SELECT 1 FROM _migration_state WHERE name = 'room_members_v1'"
        ).fetchone()
    except sqlite3.OperationalError:
        return  # marker table not created yet (very early fresh install)
    if already:
        return

    # `rooms` / `web_chat_rooms` always exist by now (created in init_db's CREATE
    # block before migrations run), so a failure on those is a real error and
    # must NOT mark the migration done. `tasks`, however, is created by schema.sql
    # which runs *after* migrations on a fresh install — its absence is the
    # benign fresh-install case (nothing to backfill), tolerated like
    # `_migrate_unified_rooms._step` does. Mirrors that per-step contract.
    try:
        # The registry owner is always a member.
        conn.execute(
            "INSERT OR IGNORE INTO room_members (room_token, user_id) "
            "SELECT token, user_id FROM rooms"
        )
        # Every web handle's owner (covers web-origin rooms + any prior handle).
        conn.execute(
            "INSERT OR IGNORE INTO room_members (room_token, user_id) "
            "SELECT token, user_id FROM web_chat_rooms "
            "WHERE token IN (SELECT token FROM rooms)"
        )
    except sqlite3.OperationalError as e:
        logger.warning("room_members backfill failed, will retry: %s", e)
        return  # leave the marker unset so the next boot retries

    try:
        # Every distinct human who sent an interactive Talk turn into the room.
        conn.execute(
            "INSERT OR IGNORE INTO room_members (room_token, user_id) "
            "SELECT conversation_token, user_id FROM tasks "
            "WHERE source_type = 'talk' AND conversation_token IS NOT NULL "
            "AND conversation_token IN (SELECT token FROM rooms) "
            "GROUP BY conversation_token, user_id"
        )
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e).lower():
            logger.warning("room_members talk backfill failed, will retry: %s", e)
            return
        # `tasks` absent → fresh install, nothing to fold; fall through and mark.

    conn.execute(
        "INSERT OR IGNORE INTO _migration_state (name) VALUES ('room_members_v1')"
    )


def _migrate_scheduled_transcript_cleanup(conn: sqlite3.Connection) -> None:
    """Repair scheduled-job rows the earlier blanket backfill folded into the
    canonical `messages` store verbatim (ISSUE-133 follow-up).

    `_migrate_unified_rooms._backfill_turns` originally copied every completed
    task's raw `result` into `messages`, so silent location/monitor crons left a
    trail of literal `NO_ACTION:` and `ACTION: …`-prefixed assistant rows plus
    empty synthetic-prompt user rows — all of which the web transcript reader
    renders (it shows `origin_surface='scheduled'` assistant posts). This brings
    the historical rows in line with what was actually delivered:

      * drop scheduled `user` rows (the cron prompt was never user-authored),
      * drop scheduled `NO_ACTION:` assistant rows (never posted anywhere),
      * strip the `ACTION:` prefix from the rest.

    Markered (`scheduled_transcript_cleanup_v1`); idempotent regardless. No-op on
    fresh installs (nothing matches)."""
    try:
        already = conn.execute(
            "SELECT 1 FROM _migration_state WHERE name = 'scheduled_transcript_cleanup_v1'"
        ).fetchone()
    except sqlite3.OperationalError:
        return  # marker table not created yet (very early fresh install)
    if already:
        return
    try:
        conn.execute(
            "DELETE FROM messages WHERE origin_surface = 'scheduled' AND role = 'user'"
        )
        conn.execute(
            "DELETE FROM messages WHERE origin_surface = 'scheduled' "
            "AND role = 'assistant' AND body LIKE 'NO_ACTION:%'"
        )
        conn.execute(
            "UPDATE messages SET body = TRIM(SUBSTR(body, 8)) "
            "WHERE origin_surface = 'scheduled' AND role = 'assistant' "
            "AND body LIKE 'ACTION:%'"
        )
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return  # fresh install, messages not created yet
        logger.warning("scheduled transcript cleanup failed: %s", e)
        return
    conn.execute(
        "INSERT OR IGNORE INTO _migration_state (name) "
        "VALUES ('scheduled_transcript_cleanup_v1')"
    )


def _migrate_nonconversational_transcript_cleanup(conn: sqlite3.Connection) -> None:
    """Normalize the non-conversational rows the `unified_rooms_v1` blanket
    backfill folded into the canonical `messages` store (ISSUE-176).

    `_migrate_unified_rooms._backfill_turns` ran with no source-type filter, so
    it inserted a `user` row (raw synthetic prompt) and an `assistant` row (raw
    `result`) for every completed task of every registered room — including
    `subtask` / `briefing` / etc., not just conversational turns. When the
    generalized `TRANSCRIPT_SURFACE_FILTER` (assistant-any) goes live, those
    hidden rows would surface at read time: briefing assistant rows as raw
    `{"subject":…,"body":…}` JSON, and the synthetic user rows would re-pair
    into LLM context (breaking the "user rows conversational-only" invariant).

    This one-shot generalizes `_migrate_scheduled_transcript_cleanup` from
    scheduled-only to every non-conversational source type. Over rows whose
    `origin_surface NOT IN ('web','talk','scheduled')` (scheduled is owned by
    its own marker, conversational surfaces are real turns), and never touching
    `role='system'` (the notification/log lane), it:

      * drops the `role='user'` rows — synthetic prompts, never user-authored;
        this is what restores the invariant for every reader without touching
        the context builders,
      * repairs `role='briefing'` assistant bodies that are stored JSON to the
        delivered body (the same parse live delivery does), leaving anything
        that doesn't parse — and every other source type's verbatim body — as-is
        (a plain-text subtask block is already exactly what was delivered).

    Markered (`nonconversational_transcript_cleanup_v1`); idempotent regardless.
    No-op on fresh installs (nothing matches). Must ship with the filter flip —
    it is what makes the read-time reveal clean."""
    try:
        already = conn.execute(
            "SELECT 1 FROM _migration_state "
            "WHERE name = 'nonconversational_transcript_cleanup_v1'"
        ).fetchone()
    except sqlite3.OperationalError:
        return  # marker table not created yet (very early fresh install)
    if already:
        return
    try:
        # Resolve the briefing parser BEFORE any mutation: init_db commits this
        # migration in one transaction, so a mid-migration import failure after
        # the DELETE would commit a half-applied state (user rows dropped) with
        # no marker, leaving briefing rows to reveal as raw JSON until a later
        # deploy re-runs it. Importing first means a failure aborts cleanly with
        # zero mutation, and the unmarked retry re-applies the whole thing.
        from .skills.briefing import parse_briefing_json

        # Drop synthetic non-conversational user rows (restore the invariant).
        conn.execute(
            "DELETE FROM messages "
            "WHERE role = 'user' "
            "AND origin_surface NOT IN ('web', 'talk', 'scheduled')"
        )
        # Normalize briefing assistant bodies that were stored as raw JSON.
        briefing_rows = conn.execute(
            "SELECT id, body FROM messages "
            "WHERE role = 'assistant' AND origin_surface = 'briefing'"
        ).fetchall()
        for row in briefing_rows:
            parsed = parse_briefing_json(row["body"] or "")
            if parsed and parsed.get("body") is not None:
                conn.execute(
                    "UPDATE messages SET body = ? WHERE id = ?",
                    (parsed["body"], row["id"]),
                )
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return  # fresh install, messages not created yet
        logger.warning("nonconversational transcript cleanup failed: %s", e)
        return
    except Exception as e:  # briefing parser import / edge — don't wedge init
        logger.warning("nonconversational transcript cleanup failed: %s", e)
        return
    conn.execute(
        "INSERT OR IGNORE INTO _migration_state (name) "
        "VALUES ('nonconversational_transcript_cleanup_v1')"
    )


def list_tasks(
    conn: sqlite3.Connection,
    status: str | None = None,
    user_id: str | None = None,
    limit: int = 50,
) -> list[Task]:
    """List tasks with optional filters."""
    query = "SELECT * FROM tasks WHERE 1=1"
    params: list = []

    if status:
        query += " AND status = ?"
        params.append(status)
    if user_id:
        query += " AND user_id = ?"
        params.append(user_id)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    cursor = conn.execute(query, params)
    return [_row_to_task(row) for row in cursor.fetchall()]


def is_email_processed(conn: sqlite3.Connection, email_id: str) -> bool:
    """Check if an email has already been processed."""
    cursor = conn.execute(
        "SELECT 1 FROM processed_emails WHERE email_id = ?",
        (email_id,),
    )
    return cursor.fetchone() is not None


def mark_email_processed(
    conn: sqlite3.Connection,
    email_id: str,
    sender_email: str,
    subject: str | None = None,
    thread_id: str | None = None,
    message_id: str | None = None,
    references: str | None = None,
    user_id: str | None = None,
    task_id: int | None = None,
    routing_method: str | None = None,
) -> int:
    """Record a processed email."""
    cursor = conn.execute(
        """
        INSERT INTO processed_emails (email_id, sender_email, subject, thread_id, message_id, "references", user_id, task_id, routing_method)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (email_id, sender_email, subject, thread_id, message_id, references, user_id, task_id, routing_method),
    )
    return cursor.fetchone()[0]


def get_email_for_task(conn: sqlite3.Connection, task_id: int) -> ProcessedEmail | None:
    """Get the original email info for a task."""
    cursor = conn.execute(
        """
        SELECT id, email_id, sender_email, subject, thread_id, message_id, "references", user_id, task_id, processed_at, routing_method
        FROM processed_emails
        WHERE task_id = ?
        """,
        (task_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return ProcessedEmail(
        id=row["id"],
        email_id=row["email_id"],
        sender_email=row["sender_email"],
        subject=row["subject"],
        thread_id=row["thread_id"],
        message_id=row["message_id"],
        references=row["references"],
        user_id=row["user_id"],
        task_id=row["task_id"],
        processed_at=row["processed_at"],
        routing_method=row["routing_method"],
    )


# ============================================================================
# Sent email tracking (outbound emails for emissary thread matching)
# ============================================================================


def record_sent_email(
    conn: sqlite3.Connection,
    user_id: str,
    message_id: str,
    to_addr: str,
    subject: str | None = None,
    task_id: int | None = None,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    conversation_token: str | None = None,
    talk_delivery_token: str | None = None,
    origin_target: str | None = None,
) -> int:
    """Record an outbound email for thread matching."""
    cursor = conn.execute(
        """
        INSERT INTO sent_emails
            (user_id, task_id, message_id, to_addr, subject, thread_id,
             in_reply_to, "references", conversation_token, talk_delivery_token,
             origin_target)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (user_id, task_id, message_id, to_addr, subject, thread_id,
         in_reply_to, references, conversation_token, talk_delivery_token,
         origin_target),
    )
    return cursor.fetchone()[0]


def find_sent_email_by_message_id(
    conn: sqlite3.Connection,
    message_id: str,
) -> SentEmail | None:
    """Look up a sent email by its Message-ID (for In-Reply-To matching)."""
    cursor = conn.execute(
        """
        SELECT id, user_id, task_id, message_id, to_addr, subject, thread_id,
               in_reply_to, "references", conversation_token, sent_at,
               talk_delivery_token, origin_target
        FROM sent_emails
        WHERE message_id = ?
        """,
        (message_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return SentEmail(
        id=row["id"],
        user_id=row["user_id"],
        task_id=row["task_id"],
        message_id=row["message_id"],
        to_addr=row["to_addr"],
        subject=row["subject"],
        thread_id=row["thread_id"],
        in_reply_to=row["in_reply_to"],
        references=row["references"],
        conversation_token=row["conversation_token"],
        sent_at=row["sent_at"],
        talk_delivery_token=row["talk_delivery_token"] if "talk_delivery_token" in row.keys() else None,
        origin_target=row["origin_target"] if "origin_target" in row.keys() else None,
    )


def find_sent_email_by_references(
    conn: sqlite3.Connection,
    references: list[str],
) -> SentEmail | None:
    """Find a sent email matching any of the given Message-IDs.

    Used to match inbound emails whose References header contains one of our
    sent Message-IDs. Returns the most recent match.
    """
    if not references:
        return None
    placeholders = ", ".join("?" for _ in references)
    cursor = conn.execute(
        f"""
        SELECT id, user_id, task_id, message_id, to_addr, subject, thread_id,
               in_reply_to, "references", conversation_token, sent_at,
               talk_delivery_token, origin_target
        FROM sent_emails
        WHERE message_id IN ({placeholders})
        ORDER BY sent_at DESC
        LIMIT 1
        """,
        references,
    )
    row = cursor.fetchone()
    if not row:
        return None
    return SentEmail(
        id=row["id"],
        user_id=row["user_id"],
        task_id=row["task_id"],
        message_id=row["message_id"],
        to_addr=row["to_addr"],
        subject=row["subject"],
        thread_id=row["thread_id"],
        in_reply_to=row["in_reply_to"],
        references=row["references"],
        conversation_token=row["conversation_token"],
        sent_at=row["sent_at"],
        talk_delivery_token=row["talk_delivery_token"] if "talk_delivery_token" in row.keys() else None,
        origin_target=row["origin_target"] if "origin_target" in row.keys() else None,
    )


# ============================================================================
# Google OAuth token functions
# ============================================================================


def get_google_token(conn: sqlite3.Connection, user_id: str) -> dict | None:
    """Get Google OAuth tokens for a user.

    access_token and refresh_token are Fernet-decrypted via $ISTOTA_SECRET_KEY.
    Returns None if the row is missing, the secret key is unavailable, or the
    stored ciphertext fails to decrypt (treated as a corrupt/rotated-key row;
    the user has to re-connect Google).
    """
    cursor = conn.execute(
        "SELECT access_token, refresh_token, token_expiry, scopes FROM google_oauth_tokens WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None

    from istota import secrets_store

    if not secrets_store.secret_key_available():
        logger.warning(
            "google_oauth: cannot decrypt tokens for user=%s (ISTOTA_SECRET_KEY missing)",
            user_id,
        )
        return None

    try:
        fernet = secrets_store._get_fernet()
        access_token = fernet.decrypt(_as_bytes(row["access_token"])).decode("utf-8")
        refresh_token = fernet.decrypt(_as_bytes(row["refresh_token"])).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "google_oauth: decrypt failed user=%s (stale ISTOTA_SECRET_KEY?): %s",
            user_id, exc,
        )
        return None

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_expiry": row["token_expiry"],
        "scopes": row["scopes"],
    }


def upsert_google_token(
    conn: sqlite3.Connection,
    user_id: str,
    access_token: str,
    refresh_token: str,
    token_expiry: str,
    scopes: str = "[]",
) -> None:
    """Insert or update Google OAuth tokens for a user.

    access_token and refresh_token are Fernet-encrypted at rest via
    $ISTOTA_SECRET_KEY. Raises if the key is unavailable -- writing plaintext
    is exactly what this table no longer tolerates.
    """
    from istota import secrets_store

    fernet = secrets_store._get_fernet()
    access_ct = fernet.encrypt(access_token.encode("utf-8"))
    refresh_ct = fernet.encrypt(refresh_token.encode("utf-8"))

    conn.execute(
        """INSERT INTO google_oauth_tokens (user_id, access_token, refresh_token, token_expiry, scopes)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            access_token = excluded.access_token,
            refresh_token = excluded.refresh_token,
            token_expiry = excluded.token_expiry,
            scopes = excluded.scopes,
            updated_at = datetime('now')""",
        (user_id, access_ct, refresh_ct, token_expiry, scopes),
    )
    conn.commit()


def _as_bytes(value) -> bytes:
    """Coerce an SQLite cell to bytes for Fernet decrypt.

    Cells may come back as bytes (BLOB) or str (TEXT, on legacy schemas where
    plaintext UTF-8 was stored as text). Fernet.decrypt accepts both forms
    when given bytes, so we normalise here.
    """
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError(f"unexpected token cell type: {type(value).__name__}")


def _migrate_google_oauth_encryption(conn: sqlite3.Connection) -> int:
    """Encrypt any plaintext rows in google_oauth_tokens.

    Detection is decrypt-or-fail: a Fernet token validates an HMAC, so a real
    plaintext value reliably raises and gets re-encrypted. Idempotent --
    rows that already decrypt are left alone. No-ops without
    $ISTOTA_SECRET_KEY (logged once, leaves rows as-is for a later boot).

    Returns the number of rows re-encrypted.
    """
    try:
        rows = conn.execute(
            "SELECT user_id, access_token, refresh_token FROM google_oauth_tokens"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    if not rows:
        return 0

    from istota import secrets_store

    if not secrets_store.secret_key_available():
        logger.info(
            "google_oauth: %d row(s) present but ISTOTA_SECRET_KEY unset -- "
            "skipping plaintext-to-Fernet migration", len(rows),
        )
        return 0

    fernet = secrets_store._get_fernet()
    migrated = 0
    for user_id, at, rt in rows:
        at_b, rt_b = _as_bytes(at), _as_bytes(rt)
        try:
            fernet.decrypt(at_b)
            fernet.decrypt(rt_b)
            continue  # already encrypted
        except Exception:
            pass

        try:
            at_ct = fernet.encrypt(at_b)
            rt_ct = fernet.encrypt(rt_b)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "google_oauth: re-encrypt failed user=%s: %s", user_id, exc,
            )
            continue
        conn.execute(
            "UPDATE google_oauth_tokens SET access_token = ?, refresh_token = ?, "
            "updated_at = datetime('now') WHERE user_id = ?",
            (at_ct, rt_ct, user_id),
        )
        migrated += 1

    if migrated:
        conn.commit()
        logger.info("google_oauth: encrypted %d plaintext row(s) at rest", migrated)
    return migrated


def delete_google_token(conn: sqlite3.Connection, user_id: str) -> bool:
    """Delete Google OAuth tokens for a user. Returns True if a row was deleted."""
    cursor = conn.execute(
        "DELETE FROM google_oauth_tokens WHERE user_id = ?", (user_id,),
    )
    conn.commit()
    return cursor.rowcount > 0


def has_google_token(conn: sqlite3.Connection, user_id: str) -> bool:
    """Decryption-free existence check for the UI's "connected" badge.

    Distinct from ``get_google_token`` returning a non-None value -- that's
    "row present AND decryptable AND key available". This one is just "row
    present", which is what the UI cares about (a stale-key row still wants
    a Disconnect button).
    """
    row = conn.execute(
        "SELECT 1 FROM google_oauth_tokens WHERE user_id = ? LIMIT 1", (user_id,),
    ).fetchone()
    return row is not None


# ============================================================================
# Talk message tracking functions
# ============================================================================


def update_task_pid(conn: sqlite3.Connection, task_id: int, pid: int) -> None:
    """Store the subprocess PID for a running task."""
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (pid, task_id))
    conn.commit()


def set_task_model_used(conn: sqlite3.Connection, task_id: int, model: str) -> None:
    """Record the model that actually ran a task (resolved canonical ID).

    Writes the dedicated ``model_used`` column, leaving ``model`` (the per-task
    override; empty = config default) untouched so a retry of a default-model
    task still re-resolves the current default rather than pinning attempt 1's
    model. Surfaces (web-chat meta) read ``model_used``.
    """
    conn.execute("UPDATE tasks SET model_used = ? WHERE id = ?", (model, task_id))
    conn.commit()


def touch_task_heartbeat(conn: sqlite3.Connection, task_id: int) -> None:
    """Record a liveness ping from the worker executing ``task_id``.

    A running worker calls this periodically. Stuck-task reclaim uses the
    heartbeat to tell a slow-but-alive worker from a dead one — see claim_task()
    (ISSUE-112). Scoped to status='running' so a ping that races task completion
    can't resurrect the heartbeat on a finished task.
    """
    conn.execute(
        "UPDATE tasks SET last_heartbeat = datetime('now') "
        "WHERE id = ? AND status = 'running'",
        (task_id,),
    )
    conn.commit()


def is_task_cancelled(conn: sqlite3.Connection, task_id: int) -> bool:
    """Check if a task has been flagged for cancellation."""
    row = conn.execute(
        "SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return bool(row and row[0])


def update_talk_response_id(
    conn: sqlite3.Connection,
    task_id: int,
    talk_response_id: int,
) -> None:
    """Store the Talk message ID of bot's response for a task."""
    conn.execute(
        "UPDATE tasks SET talk_response_id = ?, updated_at = datetime('now') WHERE id = ?",
        (talk_response_id, task_id),
    )


def get_reply_parent_task(
    conn: sqlite3.Connection,
    conversation_token: str,
    reply_to_talk_id: int,
) -> Task | None:
    """
    Find the task whose Talk message matches the replied-to ID.

    Checks both talk_message_id (user's message) and talk_response_id (bot's response)
    to find the conversation exchange being replied to.
    """
    cursor = conn.execute(
        f"""
        SELECT {_TASK_COLUMNS}
        FROM tasks
        WHERE conversation_token = ?
        AND (talk_message_id = ? OR talk_response_id = ?)
        AND status = 'completed'
        AND result IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (conversation_token, reply_to_talk_id, reply_to_talk_id),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return _row_to_task(row)


def save_task_selected_skills(
    conn: sqlite3.Connection,
    task_id: int,
    selected_skills: list[str],
) -> None:
    """Store the skills selected for a task (called right after skill selection)."""
    conn.execute(
        "UPDATE tasks SET selected_skills = ? WHERE id = ?",
        (json.dumps(selected_skills), task_id),
    )


def get_recent_conversation_skills(
    conn: sqlite3.Connection,
    conversation_token: str,
    exclude_task_id: int | None = None,
    max_age_minutes: int = 30,
    limit: int = 2,
) -> set[str]:
    """Get skill names from recent completed tasks in the same conversation.

    Returns a union of skills from the last N tasks within the time window.
    Used for skill stickiness in follow-up messages.
    """
    query = """
        SELECT selected_skills
        FROM tasks
        WHERE conversation_token = ?
        AND status = 'completed'
        AND selected_skills IS NOT NULL
        AND created_at > datetime('now', ?)
    """
    params: list = [conversation_token, f"-{max_age_minutes} minutes"]

    if exclude_task_id is not None:
        query += " AND id != ?"
        params.append(exclude_task_id)

    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)

    cursor = conn.execute(query, params)
    rows = cursor.fetchall()

    skills: set[str] = set()
    for row in rows:
        try:
            skills.update(json.loads(row["selected_skills"]))
        except (json.JSONDecodeError, TypeError):
            pass
    return skills


# ============================================================================
# Cleanup functions for scheduler robustness
# ============================================================================


def expire_stale_confirmations(conn: sqlite3.Connection, timeout_minutes: int) -> list[dict]:
    """
    Cancel tasks that have been pending_confirmation longer than timeout.
    Returns list of cancelled task info for notification.
    """
    cursor = conn.execute(
        """
        UPDATE tasks
        SET status = 'cancelled',
            error = 'Confirmation request timed out',
            updated_at = datetime('now')
        WHERE status = 'pending_confirmation'
        AND updated_at < datetime('now', '-' || ? || ' minutes')
        RETURNING id, user_id, conversation_token, prompt
        """,
        (timeout_minutes,),
    )
    return [
        {
            "id": row["id"],
            "user_id": row["user_id"],
            "conversation_token": row["conversation_token"],
            "prompt": row["prompt"][:100] if row["prompt"] else None,
        }
        for row in cursor.fetchall()
    ]


def get_stale_pending_tasks(conn: sqlite3.Connection, warn_minutes: int) -> list[Task]:
    """
    Get tasks that have been pending longer than threshold for logging.
    Excludes tasks that are scheduled for the future.
    """
    cursor = conn.execute(
        f"""
        SELECT {_TASK_COLUMNS}
        FROM tasks
        WHERE status = 'pending'
        AND created_at < datetime('now', '-' || ? || ' minutes')
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        """,
        (warn_minutes,),
    )
    return [_row_to_task(row) for row in cursor.fetchall()]


def fail_ancient_pending_tasks(conn: sqlite3.Connection, fail_hours: int) -> list[dict]:
    """
    Auto-fail tasks that have been pending too long.
    Returns list of failed task info for notification.
    Excludes tasks that are scheduled for the future.
    """
    cursor = conn.execute(
        """
        UPDATE tasks
        SET status = 'failed',
            error = 'Task timed out - pending too long without being processed',
            completed_at = datetime('now'),
            updated_at = datetime('now')
        WHERE status = 'pending'
        AND created_at < datetime('now', '-' || ? || ' hours')
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        RETURNING id, user_id, conversation_token, source_type, prompt
        """,
        (fail_hours,),
    )
    return [
        {
            "id": row["id"],
            "user_id": row["user_id"],
            "conversation_token": row["conversation_token"],
            "source_type": row["source_type"],
            "prompt": row["prompt"][:100] if row["prompt"] else None,
        }
        for row in cursor.fetchall()
    ]


def fail_stuck_locked_running_tasks(
    conn: sqlite3.Connection, max_retry_age_minutes: int = 60,
    stuck_running_minutes: int = 15,
    heartbeat_stuck_minutes: int = 5,
) -> list[dict]:
    """Fail or release tasks stuck in 'locked' or 'running' state.

    This mirrors the recovery logic in claim_task() but runs independently
    so stuck tasks are cleaned up even when no new tasks are being claimed.
    See claim_task() for ``stuck_running_minutes`` / ``heartbeat_stuck_minutes``
    (ISSUE-112).

    Returns list of failed task info for logging.
    """
    failed = []

    # Fail old stale locks (created too long ago to be worth retrying)
    cursor = conn.execute(
        """
        UPDATE tasks
        SET status = 'failed', error = 'Task too old to retry (stale lock)',
            locked_at = NULL, locked_by = NULL,
            completed_at = datetime('now'), updated_at = datetime('now')
        WHERE status = 'locked'
        AND locked_at < datetime('now', '-30 minutes')
        AND created_at < datetime('now', ? || ' minutes')
        RETURNING id, user_id, conversation_token, source_type
        """,
        (f"-{max_retry_age_minutes}",),
    )
    for row in cursor.fetchall():
        failed.append(dict(row))

    # Release recent stale locks (younger tasks get retried)
    conn.execute(
        """
        UPDATE tasks
        SET status = 'pending', locked_at = NULL, locked_by = NULL
        WHERE status = 'locked'
        AND locked_at < datetime('now', '-30 minutes')
        AND created_at >= datetime('now', ? || ' minutes')
        """,
        (f"-{max_retry_age_minutes}",),
    )

    # Fail old stuck 'running' tasks
    cursor = conn.execute(
        f"""
        UPDATE tasks
        SET status = 'failed', error = 'Task too old to retry (stuck running)',
            completed_at = datetime('now'), updated_at = datetime('now')
        WHERE status = 'running'
        AND {_STUCK_RUNNING_PREDICATE}
        AND created_at < datetime('now', ? || ' minutes')
        RETURNING id, user_id, conversation_token, source_type
        """,
        (*_stuck_running_params(heartbeat_stuck_minutes, stuck_running_minutes),
         f"-{max_retry_age_minutes}"),
    )
    for row in cursor.fetchall():
        failed.append(dict(row))

    # Release recent stuck 'running' tasks for retry. Clear last_heartbeat too:
    # leaving the dead worker's stale heartbeat on the row would keep the
    # _STUCK_RUNNING_PREDICATE firing after the next worker re-claims and re-runs
    # it, letting a second concurrent claimer re-steal it (duplicate execution).
    conn.execute(
        f"""
        UPDATE tasks
        SET status = 'pending', started_at = NULL, locked_at = NULL, locked_by = NULL,
            last_heartbeat = NULL, attempt_count = attempt_count + 1
        WHERE status = 'running'
        AND {_STUCK_RUNNING_PREDICATE}
        AND created_at >= datetime('now', ? || ' minutes')
        AND attempt_count < max_attempts
        """,
        (*_stuck_running_params(heartbeat_stuck_minutes, stuck_running_minutes),
         f"-{max_retry_age_minutes}"),
    )

    # Fail stuck 'running' tasks that have exhausted retries
    cursor = conn.execute(
        f"""
        UPDATE tasks
        SET status = 'failed',
            error = 'Task stuck in running state - worker may have crashed',
            completed_at = datetime('now'), updated_at = datetime('now')
        WHERE status = 'running'
        AND {_STUCK_RUNNING_PREDICATE}
        AND attempt_count >= max_attempts
        RETURNING id, user_id, conversation_token, source_type
        """,
        _stuck_running_params(heartbeat_stuck_minutes, stuck_running_minutes),
    )
    for row in cursor.fetchall():
        failed.append(dict(row))

    return failed


def recover_orphaned_tasks(
    conn: sqlite3.Connection, max_retry_age_minutes: int = 60,
) -> list[dict]:
    """Reclaim tasks left mid-execution by a dead prior daemon instance.

    Called once at daemon startup, under the singleton flock and before any
    worker spawns, so every ``running``/``locked`` row is definitionally an
    orphan — no live worker owns it. Unlike ``fail_stuck_locked_running_tasks``
    there is no time-based liveness guess: a fresh daemon knows the previous
    one is gone, so recovery is immediate instead of waiting out
    ``worker_stuck_minutes``. ``pending_confirmation`` is left alone (it's
    legitimately awaiting the user).

    Each orphan is resolved one of three ways, in priority order:

    - **cancelled** — ``cancel_requested`` was set (the user asked to cancel
      during the orphan window). Resolve straight to ``cancelled`` rather than
      re-running the whole task just to cancel on its first event.
    - **failed** — retries exhausted, too old to retry, or an inline-only
      source type (REPL runs in a separate process the daemon never claims, so
      releasing it would strand it ``pending`` forever).
    - **released** — otherwise: back to ``pending`` with ``attempt_count``
      bumped and every liveness column cleared, for a fresh attempt by the next
      worker.

    Returns one dict per recovered task — ``id``, ``user_id``,
    ``conversation_token``, ``source_type``, ``action`` (cancelled/failed/
    released) — so the caller can emit terminal event frames for the non-rerun
    cases. Ordering matches the branch priority above; each UPDATE filters on
    ``status IN ('running','locked')`` so a row resolved by an earlier branch
    is excluded from the later ones.
    """
    recovered: list[dict] = []

    # 1. User asked to cancel — honor it without a re-run.
    cursor = conn.execute(
        """
        UPDATE tasks
        SET status = 'cancelled', error = 'Cancelled by user',
            updated_at = datetime('now')
        WHERE status IN ('running', 'locked') AND cancel_requested = 1
        RETURNING id, user_id, conversation_token, source_type
        """
    )
    for row in cursor.fetchall():
        recovered.append({**dict(row), "action": "cancelled"})

    # 2. Not worth retrying: out of attempts, too old, or inline-only source.
    cursor = conn.execute(
        f"""
        UPDATE tasks
        SET status = 'failed', completed_at = datetime('now'),
            error = 'Worker died mid-task (scheduler restart); not retried',
            updated_at = datetime('now')
        WHERE status IN ('running', 'locked')
        AND (
            attempt_count >= max_attempts
            OR created_at < datetime('now', ? || ' minutes')
            OR source_type IN ({_INLINE_ONLY_IN})
        )
        RETURNING id, user_id, conversation_token, source_type
        """,
        (f"-{max_retry_age_minutes}",),
    )
    for row in cursor.fetchall():
        recovered.append({**dict(row), "action": "failed"})

    # 3. Retry-eligible: requeue with liveness cleared so the stuck predicate
    # can't re-fire and a second claimer can't re-steal it.
    cursor = conn.execute(
        """
        UPDATE tasks
        SET status = 'pending', attempt_count = attempt_count + 1,
            last_heartbeat = NULL, started_at = NULL,
            locked_at = NULL, locked_by = NULL, worker_pid = NULL,
            updated_at = datetime('now')
        WHERE status IN ('running', 'locked')
        RETURNING id, user_id, conversation_token, source_type
        """
    )
    for row in cursor.fetchall():
        recovered.append({**dict(row), "action": "released"})

    return recovered


def cleanup_old_tasks(conn: sqlite3.Connection, retention_days: int) -> int:
    """
    Delete old completed/failed/cancelled tasks and their logs.
    Returns number of tasks deleted.
    """
    # First, delete logs for tasks that will be deleted
    conn.execute(
        """
        DELETE FROM task_logs
        WHERE task_id IN (
            SELECT id FROM tasks
            WHERE status IN ('completed', 'failed', 'cancelled')
            AND completed_at < datetime('now', '-' || ? || ' days')
        )
        """,
        (retention_days,),
    )

    # ON DELETE CASCADE is a no-op without PRAGMA foreign_keys, so hand-delete
    # the event stream alongside the logs (same retention window).
    conn.execute(
        """
        DELETE FROM task_events
        WHERE task_id IN (
            SELECT id FROM tasks
            WHERE status IN ('completed', 'failed', 'cancelled')
            AND completed_at < datetime('now', '-' || ? || ' days')
        )
        """,
        (retention_days,),
    )

    # Delete the tasks themselves
    cursor = conn.execute(
        """
        DELETE FROM tasks
        WHERE status IN ('completed', 'failed', 'cancelled')
        AND completed_at < datetime('now', '-' || ? || ' days')
        """,
        (retention_days,),
    )
    return cursor.rowcount


# ============================================================================
# Trusted Email Senders
# ============================================================================


def add_trusted_sender(
    conn: sqlite3.Connection, user_id: str, sender_email: str,
) -> bool:
    """Add a trusted email sender. Returns True if newly added, False if already exists."""
    try:
        conn.execute(
            "INSERT INTO trusted_email_senders (user_id, sender_email) VALUES (?, ?)",
            (user_id, sender_email.lower()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def remove_trusted_sender(
    conn: sqlite3.Connection, user_id: str, sender_email: str,
) -> bool:
    """Remove a trusted email sender. Returns True if removed, False if not found."""
    cursor = conn.execute(
        "DELETE FROM trusted_email_senders WHERE user_id = ? AND sender_email = ?",
        (user_id, sender_email.lower()),
    )
    return cursor.rowcount > 0


def list_trusted_senders(
    conn: sqlite3.Connection, user_id: str,
) -> list[dict]:
    """List all trusted email senders for a user. Returns list of {sender_email, added_at}."""
    cursor = conn.execute(
        "SELECT sender_email, added_at FROM trusted_email_senders WHERE user_id = ? ORDER BY sender_email",
        (user_id,),
    )
    return [{"sender_email": row["sender_email"], "added_at": row["added_at"]} for row in cursor]


def is_sender_trusted_in_db(
    conn: sqlite3.Connection, user_id: str, sender_email: str,
) -> bool:
    """Check if an email sender is in the runtime trusted senders table."""
    cursor = conn.execute(
        "SELECT 1 FROM trusted_email_senders WHERE user_id = ? AND sender_email = ?",
        (user_id, sender_email.lower()),
    )
    return cursor.fetchone() is not None


# ============================================================================
# Key-Value Store
# ============================================================================


def kv_get(
    conn: sqlite3.Connection, user_id: str, namespace: str, key: str
) -> dict | None:
    """Get a value from the KV store. Returns dict with value and updated_at, or None."""
    cursor = conn.execute(
        "SELECT value, updated_at FROM istota_kv WHERE user_id = ? AND namespace = ? AND key = ?",
        (user_id, namespace, key),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {"value": row["value"], "updated_at": row["updated_at"]}


def kv_set(
    conn: sqlite3.Connection, user_id: str, namespace: str, key: str, value: str
) -> None:
    """Set a value in the KV store. Upserts if key already exists."""
    conn.execute(
        """
        INSERT INTO istota_kv (user_id, namespace, key, value, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(user_id, namespace, key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (user_id, namespace, key, value),
    )


def kv_delete(
    conn: sqlite3.Connection, user_id: str, namespace: str, key: str
) -> bool:
    """Delete a key from the KV store. Returns True if key existed."""
    cursor = conn.execute(
        "DELETE FROM istota_kv WHERE user_id = ? AND namespace = ? AND key = ?",
        (user_id, namespace, key),
    )
    return cursor.rowcount > 0


def kv_list(
    conn: sqlite3.Connection, user_id: str, namespace: str
) -> list[dict]:
    """List all entries in a namespace. Returns list of dicts with key, value, updated_at."""
    cursor = conn.execute(
        "SELECT key, value, updated_at FROM istota_kv WHERE user_id = ? AND namespace = ? ORDER BY key",
        (user_id, namespace),
    )
    return [
        {"key": row["key"], "value": row["value"], "updated_at": row["updated_at"]}
        for row in cursor.fetchall()
    ]


def kv_namespaces(conn: sqlite3.Connection, user_id: str) -> list[str]:
    """List distinct namespaces for a user."""
    cursor = conn.execute(
        "SELECT DISTINCT namespace FROM istota_kv WHERE user_id = ? ORDER BY namespace",
        (user_id,),
    )
    return [row["namespace"] for row in cursor.fetchall()]


# ============================================================================
# Talk polling state functions
# ============================================================================


def get_talk_poll_state(conn: sqlite3.Connection, conversation_token: str) -> int | None:
    """Get the last known message ID for a conversation."""
    cursor = conn.execute(
        "SELECT last_known_message_id FROM talk_poll_state WHERE conversation_token = ?",
        (conversation_token,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def set_talk_poll_state(
    conn: sqlite3.Connection,
    conversation_token: str,
    message_id: int,
) -> None:
    """Set the last known message ID for a conversation."""
    conn.execute(
        """
        INSERT INTO talk_poll_state (conversation_token, last_known_message_id, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(conversation_token) DO UPDATE SET
            last_known_message_id = excluded.last_known_message_id,
            updated_at = excluded.updated_at
        """,
        (conversation_token, message_id),
    )


# ============================================================================
# TASKS.md file task functions
# ============================================================================


def is_istota_task_tracked(conn: sqlite3.Connection, user_id: str, content_hash: str) -> bool:
    """Check if a TASKS.md task has already been tracked."""
    cursor = conn.execute(
        "SELECT 1 FROM istota_file_tasks WHERE user_id = ? AND content_hash = ?",
        (user_id, content_hash),
    )
    return cursor.fetchone() is not None


def track_istota_file_task(
    conn: sqlite3.Connection,
    user_id: str,
    content_hash: str,
    original_line: str,
    normalized_content: str,
    file_path: str,
    task_id: int,
) -> int:
    """Track a new task from a TASKS.md file."""
    cursor = conn.execute(
        """
        INSERT INTO istota_file_tasks (
            user_id, content_hash, original_line, normalized_content,
            file_path, task_id, status
        ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
        RETURNING id
        """,
        (user_id, content_hash, original_line, normalized_content, file_path, task_id),
    )
    return cursor.fetchone()[0]


def get_istota_file_task(conn: sqlite3.Connection, istota_task_id: int) -> IstotaFileTask | None:
    """Get a TASKS.md file task by its ID."""
    cursor = conn.execute(
        """
        SELECT id, user_id, content_hash, original_line, normalized_content,
               status, task_id, result_summary, error_message, attempt_count,
               max_attempts, file_path, created_at, started_at, completed_at
        FROM istota_file_tasks WHERE id = ?
        """,
        (istota_task_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return IstotaFileTask(
        id=row["id"],
        user_id=row["user_id"],
        content_hash=row["content_hash"],
        original_line=row["original_line"],
        normalized_content=row["normalized_content"],
        status=row["status"],
        task_id=row["task_id"],
        result_summary=row["result_summary"],
        error_message=row["error_message"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        file_path=row["file_path"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def get_istota_file_task_by_task_id(conn: sqlite3.Connection, task_id: int) -> IstotaFileTask | None:
    """Get a TASKS.md file task by its associated task ID."""
    cursor = conn.execute(
        """
        SELECT id, user_id, content_hash, original_line, normalized_content,
               status, task_id, result_summary, error_message, attempt_count,
               max_attempts, file_path, created_at, started_at, completed_at
        FROM istota_file_tasks WHERE task_id = ?
        """,
        (task_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return IstotaFileTask(
        id=row["id"],
        user_id=row["user_id"],
        content_hash=row["content_hash"],
        original_line=row["original_line"],
        normalized_content=row["normalized_content"],
        status=row["status"],
        task_id=row["task_id"],
        result_summary=row["result_summary"],
        error_message=row["error_message"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        file_path=row["file_path"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def update_istota_file_task_status(
    conn: sqlite3.Connection,
    istota_task_id: int,
    status: str,
    result_summary: str | None = None,
    error_message: str | None = None,
) -> None:
    """Update the status of a TASKS.md file task."""
    if status == "in_progress":
        conn.execute(
            "UPDATE istota_file_tasks SET status = ?, started_at = datetime('now') WHERE id = ?",
            (status, istota_task_id),
        )
    elif status == "completed":
        conn.execute(
            """
            UPDATE istota_file_tasks
            SET status = ?, completed_at = datetime('now'), result_summary = ?
            WHERE id = ?
            """,
            (status, result_summary, istota_task_id),
        )
    elif status == "failed":
        conn.execute(
            """
            UPDATE istota_file_tasks
            SET status = ?, completed_at = datetime('now'), error_message = ?,
                attempt_count = attempt_count + 1
            WHERE id = ?
            """,
            (status, error_message, istota_task_id),
        )
    else:
        conn.execute(
            "UPDATE istota_file_tasks SET status = ? WHERE id = ?",
            (status, istota_task_id),
        )


# ============================================================================
# Scheduled job functions
# ============================================================================


def get_enabled_scheduled_jobs(conn: sqlite3.Connection) -> list[ScheduledJob]:
    """Fetch all enabled scheduled jobs."""
    cursor = conn.execute(
        """
        SELECT id, user_id, name, cron_expression, prompt, command,
               conversation_token, output_target, enabled, last_run_at, created_at,
               silent_unless_action, skip_log_channel,
               consecutive_failures, last_error, last_success_at,
               once, model, effort, skill, skill_args
        FROM scheduled_jobs
        WHERE enabled = 1
        """
    )
    return [_row_to_scheduled_job(row) for row in cursor.fetchall()]


def get_user_scheduled_jobs(conn: sqlite3.Connection, user_id: str) -> list[ScheduledJob]:
    """Fetch all scheduled jobs for a user (enabled and disabled)."""
    cursor = conn.execute(
        """
        SELECT id, user_id, name, cron_expression, prompt, command,
               conversation_token, output_target, enabled, last_run_at, created_at,
               silent_unless_action, skip_log_channel,
               consecutive_failures, last_error, last_success_at,
               once, model, effort, skill, skill_args
        FROM scheduled_jobs
        WHERE user_id = ?
        ORDER BY name
        """,
        (user_id,),
    )
    return [_row_to_scheduled_job(row) for row in cursor.fetchall()]


def _row_to_scheduled_job(row: sqlite3.Row) -> ScheduledJob:
    """Convert a database row to a ScheduledJob object."""
    return ScheduledJob(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        cron_expression=row["cron_expression"],
        prompt=row["prompt"],
        conversation_token=row["conversation_token"],
        output_target=row["output_target"],
        enabled=bool(row["enabled"]),
        last_run_at=row["last_run_at"],
        created_at=row["created_at"],
        command=row["command"] if "command" in row.keys() else None,
        silent_unless_action=bool(row["silent_unless_action"]) if "silent_unless_action" in row.keys() else False,
        skip_log_channel=bool(row["skip_log_channel"]) if "skip_log_channel" in row.keys() else False,
        consecutive_failures=row["consecutive_failures"] if "consecutive_failures" in row.keys() else 0,
        last_error=row["last_error"] if "last_error" in row.keys() else None,
        last_success_at=row["last_success_at"] if "last_success_at" in row.keys() else None,
        once=bool(row["once"]) if "once" in row.keys() else False,
        model=row["model"] if "model" in row.keys() else None,
        effort=row["effort"] if "effort" in row.keys() else None,
        skill=row["skill"] if "skill" in row.keys() else None,
        skill_args=row["skill_args"] if "skill_args" in row.keys() else None,
    )


def set_scheduled_job_last_run(conn: sqlite3.Connection, job_id: int) -> None:
    """Update last_run_at to now for a scheduled job.

    Truncates seconds to :00 so croniter (minute resolution) never computes
    a next-fire time within the same minute, preventing double-fires.
    """
    conn.execute(
        "UPDATE scheduled_jobs SET last_run_at = strftime('%Y-%m-%d %H:%M:00', 'now') WHERE id = ?",
        (job_id,),
    )


def increment_scheduled_job_failures(
    conn: sqlite3.Connection, job_id: int, error: str,
) -> int:
    """Increment consecutive failure count and store error. Returns new count."""
    conn.execute(
        """
        UPDATE scheduled_jobs
        SET consecutive_failures = consecutive_failures + 1,
            last_error = ?
        WHERE id = ?
        """,
        (error[:500], job_id),
    )
    row = conn.execute(
        "SELECT consecutive_failures FROM scheduled_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    return row[0] if row else 0


def reset_scheduled_job_failures(conn: sqlite3.Connection, job_id: int) -> None:
    """Reset failure tracking on success."""
    conn.execute(
        """
        UPDATE scheduled_jobs
        SET consecutive_failures = 0, last_error = NULL,
            last_success_at = datetime('now')
        WHERE id = ?
        """,
        (job_id,),
    )


def disable_scheduled_job(conn: sqlite3.Connection, job_id: int) -> None:
    """Disable a scheduled job."""
    conn.execute(
        "UPDATE scheduled_jobs SET enabled = 0 WHERE id = ?",
        (job_id,),
    )


def get_scheduled_job(conn: sqlite3.Connection, job_id: int) -> ScheduledJob | None:
    """Look up a scheduled job by ID."""
    cursor = conn.execute(
        """
        SELECT id, user_id, name, cron_expression, prompt, command,
               conversation_token, output_target, enabled, last_run_at, created_at,
               silent_unless_action, skip_log_channel,
               consecutive_failures, last_error, last_success_at,
               once, model, effort, skill, skill_args
        FROM scheduled_jobs
        WHERE id = ?
        """,
        (job_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return _row_to_scheduled_job(row)


def delete_scheduled_job(conn: sqlite3.Connection, job_id: int) -> None:
    """Delete a scheduled job from the database."""
    conn.execute("DELETE FROM scheduled_jobs WHERE id = ?", (job_id,))


def enable_scheduled_job(conn: sqlite3.Connection, job_id: int) -> None:
    """Enable a scheduled job, reset failure count, and reset last_run_at to now.

    Resetting last_run_at prevents the scheduler from treating the re-enable as
    a catch-up opportunity and firing immediately. The next run will occur at the
    next scheduled window after the enable time.
    """
    conn.execute(
        """
        UPDATE scheduled_jobs
        SET enabled = 1, consecutive_failures = 0, last_error = NULL,
            last_run_at = datetime('now')
        WHERE id = ?
        """,
        (job_id,),
    )


def get_scheduled_job_by_name(
    conn: sqlite3.Connection, user_id: str, name: str,
) -> ScheduledJob | None:
    """Look up a scheduled job by user_id and name."""
    cursor = conn.execute(
        """
        SELECT id, user_id, name, cron_expression, prompt, command,
               conversation_token, output_target, enabled, last_run_at, created_at,
               silent_unless_action, skip_log_channel,
               consecutive_failures, last_error, last_success_at,
               once, model, effort, skill, skill_args
        FROM scheduled_jobs
        WHERE user_id = ? AND name = ?
        """,
        (user_id, name),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return _row_to_scheduled_job(row)


# ============================================================================
# Worker pool isolation queries
# ============================================================================


def get_users_with_pending_interactive_tasks(conn: sqlite3.Connection) -> list[str]:
    """Get users with pending interactive (talk/email) tasks."""
    cursor = conn.execute(
        """
        SELECT DISTINCT user_id FROM tasks
        WHERE status = 'pending'
        AND source_type IN ('talk', 'email')
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        """
    )
    return [row[0] for row in cursor.fetchall()]


def get_users_with_pending_background_tasks(conn: sqlite3.Connection) -> list[str]:
    """Get users with pending background (non-interactive) tasks only."""
    cursor = conn.execute(
        f"""
        SELECT DISTINCT user_id FROM tasks
        WHERE status = 'pending'
        AND source_type NOT IN ('talk', 'email', {_INLINE_ONLY_IN})
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        """
    )
    return [row[0] for row in cursor.fetchall()]


def get_users_with_pending_fg_queue_tasks(conn: sqlite3.Connection) -> list[str]:
    """Get users with pending foreground queue tasks."""
    cursor = conn.execute(
        f"""
        SELECT DISTINCT user_id FROM tasks
        WHERE status = 'pending'
        AND queue = 'foreground'
        AND source_type NOT IN ({_INLINE_ONLY_IN})
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        """
    )
    return [row[0] for row in cursor.fetchall()]


def get_users_with_pending_bg_queue_tasks(conn: sqlite3.Connection) -> list[str]:
    """Get users with pending background queue tasks."""
    cursor = conn.execute(
        f"""
        SELECT DISTINCT user_id FROM tasks
        WHERE status = 'pending'
        AND queue = 'background'
        AND source_type NOT IN ({_INLINE_ONLY_IN})
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        """
    )
    return [row[0] for row in cursor.fetchall()]


def count_running_tasks(conn: sqlite3.Connection) -> int:
    """Count all tasks currently in the ``running`` state.

    Process-wide denominator for the scheduler_stats health line — a thread
    or fd spike with zero running tasks is a leak, the same spike during
    heavy task processing is expected.
    """
    cursor = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'")
    return cursor.fetchone()[0]


def count_pending_tasks_for_user_queue(
    conn: sqlite3.Connection, user_id: str, queue: str,
) -> int:
    """Count pending tasks for a specific user and queue type.

    Raw backlog: counts every ready pending row, ignoring the per-channel
    single-active gate. Use for status / observability. For spawn-or-poll
    decisions use count_claimable_tasks_for_user_queue, which excludes tasks
    claim_task would currently refuse.
    """
    cursor = conn.execute(
        """
        SELECT COUNT(*) FROM tasks
        WHERE user_id = ? AND queue = ? AND status = 'pending'
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        """,
        (user_id, queue),
    )
    return cursor.fetchone()[0]


def count_claimable_tasks_for_user_queue(
    conn: sqlite3.Connection, user_id: str, queue: str,
) -> int:
    """Count pending tasks for (user, queue) that claim_task could claim *now*.

    Mirrors claim_task's claimability WHERE clause — same inline-only exclusion,
    same schedule gate, and (for the foreground queue) the same per-channel
    single-active gate via the shared _CLAIM_CHANNEL_GATE_SQL — so dispatch's
    spawn count and the idle worker's pre-check never count a task claim_task
    would refuse. Without this, a follow-up queued behind an active task in the
    same room reads as "1 pending" to dispatch (spawns a doomed worker) and to
    the idle pre-check (busy-polls claim_task every tick) for the whole lifetime
    of the blocking task.

    It does NOT replay the stale-lock / stuck-running maintenance UPDATEs
    claim_task runs first; a soon-to-be-released stuck task is simply not counted
    until released (a safe undercount, picked up on the next tick).
    """
    filters = [
        "user_id = ?",
        "queue = ?",
        "status = 'pending'",
        f"source_type NOT IN ({_INLINE_ONLY_IN})",
        "(scheduled_for IS NULL OR scheduled_for <= datetime('now'))",
    ]
    params: list = [user_id, queue]
    if queue == "foreground" or queue is None:
        filters.append(_CLAIM_CHANNEL_GATE_SQL)
    where_clause = " AND ".join(filters)
    cursor = conn.execute(
        f"SELECT COUNT(*) FROM tasks WHERE {where_clause}",
        params,
    )
    return cursor.fetchone()[0]


def has_active_foreground_task_for_channel(
    conn: sqlite3.Connection, conversation_token: str,
) -> bool:
    """Check if there's an active foreground task for the given channel.

    Active means pending, locked, or running — but not if cancellation
    has been requested (the task is winding down).
    """
    cursor = conn.execute(
        """
        SELECT 1 FROM tasks
        WHERE conversation_token = ?
        AND queue = 'foreground'
        AND status IN ('pending', 'locked', 'running')
        AND cancel_requested = 0
        LIMIT 1
        """,
        (conversation_token,),
    )
    return cursor.fetchone() is not None


# ============================================================================
# Sleep cycle state functions
# ============================================================================


def get_sleep_cycle_last_run(
    conn: sqlite3.Connection,
    user_id: str,
) -> tuple[str | None, int | None]:
    """
    Get the last sleep cycle run state for a user.

    Returns (last_run_at, last_processed_task_id).
    """
    cursor = conn.execute(
        "SELECT last_run_at, last_processed_task_id FROM sleep_cycle_state WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None, None
    return row["last_run_at"], row["last_processed_task_id"]


def set_sleep_cycle_last_run(
    conn: sqlite3.Connection,
    user_id: str,
    last_task_id: int | None,
) -> None:
    """Update the sleep cycle state for a user."""
    conn.execute(
        """
        INSERT INTO sleep_cycle_state (user_id, last_run_at, last_processed_task_id)
        VALUES (?, datetime('now'), ?)
        ON CONFLICT (user_id) DO UPDATE SET
            last_run_at = datetime('now'),
            last_processed_task_id = excluded.last_processed_task_id
        """,
        (user_id, last_task_id),
    )


# ============================================================================
# Channel sleep cycle state functions
# ============================================================================


def get_channel_sleep_cycle_last_run(
    conn: sqlite3.Connection,
    conversation_token: str,
) -> tuple[str | None, int | None]:
    """
    Get the last channel sleep cycle run state.

    Returns (last_run_at, last_processed_task_id).
    """
    cursor = conn.execute(
        "SELECT last_run_at, last_processed_task_id FROM channel_sleep_cycle_state WHERE conversation_token = ?",
        (conversation_token,),
    )
    row = cursor.fetchone()
    if not row:
        return None, None
    return row["last_run_at"], row["last_processed_task_id"]


def set_channel_sleep_cycle_last_run(
    conn: sqlite3.Connection,
    conversation_token: str,
    last_task_id: int | None,
) -> None:
    """Update the channel sleep cycle state."""
    conn.execute(
        """
        INSERT INTO channel_sleep_cycle_state (conversation_token, last_run_at, last_processed_task_id)
        VALUES (?, datetime('now'), ?)
        ON CONFLICT (conversation_token) DO UPDATE SET
            last_run_at = datetime('now'),
            last_processed_task_id = excluded.last_processed_task_id
        """,
        (conversation_token, last_task_id),
    )


def get_completed_channel_tasks_since(
    conn: sqlite3.Connection,
    conversation_token: str,
    since_datetime: str,
    after_task_id: int | None = None,
) -> list[Task]:
    """
    Fetch completed tasks for a conversation token since a given datetime.

    Returns list of Task objects ordered by id ascending.
    """
    query = f"""
        SELECT {_TASK_COLUMNS}
        FROM tasks
        WHERE conversation_token = ?
        AND status = 'completed'
        AND result IS NOT NULL
        AND completed_at >= ?
    """
    params: list = [conversation_token, since_datetime]

    if after_task_id is not None:
        query += " AND id > ?"
        params.append(after_task_id)

    query += " ORDER BY id ASC"

    cursor = conn.execute(query, params)
    return [_row_to_task(row) for row in cursor.fetchall()]


def get_active_channel_tokens(
    conn: sqlite3.Connection,
    since_datetime: str,
) -> list[str]:
    """
    Get distinct conversation tokens from recent completed tasks.

    Used to auto-discover active channels for sleep cycle processing.
    """
    cursor = conn.execute(
        """
        SELECT DISTINCT conversation_token
        FROM tasks
        WHERE status = 'completed'
        AND conversation_token IS NOT NULL
        AND conversation_token != ''
        AND completed_at >= ?
        ORDER BY conversation_token
        """,
        (since_datetime,),
    )
    return [row[0] for row in cursor.fetchall()]


def get_completed_tasks_since(
    conn: sqlite3.Connection,
    user_id: str,
    since_datetime: str,
    after_task_id: int | None = None,
) -> list[Task]:
    """
    Fetch completed tasks for a user since a given datetime.

    Args:
        since_datetime: ISO format datetime string (UTC)
        after_task_id: Only return tasks with id > this value (to avoid reprocessing)

    Returns list of Task objects ordered by id ascending.
    """
    query = f"""
        SELECT {_TASK_COLUMNS}
        FROM tasks
        WHERE user_id = ?
        AND status = 'completed'
        AND result IS NOT NULL
        AND completed_at >= ?
    """
    params: list = [user_id, since_datetime]

    if after_task_id is not None:
        query += " AND id > ?"
        params.append(after_task_id)

    query += " ORDER BY id ASC"

    cursor = conn.execute(query, params)
    return [_row_to_task(row) for row in cursor.fetchall()]


def list_istota_file_tasks(
    conn: sqlite3.Connection,
    user_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[IstotaFileTask]:
    """List TASKS.md file tasks with optional filters."""
    query = "SELECT * FROM istota_file_tasks WHERE 1=1"
    params: list = []

    if user_id:
        query += " AND user_id = ?"
        params.append(user_id)
    if status:
        query += " AND status = ?"
        params.append(status)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    cursor = conn.execute(query, params)
    return [
        IstotaFileTask(
            id=row["id"],
            user_id=row["user_id"],
            content_hash=row["content_hash"],
            original_line=row["original_line"],
            normalized_content=row["normalized_content"],
            status=row["status"],
            task_id=row["task_id"],
            result_summary=row["result_summary"],
            error_message=row["error_message"],
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            file_path=row["file_path"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )
        for row in cursor.fetchall()
    ]


# ============================================================================
# Heartbeat state functions
# ============================================================================


@dataclass
class HeartbeatState:
    """State for a heartbeat check."""
    user_id: str
    check_name: str
    last_check_at: str | None
    last_alert_at: str | None
    last_healthy_at: str | None
    last_error_at: str | None
    consecutive_errors: int


def get_heartbeat_state(
    conn: sqlite3.Connection,
    user_id: str,
    check_name: str,
) -> HeartbeatState | None:
    """Get the state for a heartbeat check."""
    cursor = conn.execute(
        """
        SELECT user_id, check_name, last_check_at, last_alert_at,
               last_healthy_at, last_error_at, consecutive_errors
        FROM heartbeat_state
        WHERE user_id = ? AND check_name = ?
        """,
        (user_id, check_name),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return HeartbeatState(
        user_id=row["user_id"],
        check_name=row["check_name"],
        last_check_at=row["last_check_at"],
        last_alert_at=row["last_alert_at"],
        last_healthy_at=row["last_healthy_at"],
        last_error_at=row["last_error_at"],
        consecutive_errors=row["consecutive_errors"],
    )


def update_heartbeat_state(
    conn: sqlite3.Connection,
    user_id: str,
    check_name: str,
    *,
    last_check_at: bool = False,
    last_alert_at: bool = False,
    last_healthy_at: bool = False,
    last_error_at: bool = False,
    reset_errors: bool = False,
    increment_errors: bool = False,
) -> None:
    """
    Update heartbeat state fields.

    Pass True for timestamp fields to set them to now.
    Pass reset_errors=True to reset consecutive_errors to 0.
    Pass increment_errors=True to increment consecutive_errors.
    """
    # Ensure row exists first
    conn.execute(
        """
        INSERT INTO heartbeat_state (user_id, check_name)
        VALUES (?, ?)
        ON CONFLICT (user_id, check_name) DO NOTHING
        """,
        (user_id, check_name),
    )

    updates = []
    params: list = []
    if last_check_at:
        updates.append("last_check_at = datetime('now')")
    if last_alert_at:
        updates.append("last_alert_at = datetime('now')")
    if last_healthy_at:
        updates.append("last_healthy_at = datetime('now')")
    if last_error_at:
        updates.append("last_error_at = datetime('now')")
    if reset_errors:
        updates.append("consecutive_errors = 0")
    if increment_errors:
        updates.append("consecutive_errors = consecutive_errors + 1")

    if updates:
        params.extend([user_id, check_name])
        conn.execute(
            f"""
            UPDATE heartbeat_state
            SET {", ".join(updates)}
            WHERE user_id = ? AND check_name = ?
            """,
            params,
        )


# ============================================================================
# Reminder state functions (for shuffle-queue rotation)
# ============================================================================


@dataclass
class ReminderState:
    """State for reminder rotation queue."""
    user_id: str
    queue: list[int]  # Remaining reminder indices
    content_hash: str  # Hash of reminders content


def get_reminder_state(conn: sqlite3.Connection, user_id: str) -> ReminderState | None:
    """Get the reminder rotation state for a user."""
    cursor = conn.execute(
        "SELECT user_id, queue, content_hash FROM reminder_state WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return ReminderState(
        user_id=row["user_id"],
        queue=json.loads(row["queue"]),
        content_hash=row["content_hash"],
    )


def set_reminder_state(
    conn: sqlite3.Connection,
    user_id: str,
    queue: list[int],
    content_hash: str,
) -> None:
    """Set the reminder rotation state for a user."""
    conn.execute(
        """
        INSERT INTO reminder_state (user_id, queue, content_hash, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT (user_id) DO UPDATE SET
            queue = excluded.queue,
            content_hash = excluded.content_hash,
            updated_at = datetime('now')
        """,
        (user_id, json.dumps(queue), content_hash),
    )


# ============================================================================
# Monarch Money transaction deduplication functions
# ============================================================================


def is_monarch_transaction_synced(
    conn: sqlite3.Connection,
    user_id: str,
    monarch_transaction_id: str,
) -> bool:
    """Check if a Monarch transaction has already been synced."""
    cursor = conn.execute(
        "SELECT 1 FROM monarch_synced_transactions WHERE user_id = ? AND monarch_transaction_id = ?",
        (user_id, monarch_transaction_id),
    )
    return cursor.fetchone() is not None


@dataclass
class MonarchSyncedTransaction:
    """A previously synced Monarch transaction for reconciliation."""
    id: int
    monarch_transaction_id: str
    tags_json: str | None
    amount: float | None
    merchant: str | None
    posted_account: str | None
    txn_date: str | None


def track_monarch_transaction(
    conn: sqlite3.Connection,
    user_id: str,
    monarch_transaction_id: str,
    tags_json: str | None = None,
    amount: float | None = None,
    merchant: str | None = None,
    posted_account: str | None = None,
    txn_date: str | None = None,
) -> int:
    """Record that a Monarch transaction has been synced with metadata for reconciliation."""
    cursor = conn.execute(
        """
        INSERT INTO monarch_synced_transactions (
            user_id, monarch_transaction_id, tags_json, amount, merchant, posted_account, txn_date
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (user_id, monarch_transaction_id) DO UPDATE SET
            tags_json = excluded.tags_json,
            amount = excluded.amount,
            merchant = excluded.merchant,
            posted_account = excluded.posted_account,
            txn_date = excluded.txn_date
        RETURNING id
        """,
        (user_id, monarch_transaction_id, tags_json, amount, merchant, posted_account, txn_date),
    )
    row = cursor.fetchone()
    return row[0] if row else 0


def track_monarch_transactions_batch(
    conn: sqlite3.Connection,
    user_id: str,
    transactions: list[dict],
) -> int:
    """Record multiple Monarch transactions as synced with metadata.

    Args:
        conn: Database connection
        user_id: User ID
        transactions: List of dicts with keys: id, tags_json, amount, merchant,
                      posted_account, txn_date, content_hash (optional)

    Returns:
        Count of transactions inserted/updated
    """
    count = 0
    for txn in transactions:
        cursor = conn.execute(
            """
            INSERT INTO monarch_synced_transactions (
                user_id, monarch_transaction_id, tags_json, amount, merchant,
                posted_account, txn_date, content_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id, monarch_transaction_id) DO UPDATE SET
                tags_json = excluded.tags_json,
                amount = excluded.amount,
                merchant = excluded.merchant,
                posted_account = excluded.posted_account,
                txn_date = excluded.txn_date,
                content_hash = excluded.content_hash
            """,
            (
                user_id,
                txn["id"],
                txn.get("tags_json"),
                txn.get("amount"),
                txn.get("merchant"),
                txn.get("posted_account"),
                txn.get("txn_date"),
                txn.get("content_hash"),
            ),
        )
        count += cursor.rowcount
    return count


def is_content_hash_synced(
    conn: sqlite3.Connection,
    user_id: str,
    content_hash: str,
) -> bool:
    """Check if a content hash exists in any transaction tracking table.

    Checks both monarch_synced_transactions and csv_imported_transactions
    for cross-source deduplication.
    """
    cursor = conn.execute(
        """
        SELECT 1 FROM monarch_synced_transactions
        WHERE user_id = ? AND content_hash = ?
        UNION
        SELECT 1 FROM csv_imported_transactions
        WHERE user_id = ? AND content_hash = ?
        LIMIT 1
        """,
        (user_id, content_hash, user_id, content_hash),
    )
    return cursor.fetchone() is not None


def get_active_monarch_synced_transactions(
    conn: sqlite3.Connection,
    user_id: str,
) -> list[MonarchSyncedTransaction]:
    """Get all synced transactions that haven't been recategorized.

    Used for reconciliation to check if tags have changed in Monarch.
    """
    cursor = conn.execute(
        """
        SELECT id, monarch_transaction_id, tags_json, amount, merchant, posted_account, txn_date
        FROM monarch_synced_transactions
        WHERE user_id = ? AND recategorized_at IS NULL
        """,
        (user_id,),
    )
    return [
        MonarchSyncedTransaction(
            id=row["id"],
            monarch_transaction_id=row["monarch_transaction_id"],
            tags_json=row["tags_json"],
            amount=row["amount"],
            merchant=row["merchant"],
            posted_account=row["posted_account"],
            txn_date=row["txn_date"],
        )
        for row in cursor.fetchall()
    ]


def mark_monarch_transaction_recategorized(
    conn: sqlite3.Connection,
    user_id: str,
    monarch_transaction_id: str,
) -> bool:
    """Mark a synced transaction as recategorized (business tag removed).

    Returns True if a row was updated.
    """
    cursor = conn.execute(
        """
        UPDATE monarch_synced_transactions
        SET recategorized_at = datetime('now')
        WHERE user_id = ? AND monarch_transaction_id = ? AND recategorized_at IS NULL
        """,
        (user_id, monarch_transaction_id),
    )
    return cursor.rowcount > 0


def update_monarch_transaction_posted_account(
    conn: sqlite3.Connection,
    user_id: str,
    monarch_transaction_id: str,
    new_posted_account: str,
) -> bool:
    """Update the posted_account for a synced transaction after category change.

    Returns True if a row was updated.
    """
    cursor = conn.execute(
        """
        UPDATE monarch_synced_transactions
        SET posted_account = ?
        WHERE user_id = ? AND monarch_transaction_id = ? AND recategorized_at IS NULL
        """,
        (new_posted_account, user_id, monarch_transaction_id),
    )
    return cursor.rowcount > 0


# ============================================================================
# CSV import transaction deduplication functions
# ============================================================================


def compute_transaction_hash(
    txn_date: str,
    amount: float,
    merchant: str,
    account: str = "",
) -> str:
    """Compute SHA-256 hash for transaction deduplication.

    Args:
        txn_date: Transaction date in YYYY-MM-DD format
        amount: Transaction amount
        merchant: Merchant/payee name
        account: Account name (optional, omit for cross-source matching)

    Returns:
        Hex-encoded SHA-256 hash
    """
    import hashlib
    # Normalize the components for consistent hashing
    content = f"{txn_date}|{amount:.2f}|{merchant.strip().lower()}"
    if account:
        content += f"|{account.strip().lower()}"
    return hashlib.sha256(content.encode()).hexdigest()


def is_csv_transaction_imported(
    conn: sqlite3.Connection,
    user_id: str,
    content_hash: str,
) -> bool:
    """Check if a CSV transaction has already been imported."""
    cursor = conn.execute(
        "SELECT 1 FROM csv_imported_transactions WHERE user_id = ? AND content_hash = ?",
        (user_id, content_hash),
    )
    return cursor.fetchone() is not None


def track_csv_transaction(
    conn: sqlite3.Connection,
    user_id: str,
    content_hash: str,
    source_file: str | None = None,
) -> int:
    """Record that a CSV transaction has been imported."""
    cursor = conn.execute(
        """
        INSERT INTO csv_imported_transactions (user_id, content_hash, source_file)
        VALUES (?, ?, ?)
        ON CONFLICT (user_id, content_hash) DO NOTHING
        RETURNING id
        """,
        (user_id, content_hash, source_file),
    )
    row = cursor.fetchone()
    return row[0] if row else 0


def track_csv_transactions_batch(
    conn: sqlite3.Connection,
    user_id: str,
    hashes: list[str],
    source_file: str | None = None,
) -> int:
    """Record multiple CSV transactions as imported. Returns count inserted."""
    count = 0
    for content_hash in hashes:
        cursor = conn.execute(
            """
            INSERT INTO csv_imported_transactions (user_id, content_hash, source_file)
            VALUES (?, ?, ?)
            ON CONFLICT (user_id, content_hash) DO NOTHING
            """,
            (user_id, content_hash, source_file),
        )
        count += cursor.rowcount
    return count




# ============================================================================
# Skills fingerprint functions
# ============================================================================


def get_user_skills_fingerprint(conn: sqlite3.Connection, user_id: str) -> str | None:
    """Get the stored skills fingerprint for a user."""
    cursor = conn.execute(
        "SELECT fingerprint FROM user_skills_fingerprint WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def set_user_skills_fingerprint(conn: sqlite3.Connection, user_id: str, fingerprint: str) -> None:
    """Store or update the skills fingerprint for a user."""
    conn.execute(
        """
        INSERT INTO user_skills_fingerprint (user_id, fingerprint, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT (user_id) DO UPDATE SET
            fingerprint = excluded.fingerprint,
            updated_at = datetime('now')
        """,
        (user_id, fingerprint),
    )


# ============================================================================


# ============================================================================
# Talk message cache functions
# ============================================================================


def upsert_talk_messages(
    conn: sqlite3.Connection,
    conversation_token: str,
    messages: list[dict],
) -> int:
    """Bulk insert/replace Talk API messages into the cache.

    Maps raw API field names to DB columns. Returns count of rows affected.
    """
    if not messages:
        return 0

    count = 0
    for msg in messages:
        parent = msg.get("parent")
        parent_id = None
        if isinstance(parent, dict) and parent.get("id"):
            parent_id = parent["id"]

        message_params = msg.get("messageParameters")
        if message_params is not None:
            params_json = json.dumps(message_params)
        else:
            params_json = None

        conn.execute(
            """
            INSERT INTO talk_messages (
                message_id, conversation_token, actor_id, actor_display_name,
                actor_type, message_text, message_type, message_parameters,
                timestamp, reference_id, deleted, parent_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_token, message_id) DO UPDATE SET
                actor_id = excluded.actor_id,
                actor_display_name = excluded.actor_display_name,
                actor_type = excluded.actor_type,
                message_text = excluded.message_text,
                message_type = excluded.message_type,
                message_parameters = excluded.message_parameters,
                timestamp = excluded.timestamp,
                deleted = excluded.deleted,
                parent_id = excluded.parent_id,
                reference_id = CASE
                    WHEN talk_messages.reference_id LIKE '%:result'
                    THEN talk_messages.reference_id
                    ELSE excluded.reference_id
                END
            """,
            (
                msg.get("id"),
                conversation_token,
                msg.get("actorId", ""),
                msg.get("actorDisplayName", ""),
                msg.get("actorType", "users"),
                msg.get("message", ""),
                msg.get("messageType", "comment"),
                params_json,
                msg.get("timestamp", 0),
                msg.get("referenceId"),
                1 if msg.get("deleted") else 0,
                parent_id,
            ),
        )
        count += 1
    return count


def get_cached_talk_messages(
    conn: sqlite3.Connection,
    conversation_token: str,
    limit: int = 100,
) -> list[dict]:
    """Retrieve cached messages in oldest-first order (same format as Talk API).

    Returns dicts matching the structure that build_talk_context() expects.
    """
    cursor = conn.execute(
        """
        SELECT message_id, actor_id, actor_display_name, actor_type,
               message_text, message_type, message_parameters,
               timestamp, reference_id, deleted, parent_id
        FROM talk_messages
        WHERE conversation_token = ?
        ORDER BY message_id DESC
        LIMIT ?
        """,
        (conversation_token, limit),
    )
    rows = cursor.fetchall()

    # Reverse to oldest-first (query fetches newest-first for LIMIT)
    messages = []
    for row in reversed(rows):
        params = row["message_parameters"]
        if params is not None:
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, TypeError):
                params = {}
        else:
            params = {}

        msg = {
            "id": row["message_id"],
            "actorId": row["actor_id"],
            "actorDisplayName": row["actor_display_name"],
            "actorType": row["actor_type"],
            "message": row["message_text"],
            "messageType": row["message_type"],
            "messageParameters": params,
            "timestamp": row["timestamp"],
            "referenceId": row["reference_id"],
            "deleted": bool(row["deleted"]),
        }
        if row["parent_id"] is not None:
            msg["parent"] = {"id": row["parent_id"]}
        messages.append(msg)

    return messages


def has_cached_talk_messages(
    conn: sqlite3.Connection,
    conversation_token: str,
) -> bool:
    """Check if any cached messages exist for a conversation."""
    cursor = conn.execute(
        "SELECT 1 FROM talk_messages WHERE conversation_token = ? LIMIT 1",
        (conversation_token,),
    )
    return cursor.fetchone() is not None


def cleanup_old_talk_messages(
    conn: sqlite3.Connection,
    max_per_conversation: int = 200,
) -> int:
    """Trim cached talk messages to keep only the latest N per conversation.

    Uses a per-conversation cap instead of time-based retention to avoid
    deleting old-but-still-useful context messages (which would trigger
    repeated backfills).

    Returns count of rows deleted.
    """
    cursor = conn.execute(
        """
        DELETE FROM talk_messages
        WHERE rowid IN (
            SELECT rowid FROM talk_messages AS t
            WHERE (
                SELECT COUNT(*) FROM talk_messages AS t2
                WHERE t2.conversation_token = t.conversation_token
                  AND t2.message_id >= t.message_id
            ) > ?
        )
        """,
        (max_per_conversation,),
    )
    return cursor.rowcount


# ============================================================================
# Geocode cache functions
# ============================================================================


def get_cached_geocode(
    conn: sqlite3.Connection,
    location_text: str,
) -> tuple[float, float] | None:
    """Look up a cached geocode result. Returns (lat, lon) or None."""
    cursor = conn.execute(
        "SELECT lat, lon FROM geocode_cache WHERE location_text = ?",
        (location_text,),
    )
    row = cursor.fetchone()
    if row:
        return (row["lat"], row["lon"])
    return None


def cache_geocode(
    conn: sqlite3.Connection,
    location_text: str,
    lat: float,
    lon: float,
) -> None:
    """Cache a geocode result."""
    conn.execute(
        """
        INSERT OR REPLACE INTO geocode_cache (location_text, lat, lon)
        VALUES (?, ?, ?)
        """,
        (location_text, lat, lon),
    )


def get_reverse_geocode(
    conn: sqlite3.Connection,
    lat: float,
    lon: float,
) -> dict | None:
    """Look up a cached reverse geocode result. Returns dict or None."""
    lat_rounded = round(lat, 4)
    lon_rounded = round(lon, 4)
    cursor = conn.execute(
        """SELECT display_name, neighborhood, suburb, road, city
           FROM reverse_geocode_cache
           WHERE lat_rounded = ? AND lon_rounded = ?""",
        (lat_rounded, lon_rounded),
    )
    row = cursor.fetchone()
    if row:
        return dict(row)
    return None


def cache_reverse_geocode(
    conn: sqlite3.Connection,
    lat: float,
    lon: float,
    result: dict,
) -> None:
    """Cache a reverse geocode result. Rounds to 4 decimal places (~11m)."""
    lat_rounded = round(lat, 4)
    lon_rounded = round(lon, 4)
    conn.execute(
        """INSERT OR REPLACE INTO reverse_geocode_cache
           (lat_rounded, lon_rounded, display_name, neighborhood, suburb, road, city, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            lat_rounded,
            lon_rounded,
            result.get("display_name"),
            result.get("neighborhood"),
            result.get("suburb"),
            result.get("road"),
            result.get("city"),
            json.dumps(result.get("raw", {})),
        ),
    )
