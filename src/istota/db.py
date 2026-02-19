"""Database operations for istota task queue."""

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

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
    scheduled_job_id: int | None = None
    queue: str = "foreground"


@dataclass
class UserResource:
    id: int
    user_id: str
    resource_type: str
    resource_path: str
    display_name: str | None
    permissions: str


@dataclass
class BriefingConfig:
    id: int
    user_id: str
    name: str
    cron_expression: str
    conversation_token: str
    components: dict
    enabled: bool
    last_run_at: str | None


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
    consecutive_failures: int = 0
    last_error: str | None = None
    last_success_at: str | None = None
    once: bool = False


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
        ("heartbeat_silent", "INTEGER DEFAULT 0"),
        ("scheduled_job_id", "INTEGER"),
        ("command", "TEXT"),
        ("queue", "TEXT DEFAULT 'foreground'"),
        ("actions_taken", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # Column already exists or table doesn't exist yet

    # Scheduled jobs table migrations
    for col, col_type in [
        ("silent_unless_action", "INTEGER DEFAULT 0"),
        ("command", "TEXT"),
        ("consecutive_failures", "INTEGER DEFAULT 0"),
        ("last_error", "TEXT"),
        ("last_success_at", "TEXT"),
        ("once", "INTEGER DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE scheduled_jobs ADD COLUMN {col} {col_type}")
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
    scheduled_job_id: int | None = None,
    command: str | None = None,
    queue: str = "foreground",
) -> int:
    """Create a new task and return its ID."""
    cursor = conn.execute(
        """
        INSERT INTO tasks (
            prompt, command, user_id, source_type, conversation_token,
            parent_task_id, is_group_chat, attachments, priority, scheduled_for,
            output_target, talk_message_id, reply_to_talk_id, reply_to_content,
            heartbeat_silent, scheduled_job_id, queue
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            scheduled_job_id,
            queue,
        ),
    )
    task_id = cursor.fetchone()[0]
    logger.debug("Created task %d for user %s (source: %s)", task_id, user_id, source_type)
    return task_id


def _row_to_task(row: sqlite3.Row) -> Task:
    """Convert a database row to a Task object."""
    return Task(
        id=row["id"],
        status=row["status"],
        source_type=row["source_type"],
        user_id=row["user_id"],
        prompt=row["prompt"],
        command=row["command"] if "command" in row.keys() else None,
        conversation_token=row["conversation_token"],
        parent_task_id=row["parent_task_id"],
        is_group_chat=bool(row["is_group_chat"]),
        attachments=json.loads(row["attachments"]) if row["attachments"] else None,
        result=row["result"] if "result" in row.keys() else None,
        actions_taken=row["actions_taken"] if "actions_taken" in row.keys() else None,
        error=row["error"] if "error" in row.keys() else None,
        confirmation_prompt=row["confirmation_prompt"] if "confirmation_prompt" in row.keys() else None,
        priority=row["priority"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        created_at=row["created_at"],
        scheduled_for=row["scheduled_for"],
        output_target=row["output_target"],
        talk_message_id=row["talk_message_id"] if "talk_message_id" in row.keys() else None,
        talk_response_id=row["talk_response_id"] if "talk_response_id" in row.keys() else None,
        reply_to_talk_id=row["reply_to_talk_id"] if "reply_to_talk_id" in row.keys() else None,
        reply_to_content=row["reply_to_content"] if "reply_to_content" in row.keys() else None,
        heartbeat_silent=bool(row["heartbeat_silent"]) if "heartbeat_silent" in row.keys() else False,
        scheduled_job_id=row["scheduled_job_id"] if "scheduled_job_id" in row.keys() else None,
        queue=row["queue"] if "queue" in row.keys() else "foreground",
    )


def claim_task(
    conn: sqlite3.Connection,
    worker_id: str,
    max_retry_age_minutes: int = 60,
    user_id: str | None = None,
    queue: str | None = None,
) -> Task | None:
    """Atomically claim the next available task. Returns None if no tasks available.

    Args:
        worker_id: Unique identifier for the claiming worker.
        max_retry_age_minutes: Tasks older than this are failed instead of retried.
        user_id: If provided, only claim tasks for this user.
        queue: If provided, only claim tasks in this queue ('foreground' or 'background').
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
        """
        UPDATE tasks
        SET status = 'failed', error = 'Task too old to retry (stuck running)'
        WHERE status = 'running'
        AND started_at < datetime('now', '-15 minutes')
        AND created_at < datetime('now', ? || ' minutes')
        """,
        (f"-{max_retry_age_minutes}",),
    )

    # Release recent stuck 'running' tasks for retry
    conn.execute(
        """
        UPDATE tasks
        SET status = 'pending', started_at = NULL, locked_at = NULL, locked_by = NULL,
            attempt_count = attempt_count + 1
        WHERE status = 'running'
        AND started_at < datetime('now', '-15 minutes')
        AND created_at >= datetime('now', ? || ' minutes')
        AND attempt_count < max_attempts
        """,
        (f"-{max_retry_age_minutes}",),
    )

    # Mark stuck 'running' tasks as failed if they've exhausted retries
    conn.execute(
        """
        UPDATE tasks
        SET status = 'failed', error = 'Task stuck in running state - worker may have crashed'
        WHERE status = 'running'
        AND started_at < datetime('now', '-15 minutes')
        AND attempt_count >= max_attempts
        """
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
        RETURNING id, status, source_type, user_id, prompt, command,
                  conversation_token,
                  parent_task_id, is_group_chat, attachments, priority,
                  attempt_count, max_attempts, created_at, scheduled_for,
                  output_target, talk_message_id, talk_response_id,
                  reply_to_talk_id, reply_to_content,
                  heartbeat_silent, scheduled_job_id, queue
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
        """
        SELECT id, status, source_type, user_id, prompt, command,
               conversation_token,
               parent_task_id, is_group_chat, attachments, result, actions_taken, error,
               confirmation_prompt, priority, attempt_count, max_attempts,
               created_at, scheduled_for, output_target,
               talk_message_id, talk_response_id, reply_to_talk_id, reply_to_content,
               heartbeat_silent, scheduled_job_id, queue
        FROM tasks WHERE id = ?
        """,
        (task_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None

    return _row_to_task(row)


def update_task_status(
    conn: sqlite3.Connection,
    task_id: int,
    status: str,
    result: str | None = None,
    error: str | None = None,
    actions_taken: str | None = None,
) -> None:
    """Update task status and optionally result/error."""
    now = datetime.now().isoformat()
    if status == "running":
        conn.execute(
            "UPDATE tasks SET status = ?, started_at = ?, updated_at = ? WHERE id = ?",
            (status, now, now, task_id),
        )
    elif status == "completed":
        conn.execute(
            "UPDATE tasks SET status = ?, completed_at = ?, result = ?, actions_taken = ?, updated_at = ? WHERE id = ?",
            (status, now, result, actions_taken, now, task_id),
        )
    elif status == "failed":
        conn.execute(
            "UPDATE tasks SET status = ?, completed_at = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, now, error, now, task_id),
        )
    else:
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, task_id),
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


def get_pending_confirmation(
    conn: sqlite3.Connection,
    conversation_token: str,
) -> Task | None:
    """
    Get a task that is pending confirmation for a conversation.

    Returns the most recent task awaiting confirmation, or None if none found.
    """
    cursor = conn.execute(
        """
        SELECT id, status, source_type, user_id, prompt, conversation_token,
               parent_task_id, is_group_chat, attachments, result, error,
               confirmation_prompt, priority, attempt_count, max_attempts,
               created_at, scheduled_for, output_target,
               talk_message_id, talk_response_id, reply_to_talk_id, reply_to_content,
               heartbeat_silent
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


def get_user_resources(
    conn: sqlite3.Connection,
    user_id: str,
    resource_type: str | None = None,
) -> list[UserResource]:
    """Get resources accessible to a user."""
    if resource_type:
        cursor = conn.execute(
            """
            SELECT id, user_id, resource_type, resource_path, display_name, permissions
            FROM user_resources
            WHERE user_id = ? AND resource_type = ?
            """,
            (user_id, resource_type),
        )
    else:
        cursor = conn.execute(
            """
            SELECT id, user_id, resource_type, resource_path, display_name, permissions
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
        )
        for row in cursor.fetchall()
    ]


def add_user_resource(
    conn: sqlite3.Connection,
    user_id: str,
    resource_type: str,
    resource_path: str,
    display_name: str | None = None,
    permissions: str = "read",
) -> int:
    """Add a resource permission for a user."""
    cursor = conn.execute(
        """
        INSERT INTO user_resources (user_id, resource_type, resource_path, display_name, permissions)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (user_id, resource_type, resource_path) DO UPDATE SET
            display_name = excluded.display_name,
            permissions = excluded.permissions
        RETURNING id
        """,
        (user_id, resource_type, resource_path, display_name, permissions),
    )
    return cursor.fetchone()[0]


def get_briefing_configs(
    conn: sqlite3.Connection,
    user_id: str | None = None,
    enabled_only: bool = True,
) -> list[BriefingConfig]:
    """Get briefing configurations."""
    query = "SELECT * FROM briefing_configs WHERE 1=1"
    params: list = []

    if user_id:
        query += " AND user_id = ?"
        params.append(user_id)
    if enabled_only:
        query += " AND enabled = 1"

    cursor = conn.execute(query, params)
    return [
        BriefingConfig(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            cron_expression=row["cron_expression"],
            conversation_token=row["conversation_token"],
            components=json.loads(row["components"]),
            enabled=bool(row["enabled"]),
            last_run_at=row["last_run_at"],
        )
        for row in cursor.fetchall()
    ]


def add_briefing_config(
    conn: sqlite3.Connection,
    user_id: str,
    name: str,
    cron_expression: str,
    conversation_token: str,
    components: dict,
) -> int:
    """Add or update a briefing configuration."""
    cursor = conn.execute(
        """
        INSERT INTO briefing_configs (user_id, name, cron_expression, conversation_token, components)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (user_id, name) DO UPDATE SET
            cron_expression = excluded.cron_expression,
            conversation_token = excluded.conversation_token,
            components = excluded.components
        RETURNING id
        """,
        (user_id, name, cron_expression, conversation_token, json.dumps(components)),
    )
    return cursor.fetchone()[0]


def update_briefing_last_run(conn: sqlite3.Connection, briefing_id: int) -> None:
    """Update the last run timestamp for a briefing (legacy DB-based briefings)."""
    conn.execute(
        "UPDATE briefing_configs SET last_run_at = datetime('now') WHERE id = ?",
        (briefing_id,),
    )


def get_briefing_last_run(conn: sqlite3.Connection, user_id: str, briefing_name: str) -> str | None:
    """Get the last run timestamp for a config-based briefing."""
    cursor = conn.execute(
        "SELECT last_run_at FROM briefing_state WHERE user_id = ? AND briefing_name = ?",
        (user_id, briefing_name),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def set_briefing_last_run(conn: sqlite3.Connection, user_id: str, briefing_name: str) -> None:
    """Set the last run timestamp for a config-based briefing."""
    conn.execute(
        """
        INSERT INTO briefing_state (user_id, briefing_name, last_run_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT (user_id, briefing_name) DO UPDATE SET
            last_run_at = datetime('now')
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


def get_previous_task(
    conn: sqlite3.Connection,
    conversation_token: str,
    exclude_task_id: int | None = None,
) -> ConversationMessage | None:
    """
    Get the single most recent completed task in a conversation,
    regardless of source_type.

    Used to ensure the immediately previous message is always available
    in context even when its source_type would normally be excluded.
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

    query += " ORDER BY created_at DESC, id DESC LIMIT 1"

    cursor = conn.execute(query, params)
    row = cursor.fetchone()
    if row is None:
        return None

    return ConversationMessage(
        id=row["id"],
        prompt=row["prompt"],
        result=row["result"],
        created_at=row["created_at"],
        actions_taken=row["actions_taken"] if "actions_taken" in row.keys() else None,
        source_type=row["source_type"] if "source_type" in row.keys() else "talk",
        user_id=row["user_id"] if "user_id" in row.keys() else None,
    )


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
) -> int:
    """Record a processed email."""
    cursor = conn.execute(
        """
        INSERT INTO processed_emails (email_id, sender_email, subject, thread_id, message_id, "references", user_id, task_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (email_id, sender_email, subject, thread_id, message_id, references, user_id, task_id),
    )
    return cursor.fetchone()[0]


def get_email_for_task(conn: sqlite3.Connection, task_id: int) -> ProcessedEmail | None:
    """Get the original email info for a task."""
    cursor = conn.execute(
        """
        SELECT id, email_id, sender_email, subject, thread_id, message_id, "references", user_id, task_id, processed_at
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
    )


# ============================================================================
# Talk message tracking functions
# ============================================================================


def update_task_pid(conn: sqlite3.Connection, task_id: int, pid: int) -> None:
    """Store the subprocess PID for a running task."""
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (pid, task_id))
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
        """
        SELECT id, status, source_type, user_id, prompt, conversation_token,
               parent_task_id, is_group_chat, attachments, result, error,
               confirmation_prompt, priority, attempt_count, max_attempts,
               created_at, scheduled_for, output_target,
               talk_message_id, talk_response_id, reply_to_talk_id, reply_to_content,
               heartbeat_silent
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
        """
        SELECT id, status, source_type, user_id, prompt, conversation_token,
               parent_task_id, is_group_chat, attachments, result, error,
               confirmation_prompt, priority, attempt_count, max_attempts,
               created_at, scheduled_for, output_target,
               talk_message_id, talk_response_id, reply_to_talk_id, reply_to_content,
               heartbeat_silent
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
    now = datetime.now().isoformat()
    if status == "in_progress":
        conn.execute(
            "UPDATE istota_file_tasks SET status = ?, started_at = ? WHERE id = ?",
            (status, now, istota_task_id),
        )
    elif status == "completed":
        conn.execute(
            """
            UPDATE istota_file_tasks
            SET status = ?, completed_at = ?, result_summary = ?
            WHERE id = ?
            """,
            (status, now, result_summary, istota_task_id),
        )
    elif status == "failed":
        conn.execute(
            """
            UPDATE istota_file_tasks
            SET status = ?, completed_at = ?, error_message = ?,
                attempt_count = attempt_count + 1
            WHERE id = ?
            """,
            (status, now, error_message, istota_task_id),
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
               silent_unless_action, consecutive_failures, last_error, last_success_at,
               once
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
               silent_unless_action, consecutive_failures, last_error, last_success_at,
               once
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
        consecutive_failures=row["consecutive_failures"] if "consecutive_failures" in row.keys() else 0,
        last_error=row["last_error"] if "last_error" in row.keys() else None,
        last_success_at=row["last_success_at"] if "last_success_at" in row.keys() else None,
        once=bool(row["once"]) if "once" in row.keys() else False,
    )


def set_scheduled_job_last_run(conn: sqlite3.Connection, job_id: int) -> None:
    """Update last_run_at to now for a scheduled job."""
    conn.execute(
        "UPDATE scheduled_jobs SET last_run_at = datetime('now') WHERE id = ?",
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
               silent_unless_action, consecutive_failures, last_error, last_success_at,
               once
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
    """Enable a scheduled job and reset failure count."""
    conn.execute(
        """
        UPDATE scheduled_jobs
        SET enabled = 1, consecutive_failures = 0, last_error = NULL
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
               silent_unless_action, consecutive_failures, last_error, last_success_at,
               once
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
    query = """
        SELECT id, status, source_type, user_id, prompt, conversation_token,
               parent_task_id, is_group_chat, attachments, result, error,
               confirmation_prompt, priority, attempt_count, max_attempts,
               created_at, scheduled_for, output_target,
               talk_message_id, talk_response_id, reply_to_talk_id, reply_to_content,
               heartbeat_silent
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
    query = """
        SELECT id, status, source_type, user_id, prompt, conversation_token,
               parent_task_id, is_group_chat, attachments, result, error,
               confirmation_prompt, priority, attempt_count, max_attempts,
               created_at, scheduled_for, output_target,
               talk_message_id, talk_response_id, reply_to_talk_id, reply_to_content,
               heartbeat_silent
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
# Invoice schedule state functions
# ============================================================================


@dataclass
class InvoiceScheduleState:
    """State for scheduled invoice generation/reminders."""
    user_id: str
    client_key: str
    last_reminder_at: str | None
    last_generation_at: str | None


def get_invoice_schedule_state(
    conn: sqlite3.Connection,
    user_id: str,
    client_key: str,
) -> InvoiceScheduleState | None:
    """Get the schedule state for a user/client pair."""
    cursor = conn.execute(
        """
        SELECT user_id, client_key, last_reminder_at, last_generation_at
        FROM invoice_schedule_state
        WHERE user_id = ? AND client_key = ?
        """,
        (user_id, client_key),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return InvoiceScheduleState(
        user_id=row["user_id"],
        client_key=row["client_key"],
        last_reminder_at=row["last_reminder_at"],
        last_generation_at=row["last_generation_at"],
    )


def set_invoice_schedule_reminder(
    conn: sqlite3.Connection,
    user_id: str,
    client_key: str,
) -> None:
    """Record that a reminder was sent for this user/client."""
    conn.execute(
        """
        INSERT INTO invoice_schedule_state (user_id, client_key, last_reminder_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT (user_id, client_key) DO UPDATE SET
            last_reminder_at = datetime('now')
        """,
        (user_id, client_key),
    )


def set_invoice_schedule_generation(
    conn: sqlite3.Connection,
    user_id: str,
    client_key: str,
) -> None:
    """Record that invoices were generated for this user/client."""
    conn.execute(
        """
        INSERT INTO invoice_schedule_state (user_id, client_key, last_generation_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT (user_id, client_key) DO UPDATE SET
            last_generation_at = datetime('now')
        """,
        (user_id, client_key),
    )


def get_notified_overdue_invoices(
    conn: sqlite3.Connection,
    user_id: str,
) -> set[str]:
    """Return set of invoice numbers already notified as overdue for this user."""
    cursor = conn.execute(
        "SELECT invoice_number FROM invoice_overdue_notified WHERE user_id = ?",
        (user_id,),
    )
    return {row["invoice_number"] for row in cursor.fetchall()}


def mark_invoice_overdue_notified(
    conn: sqlite3.Connection,
    user_id: str,
    invoice_number: str,
) -> None:
    """Record that an overdue notification was sent for this invoice."""
    conn.execute(
        """
        INSERT OR IGNORE INTO invoice_overdue_notified (user_id, invoice_number)
        VALUES (?, ?)
        """,
        (user_id, invoice_number),
    )


def clear_overdue_notification(
    conn: sqlite3.Connection,
    user_id: str,
    invoice_number: str,
) -> None:
    """Remove overdue notification record (e.g. when invoice is paid)."""
    conn.execute(
        "DELETE FROM invoice_overdue_notified WHERE user_id = ? AND invoice_number = ?",
        (user_id, invoice_number),
    )


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
# Feed polling functions
# ============================================================================


@dataclass
class FeedState:
    """State for a single feed's polling progress."""
    user_id: str
    feed_name: str
    last_poll_at: str | None
    last_item_id: str | None
    etag: str | None
    last_modified: str | None
    consecutive_errors: int
    last_error: str | None


@dataclass
class FeedItem:
    """A single item from a feed."""
    id: int
    user_id: str
    feed_name: str
    item_id: str
    title: str | None
    url: str | None
    content_text: str | None
    content_html: str | None
    image_url: str | None
    author: str | None
    published_at: str | None
    fetched_at: str | None


def get_feed_state(
    conn: sqlite3.Connection,
    user_id: str,
    feed_name: str,
) -> FeedState | None:
    """Get the polling state for a feed."""
    cursor = conn.execute(
        """
        SELECT user_id, feed_name, last_poll_at, last_item_id,
               etag, last_modified, consecutive_errors, last_error
        FROM feed_state
        WHERE user_id = ? AND feed_name = ?
        """,
        (user_id, feed_name),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return FeedState(
        user_id=row["user_id"],
        feed_name=row["feed_name"],
        last_poll_at=row["last_poll_at"],
        last_item_id=row["last_item_id"],
        etag=row["etag"],
        last_modified=row["last_modified"],
        consecutive_errors=row["consecutive_errors"],
        last_error=row["last_error"],
    )


def update_feed_state(
    conn: sqlite3.Connection,
    user_id: str,
    feed_name: str,
    **kwargs,
) -> None:
    """Update feed polling state. Creates if not exists."""
    # Build SET clause from kwargs
    valid_fields = {
        "last_poll_at", "last_item_id", "etag", "last_modified",
        "consecutive_errors", "last_error",
    }
    updates = {k: v for k, v in kwargs.items() if k in valid_fields}
    if not updates:
        return

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values())

    conn.execute(
        f"""
        INSERT INTO feed_state (user_id, feed_name, {", ".join(updates.keys())})
        VALUES (?, ?, {", ".join("?" for _ in updates)})
        ON CONFLICT (user_id, feed_name) DO UPDATE SET
            {set_clause}
        """,
        [user_id, feed_name] + values + values,
    )


def feed_item_exists(
    conn: sqlite3.Connection,
    user_id: str,
    feed_name: str,
    item_id: str,
) -> bool:
    """Check if a feed item already exists."""
    cursor = conn.execute(
        "SELECT 1 FROM feed_items WHERE user_id = ? AND feed_name = ? AND item_id = ?",
        (user_id, feed_name, item_id),
    )
    return cursor.fetchone() is not None


def insert_feed_item(
    conn: sqlite3.Connection,
    user_id: str,
    feed_name: str,
    item_id: str,
    title: str | None = None,
    url: str | None = None,
    content_text: str | None = None,
    content_html: str | None = None,
    image_url: str | None = None,
    author: str | None = None,
    published_at: str | None = None,
) -> bool:
    """Insert a feed item. Returns False on duplicate (already exists)."""
    try:
        conn.execute(
            """
            INSERT INTO feed_items (
                user_id, feed_name, item_id, title, url,
                content_text, content_html, image_url, author, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, feed_name, item_id, title, url,
             content_text, content_html, image_url, author, published_at),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def get_feed_items(
    conn: sqlite3.Connection,
    user_id: str,
    limit: int = 200,
    max_age_days: int = 0,
) -> list[FeedItem]:
    """Get recent feed items for a user, newest first.

    If max_age_days > 0, only return items fetched within that many days.
    """
    if max_age_days > 0:
        query = """
            SELECT id, user_id, feed_name, item_id, title, url,
                   content_text, content_html, image_url, author,
                   published_at, fetched_at
            FROM feed_items
            WHERE user_id = ? AND fetched_at >= datetime('now', ? || ' days')
            ORDER BY COALESCE(published_at, fetched_at) DESC
            LIMIT ?
        """
        params = (user_id, f"-{max_age_days}", limit)
    else:
        query = """
            SELECT id, user_id, feed_name, item_id, title, url,
                   content_text, content_html, image_url, author,
                   published_at, fetched_at
            FROM feed_items
            WHERE user_id = ?
            ORDER BY COALESCE(published_at, fetched_at) DESC
            LIMIT ?
        """
        params = (user_id, limit)
    cursor = conn.execute(query, params)
    return [
        FeedItem(
            id=row["id"],
            user_id=row["user_id"],
            feed_name=row["feed_name"],
            item_id=row["item_id"],
            title=row["title"],
            url=row["url"],
            content_text=row["content_text"],
            content_html=row["content_html"],
            image_url=row["image_url"],
            author=row["author"],
            published_at=row["published_at"],
            fetched_at=row["fetched_at"],
        )
        for row in cursor.fetchall()
    ]


def cleanup_old_feed_items(conn: sqlite3.Connection, retention_days: int = 30) -> int:
    """Delete feed items older than retention_days. Returns count deleted."""
    cursor = conn.execute(
        """
        DELETE FROM feed_items
        WHERE fetched_at < datetime('now', ? || ' days')
        """,
        (f"-{retention_days}",),
    )
    return cursor.rowcount
