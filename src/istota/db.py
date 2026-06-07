"""Database operations for istota task queue."""

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
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
    queue: str = "foreground"
    confirmed_at: str | None = None
    selected_skills: str | None = None  # JSON array of skill names
    model: str | None = None  # Per-task model override; empty/None = use config default
    effort: str | None = None  # Per-task effort override; empty/None = use config default
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
        ("command", "TEXT"),
        ("queue", "TEXT DEFAULT 'foreground'"),
        ("actions_taken", "TEXT"),
        ("execution_trace", "TEXT"),
        ("selected_skills", "TEXT"),
        ("model", "TEXT"),
        ("effort", "TEXT"),
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

    # Encrypt any plaintext Google OAuth tokens at rest. Idempotent --
    # rows already in Fernet form (the new write path) are detected via
    # decrypt-or-fail and skipped. No-op on fresh installs (table not
    # created until schema.sql runs below) and on deployments without
    # $ISTOTA_SECRET_KEY (the read path will fail loudly so operators
    # notice and the user re-auths).
    _migrate_google_oauth_encryption(conn)


def init_db(db_path: Path) -> None:
    """Initialize database with schema."""
    schema_path = Path(__file__).parent.parent.parent / "schema.sql"
    with sqlite3.connect(db_path) as conn:
        # Run migrations first so new columns exist before schema creates indexes on them
        _run_migrations(conn)
        conn.executescript(schema_path.read_text())


@contextmanager
def get_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Get database connection with row factory."""
    # timeout=30.0 waits up to 30s for locks instead of failing immediately
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
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
            heartbeat_silent, skip_log_channel, scheduled_job_id, queue, model, effort,
            talk_delivery_token, skill, skill_args
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    "queue, confirmed_at, selected_skills, model, effort, "
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
        queue=row["queue"],
        confirmed_at=row["confirmed_at"],
        selected_skills=row["selected_skills"],
        model=row["model"],
        effort=row["effort"],
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

    # Release recent stuck 'running' tasks for retry
    conn.execute(
        f"""
        UPDATE tasks
        SET status = 'pending', started_at = NULL, locked_at = NULL, locked_by = NULL,
            attempt_count = attempt_count + 1
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

    # Atomically claim a task (optionally filtered by user_id and/or queue)
    filters = ["status = 'pending'", "(scheduled_for IS NULL OR scheduled_for <= datetime('now'))"]
    params: list = [worker_id]
    if user_id is not None:
        filters.append("user_id = ?")
        params.append(user_id)
    if queue is not None:
        filters.append("queue = ?")
        params.append(queue)

    # Per-channel gate: one active foreground task per conversation_token.
    # Skip pending fg tasks whose channel already has a locked/running fg task.
    # Tasks with no conversation_token (cron, email routed to talk later) and
    # background-queue tasks are unaffected.
    if queue == "foreground" or queue is None:
        filters.append(
            """
            NOT (
                tasks.queue = 'foreground'
                AND tasks.conversation_token IS NOT NULL
                AND tasks.conversation_token != ''
                AND EXISTS (
                    SELECT 1 FROM tasks t2
                    WHERE t2.conversation_token = tasks.conversation_token
                    AND t2.queue = 'foreground'
                    AND t2.status IN ('locked', 'running')
                    AND t2.cancel_requested = 0
                    AND t2.id != tasks.id
                )
            )
            """
        )

    where_clause = " AND ".join(filters)

    cursor = conn.execute(
        f"""
        UPDATE tasks
        SET status = 'locked', locked_at = datetime('now'), locked_by = ?
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
        """
        SELECT DISTINCT user_id FROM tasks
        WHERE status = 'pending'
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
    """Mark task for retry after a delay."""
    conn.execute(
        """
        UPDATE tasks
        SET status = 'pending',
            attempt_count = attempt_count + 1,
            error = ?,
            scheduled_for = datetime('now', '+' || ? || ' minutes'),
            locked_at = NULL,
            locked_by = NULL,
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

    Args:
        exclude_source_types: If provided, exclude tasks with these source_types
            from the history (e.g. ["scheduled", "briefing", "heartbeat"]).
    """
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


def get_previous_tasks(
    conn: sqlite3.Connection,
    conversation_token: str,
    exclude_task_id: int | None = None,
    limit: int = 3,
) -> list[ConversationMessage]:
    """
    Get the most recent completed tasks in a conversation,
    regardless of source_type.

    Used to ensure recent messages are always available in context even
    when their source_type would normally be excluded (e.g. scheduled,
    briefing).  Returns up to ``limit`` tasks in oldest-first order.
    """
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


def delete_task_events(conn: sqlite3.Connection, task_id: int) -> int:
    """Delete all events for a task. Returns the row count.

    Called on retry (a task keeps its id across attempts; a fresh EventWriter
    resets its seq counter to 1, which would collide with the prior attempt's
    surviving rows on UNIQUE(task_id, seq)). Clearing the slate makes the event
    log reflect the final attempt only — matching deferred-op behavior.
    """
    cursor = conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
    return cursor.rowcount


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
) -> int:
    """Record an outbound email for thread matching."""
    cursor = conn.execute(
        """
        INSERT INTO sent_emails
            (user_id, task_id, message_id, to_addr, subject, thread_id,
             in_reply_to, "references", conversation_token, talk_delivery_token)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (user_id, task_id, message_id, to_addr, subject, thread_id,
         in_reply_to, references, conversation_token, talk_delivery_token),
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
               talk_delivery_token
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
               talk_delivery_token
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

    # Release recent stuck 'running' tasks for retry
    conn.execute(
        f"""
        UPDATE tasks
        SET status = 'pending', started_at = NULL, locked_at = NULL, locked_by = NULL,
            attempt_count = attempt_count + 1
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
        """
        SELECT DISTINCT user_id FROM tasks
        WHERE status = 'pending'
        AND source_type NOT IN ('talk', 'email')
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        """
    )
    return [row[0] for row in cursor.fetchall()]


def get_users_with_pending_fg_queue_tasks(conn: sqlite3.Connection) -> list[str]:
    """Get users with pending foreground queue tasks."""
    cursor = conn.execute(
        """
        SELECT DISTINCT user_id FROM tasks
        WHERE status = 'pending'
        AND queue = 'foreground'
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        """
    )
    return [row[0] for row in cursor.fetchall()]


def get_users_with_pending_bg_queue_tasks(conn: sqlite3.Connection) -> list[str]:
    """Get users with pending background queue tasks."""
    cursor = conn.execute(
        """
        SELECT DISTINCT user_id FROM tasks
        WHERE status = 'pending'
        AND queue = 'background'
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        """
    )
    return [row[0] for row in cursor.fetchall()]


def count_pending_tasks_for_user_queue(
    conn: sqlite3.Connection, user_id: str, queue: str,
) -> int:
    """Count pending tasks for a specific user and queue type."""
    cursor = conn.execute(
        """
        SELECT COUNT(*) FROM tasks
        WHERE user_id = ? AND queue = ? AND status = 'pending'
        AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
        """,
        (user_id, queue),
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
