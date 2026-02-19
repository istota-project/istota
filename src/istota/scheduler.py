"""Task scheduler - processes pending tasks and briefings."""

import asyncio
import fcntl
import json
import logging
import os
import random
import re
import signal
import socket
import subprocess
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

logger = logging.getLogger("istota.scheduler")

from . import db
from .briefing import build_briefing_prompt
from .briefing_loader import get_briefings_for_user
from .config import Config, load_config
from .executor import execute_task, parse_api_error
from .nextcloud_api import hydrate_user_configs
from .talk import TalkClient, split_message
from .email_poller import get_email_config
from .skills.email import reply_to_email
from .storage import ensure_user_directories_v2
from .tasks_file_poller import handle_tasks_file_completion

def _now(tz=None):
    """Current time — thin wrapper for testability."""
    return datetime.now(tz)


# Pattern to detect the start of actual briefing content (emoji section header)
_BRIEFING_SECTION_RE = re.compile(
    r"^[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50]",  # Emoji at start of line
    re.MULTILINE,
)


def strip_briefing_preamble(text: str) -> str:
    """Strip any preamble/reasoning before the first emoji section header.

    Briefings always start with an emoji-prefixed section header (e.g. 📰, 📈, 📅).
    If the model outputs thinking/reasoning before the first section, strip it.
    """
    match = _BRIEFING_SECTION_RE.search(text)
    if match and match.start() > 0:
        stripped = text[match.start():]
        logger.debug(
            "Stripped %d chars of briefing preamble", match.start(),
        )
        return stripped
    return text


# Graceful shutdown flag
_shutdown_requested = False


def _signal_handler(signum, frame):
    """Handle shutdown signals."""
    global _shutdown_requested
    logger.info("Received signal %d, shutting down gracefully...", signum)
    _shutdown_requested = True

# Pattern to detect confirmation requests in Claude's output
CONFIRMATION_PATTERN = re.compile(
    r'(?:'
    r'I need your confirmation|'
    r'Please confirm|'
    r'Reply "?yes"?|'
    r'Reply yes or no|'
    r'Do you want me to proceed|'
    r'Should I proceed|'
    r'Can you confirm'
    r')',
    re.IGNORECASE
)

# Progress messages for Talk acknowledgments
PROGRESS_MESSAGES = [
    "*On it...*",
    "*Working on it...*",
    "*Hmm...*",
    "*Heard, chef...*",
    "*Investigating...*",
    "*Looking into it...*",
    "*Let me check...*",
    "*Give me a moment...*",
    "*Processing...*",
    "*Thinking...*",
    "*One sec...*",
    "*Copy that...*",
    "*Roger...*",
    "*Understood...*",
    "*Affirmative...*",
    "*Analyzing...*",
    "*Considering...*",
    "*Right away...*",
    "*Coming right up...*",
    "*Let me see...*",
    "*The struggle is real...*",
    "*Checking...*",
    "*Thinkifying...*",
    "*Operating...*",
    "*I'll hit you back...*",
    "*Braining...*",
    "*Computing away...*",
    "*Keep your vidcom open...*",
    "*Thought leading...*",
    "*All work no play...*",
    "*Improvising...*",
    "*Jamming...*",
    "*Riffing...*",
    "*Swinging...*",
    "*Grooving...*",
    "*Beboppin'...*",
    "*Noodling...*",
    "*Syncopating...*",
    "*Comping...*",
    "*Soloing...*",
]


def _format_error_for_user(error_text: str) -> str:
    """
    Convert raw error text to a user-friendly message for Talk.

    Handles API errors, OOM, timeouts, and other common failure modes.
    Logs the full error details but returns a friendly message with personality.
    """
    parsed = parse_api_error(error_text)
    if parsed:
        status = parsed["status_code"]
        request_id = parsed.get("request_id")
        # Log full details for debugging
        logger.debug(
            "API error for user message: status=%d, request_id=%s, message=%s",
            status, request_id, parsed.get("message"),
        )
        if status >= 500 or status == 529:
            return "Lost contact with the mothership. Anthropic's having a moment — try again shortly."
        elif status == 429:
            return "Being throttled by the mothership. Apparently I'm too chatty. Give it a minute."
        elif status in (401, 403):
            return "Can't authenticate with Anthropic — I've been locked out of my own brain. This needs human intervention."
        else:
            return "Something went wrong talking to Anthropic. The void stared back. Try again?"

    # Non-API errors: strip technical details, keep it friendly
    if "killed (likely out of memory)" in error_text:
        return "Ran out of memory — tried to hold too much in my head at once. Try something simpler?"
    if "timed out" in error_text.lower():
        return "Got lost in thought and timed out. Maybe break this into smaller pieces?"

    # Generic fallback - don't expose raw error
    return "Something went sideways and I'm not entirely sure what. Try again?"


def _strip_action_prefix(result: str) -> tuple[bool, str]:
    """Parse ACTION:/NO_ACTION: prefixes from a silent task result.

    Returns (should_post, result_to_post). If ACTION: found, strips prefix
    and returns True. If NO_ACTION: found, returns False. If no prefix,
    returns True with original result (fail-safe: post as-is).
    """
    has_no_action = "NO_ACTION:" in result
    has_action = result.startswith("ACTION:") or "\nACTION:" in result

    if has_action:
        if result.startswith("ACTION:"):
            return True, result[len("ACTION:"):].strip()
        idx = result.find("\nACTION:")
        return True, result[idx + len("\nACTION:"):].strip()
    elif has_no_action:
        return False, result
    else:
        # No prefix — post as-is (fail-safe)
        return True, result


def download_talk_attachments(config: Config, attachments: list[str]) -> list[str]:
    """
    Get local paths for Talk attachments.

    Talk attachments arrive as Nextcloud paths (e.g., "Talk/filename.jpg").

    If using mount:
        Returns mount paths directly (no download needed).
    If using rclone:
        Downloads to temp directory before Claude Code execution.

    Returns list of local paths (or original paths as fallback on error).
    """
    if not attachments:
        return []

    local_paths = []
    for att in attachments:
        if att.startswith("Talk/"):
            if config.use_mount:
                # Use mount path directly - no download needed
                mount_path = config.nextcloud_mount_path / att
                if mount_path.exists():
                    local_paths.append(str(mount_path))
                    logger.debug(f"Talk attachment via mount: {att} -> {mount_path}")
                else:
                    logger.warning(f"Talk attachment not found at mount path: {mount_path}")
                    local_paths.append(att)  # Fall back to original path
            else:
                # Download via rclone to temp directory
                config.temp_dir.mkdir(parents=True, exist_ok=True)
                remote_path = f"{config.rclone_remote}:{att}"
                result = subprocess.run(
                    ["rclone", "copy", remote_path, str(config.temp_dir)],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    # rclone copy preserves filename, so actual file is temp_dir/filename
                    actual_path = config.temp_dir / Path(att).name
                    if actual_path.exists():
                        local_paths.append(str(actual_path))
                        logger.debug(f"Downloaded Talk attachment: {att} -> {actual_path}")
                    else:
                        logger.warning(f"Downloaded file not found: {actual_path}")
                        local_paths.append(att)  # Fall back to original path
                else:
                    logger.warning(f"Failed to download {att}: {result.stderr}")
                    local_paths.append(att)  # Fall back to original path
        else:
            local_paths.append(att)

    return local_paths


def get_worker_id(user_id: str | None = None) -> str:
    """Generate a unique worker ID, optionally scoped to a user."""
    base = f"{socket.gethostname()}-{os.getpid()}"
    if user_id is not None:
        return f"{base}-{user_id}"
    return base


def _make_talk_progress_callback(config: Config, task: db.Task):
    """Build a rate-limited progress callback that posts updates to Talk."""
    last_send = time.time()  # starts from initial ack message
    send_count = 0
    sched = config.scheduler

    def callback(message: str):
        nonlocal last_send, send_count
        if send_count >= sched.progress_max_messages:
            return
        now = time.time()
        if now - last_send < sched.progress_min_interval:
            return
        # Split emoji prefix from description so only text is italicised
        msg = message[:200]
        if msg and not msg[0].isascii():
            # First char is emoji — find where the text starts
            parts = msg.split(" ", 1)
            if len(parts) == 2:
                formatted = f"{parts[0]} *{parts[1]}*"
            else:
                formatted = f"*{msg}*"
        else:
            formatted = f"*{msg}*"
        try:
            asyncio.run(post_result_to_talk(config, task, formatted))
            last_send = now
            send_count += 1
            with db.get_db(config.db_path) as conn:
                db.log_task(conn, task.id, "debug", f"Progress: {message[:200]}")
        except Exception as e:
            logger.debug("Progress update failed: %s", e)

    return callback


class UserWorker(threading.Thread):
    """Worker thread that processes tasks for a single user and queue serially."""

    def __init__(self, user_id: str, config: Config, pool: "WorkerPool",
                 queue_type: str = "foreground", slot: int = 0):
        super().__init__(daemon=True, name=f"worker-{user_id}-{queue_type}-{slot}")
        self.user_id = user_id
        self.queue_type = queue_type
        self.slot = slot
        self.config = config
        self.pool = pool
        self._stop_event = threading.Event()

    def run(self) -> None:
        logger.info("Worker started for user %s (%s)", self.user_id, self.queue_type)
        idle_timeout = self.config.scheduler.worker_idle_timeout
        poll_interval = self.config.scheduler.poll_interval
        try:
            while not _shutdown_requested and not self._stop_event.is_set():
                try:
                    result = process_one_task(
                        self.config, user_id=self.user_id, queue=self.queue_type,
                    )
                except Exception as e:
                    logger.error("Worker %s/%s error: %s", self.user_id, self.queue_type, e)
                    result = None

                if result is not None:
                    task_id, success = result
                    status = "completed" if success else "failed"
                    logger.info(
                        "Worker %s/%s: task %d %s",
                        self.user_id, self.queue_type, task_id, status,
                    )
                    # Processed a task — immediately check for more
                    continue

                # No tasks available — wait and check again, or exit on idle timeout
                if self._stop_event.wait(timeout=min(poll_interval, idle_timeout)):
                    break  # stop requested

                # Check if we've been idle too long
                # We use a simple approach: if no task was found, check once more
                # after poll_interval. If still nothing, exit.
                try:
                    result = process_one_task(
                        self.config, user_id=self.user_id, queue=self.queue_type,
                    )
                except Exception as e:
                    logger.error("Worker %s/%s error: %s", self.user_id, self.queue_type, e)
                    result = None

                if result is not None:
                    task_id, success = result
                    status = "completed" if success else "failed"
                    logger.info(
                        "Worker %s/%s: task %d %s",
                        self.user_id, self.queue_type, task_id, status,
                    )
                    continue

                # Still no tasks — exit idle worker
                break
        finally:
            logger.info("Worker exiting for user %s (%s/%d)", self.user_id, self.queue_type, self.slot)
            self.pool._on_worker_exit(self.user_id, self.queue_type, self.slot)

    def request_stop(self) -> None:
        self._stop_event.set()


class WorkerPool:
    """Manages per-user, per-queue worker threads with a concurrency cap.

    Each user can have multiple concurrent workers per queue type, up to their
    per-user cap. Workers are keyed by (user_id, queue_type, slot).
    """

    def __init__(self, config: Config):
        self.config = config
        self._workers: dict[tuple[str, str, int], UserWorker] = {}
        self._lock = threading.Lock()

    def dispatch(self) -> None:
        """Spawn workers for users with pending tasks, prioritizing foreground.

        Three-tier concurrency control:
        1. Instance-level fg cap: max_foreground_workers
        2. Instance-level bg cap: max_background_workers
        3. Per-user caps: effective_user_max_fg_workers / effective_user_max_bg_workers
        """
        with db.get_db(self.config.db_path) as conn:
            fg_users = db.get_users_with_pending_fg_queue_tasks(conn)
            bg_users = db.get_users_with_pending_bg_queue_tasks(conn)
            # Pre-fetch pending task counts for users that may need multiple workers
            fg_pending = {uid: db.count_pending_tasks_for_user_queue(conn, uid, "foreground") for uid in fg_users}
            bg_pending = {uid: db.count_pending_tasks_for_user_queue(conn, uid, "background") for uid in bg_users}

        fg_cap = self.config.scheduler.max_foreground_workers
        bg_cap = self.config.scheduler.max_background_workers

        with self._lock:
            # Phase 1: foreground workers
            active_fg = sum(1 for (_, qt, _) in self._workers if qt == "foreground")
            for user_id in fg_users:
                if active_fg >= fg_cap:
                    break
                user_fg_cap = self.config.effective_user_max_fg_workers(user_id)
                existing_slots = {s for (uid, qt, s) in self._workers if uid == user_id and qt == "foreground"}
                user_fg_active = len(existing_slots)
                pending = fg_pending.get(user_id, 0)
                to_spawn = min(user_fg_cap - user_fg_active, pending)
                available = (s for s in range(user_fg_cap) if s not in existing_slots)
                for slot in available:
                    if to_spawn <= 0 or active_fg >= fg_cap:
                        break
                    key = (user_id, "foreground", slot)
                    worker = UserWorker(user_id, self.config, self, queue_type="foreground", slot=slot)
                    self._workers[key] = worker
                    worker.start()
                    logger.info("Spawned foreground worker for user %s (slot %d)", user_id, slot)
                    active_fg += 1
                    to_spawn -= 1

            # Phase 2: background workers
            active_bg = sum(1 for (_, qt, _) in self._workers if qt == "background")
            for user_id in bg_users:
                if active_bg >= bg_cap:
                    break
                user_bg_cap = self.config.effective_user_max_bg_workers(user_id)
                existing_slots = {s for (uid, qt, s) in self._workers if uid == user_id and qt == "background"}
                user_bg_active = len(existing_slots)
                pending = bg_pending.get(user_id, 0)
                to_spawn = min(user_bg_cap - user_bg_active, pending)
                available = (s for s in range(user_bg_cap) if s not in existing_slots)
                for slot in available:
                    if to_spawn <= 0 or active_bg >= bg_cap:
                        break
                    key = (user_id, "background", slot)
                    worker = UserWorker(user_id, self.config, self, queue_type="background", slot=slot)
                    self._workers[key] = worker
                    worker.start()
                    logger.info("Spawned background worker for user %s (slot %d)", user_id, slot)
                    active_bg += 1
                    to_spawn -= 1

    def _on_worker_exit(self, user_id: str, queue_type: str, slot: int) -> None:
        """Called by a worker thread when it exits."""
        with self._lock:
            self._workers.pop((user_id, queue_type, slot), None)

    def shutdown(self) -> None:
        """Request all workers to stop and wait for them to finish."""
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            w.request_stop()
        for w in workers:
            w.join(timeout=10)

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._workers)


def _process_deferred_subtasks(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> int:
    """Process deferred subtask creation requests from JSON file.

    Returns count of subtasks created.
    """
    path = user_temp_dir / f"task_{task.id}_subtasks.json"
    if not path.exists():
        return 0

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Bad deferred subtasks file for task %d: %s", task.id, e)
        path.unlink(missing_ok=True)
        return 0

    # Admin-only: non-admin users cannot create subtasks
    if not config.is_admin(task.user_id):
        logger.warning(
            "Non-admin user %s attempted deferred subtask creation (task %d), ignoring",
            task.user_id, task.id,
        )
        path.unlink(missing_ok=True)
        return 0

    count = 0
    with db.get_db(config.db_path) as conn:
        for entry in data:
            prompt = entry.get("prompt", "")
            if not prompt:
                continue
            db.create_task(
                conn,
                prompt=prompt,
                user_id=task.user_id,
                source_type="subtask",
                parent_task_id=task.id,
                conversation_token=entry.get("conversation_token", task.conversation_token),
                priority=entry.get("priority", 5),
                queue=task.queue,
            )
            count += 1

    if count:
        logger.info("Created %d deferred subtasks for task %d", count, task.id)
    path.unlink(missing_ok=True)
    return count


def _process_deferred_tracking(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> int:
    """Process deferred transaction tracking requests from JSON file.

    Returns count of items processed.
    """
    path = user_temp_dir / f"task_{task.id}_tracked_transactions.json"
    if not path.exists():
        return 0

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Bad deferred tracking file for task %d: %s", task.id, e)
        path.unlink(missing_ok=True)
        return 0

    count = 0
    with db.get_db(config.db_path) as conn:
        monarch_synced = data.get("monarch_synced", [])
        if monarch_synced:
            count += db.track_monarch_transactions_batch(conn, task.user_id, monarch_synced)

        csv_imported = data.get("csv_imported", [])
        if csv_imported:
            hashes = [e["content_hash"] for e in csv_imported if "content_hash" in e]
            source_file = csv_imported[0].get("source_file") if csv_imported else None
            count += db.track_csv_transactions_batch(conn, task.user_id, hashes, source_file)

        for txn_id in data.get("monarch_recategorized", []):
            if db.mark_monarch_transaction_recategorized(conn, task.user_id, txn_id):
                count += 1

        for update in data.get("monarch_category_updates", []):
            if db.update_monarch_transaction_posted_account(
                conn, task.user_id,
                update["monarch_transaction_id"],
                update["posted_account"],
            ):
                count += 1

    if count:
        logger.info("Processed %d deferred tracking entries for task %d", count, task.id)
    path.unlink(missing_ok=True)
    return count


def _execute_command_task(
    task: db.Task, config: Config,
) -> tuple[bool, str]:
    """Execute a shell command task via subprocess.

    Returns (success, result) — same interface as execute_task().
    """
    timeout = config.scheduler.task_timeout_minutes * 60

    from .executor import build_stripped_env
    env = build_stripped_env()
    env["ISTOTA_TASK_ID"] = str(task.id)
    env["ISTOTA_USER_ID"] = task.user_id
    if config.db_path:
        env["ISTOTA_DB_PATH"] = str(config.db_path)
    if config.nextcloud_mount_path:
        env["NEXTCLOUD_MOUNT_PATH"] = str(config.nextcloud_mount_path)
    if task.conversation_token:
        env["ISTOTA_CONVERSATION_TOKEN"] = task.conversation_token

    try:
        proc = subprocess.run(
            task.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(config.temp_dir),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {config.scheduler.task_timeout_minutes} minutes"
    except Exception as e:
        return False, f"Command execution error: {e}"

    if proc.returncode == 0:
        result = proc.stdout.strip() if proc.stdout else "(no output)"
        return True, result
    else:
        error = proc.stderr.strip() if proc.stderr else f"Exit code {proc.returncode}"
        return False, error


def process_one_task(
    config: Config, dry_run: bool = False, user_id: str | None = None,
    queue: str | None = None,
) -> tuple[int, bool] | None:
    """
    Claim and process one pending task.
    Returns (task_id, success) or None if no tasks available.

    Args:
        user_id: If provided, only claim tasks for this user.
        queue: If provided, only claim tasks in this queue ('foreground' or 'background').
    """
    worker_id = get_worker_id(user_id)

    with db.get_db(config.db_path) as conn:
        # Claim a task
        task = db.claim_task(
            conn, worker_id, config.scheduler.max_retry_age_minutes,
            user_id=user_id, queue=queue,
        )
        if not task:
            return None

        task_id = task.id
        db.log_task(conn, task_id, "info", f"Task claimed by {worker_id}")

        # Update to running
        db.update_task_status(conn, task_id, "running")

        # Get user resources
        user_resources = db.get_user_resources(conn, task.user_id)

    # Command tasks skip Talk ack, attachment download, and resource loading
    if task.command:
        success, result = _execute_command_task(task, config)
        actions_taken = None
    else:
        # Send progress update for Talk tasks
        progress_callback = None
        is_rerun = task.attempt_count > 0 or task.confirmation_prompt is not None
        if task.source_type == "talk" and task.conversation_token and not dry_run:
            if not is_rerun:
                asyncio.run(post_result_to_talk(config, task, random.choice(PROGRESS_MESSAGES)))

            # Build streaming progress callback if enabled
            if config.scheduler.progress_updates:
                progress_callback = _make_talk_progress_callback(config, task)

        # Download Talk attachments to local filesystem before execution
        if task.source_type == "talk" and task.attachments:
            local_attachments = download_talk_attachments(config, task.attachments)
            # Create modified task with local paths
            task = replace(task, attachments=local_attachments)

        # Execute the task (outside the db context to avoid long locks)
        success, result, actions_taken = execute_task(
            task, config, user_resources, dry_run=dry_run, on_progress=progress_callback,
        )

    # Track if we need to call istota_file handler after db connection closes
    call_file_handler = False
    file_handler_success = False
    post_ntfy = False

    # Resolve effective output target: explicit field > inferred from source_type
    target = task.output_target
    if not target:
        if task.source_type in ("talk", "briefing"):
            target = "talk"
        elif task.source_type == "email":
            target = "email"
        elif task.source_type == "istota_file":
            target = "istota_file"

    # Track what to post after DB transaction closes
    post_talk_message = None
    post_email = False
    is_failure_notify = False

    with db.get_db(config.db_path) as conn:
        if success:
            # Check if the result is a confirmation request
            is_confirmation_request = (
                target in ("talk", "both")
                and task.conversation_token
                and CONFIRMATION_PATTERN.search(result)
            )

            if is_confirmation_request:
                # Set task to pending confirmation instead of completing
                db.set_task_confirmation(conn, task_id, result)
                db.log_task(conn, task_id, "info", "Task awaiting user confirmation")
                post_talk_message = result
            else:
                db.update_task_status(conn, task_id, "completed", result=result, actions_taken=actions_taken)
                db.log_task(conn, task_id, "info", "Task completed successfully")

                # Index conversation for memory search (non-critical)
                if config.memory_search.enabled and config.memory_search.auto_index_conversations:
                    try:
                        from .memory_search import index_conversation as _index_conv
                        _index_conv(conn, task.user_id, task_id, task.prompt, result)
                        # Also index under channel namespace if in a channel
                        if task.conversation_token:
                            channel_uid = f"channel:{task.conversation_token}"
                            _index_conv(conn, channel_uid, task_id, task.prompt, result)
                    except Exception as e:
                        logger.debug("Memory search indexing failed for task %s: %s", task_id, e)

                if task.heartbeat_silent:
                    # Silent scheduled job — ACTION/NO_ACTION logic
                    should_post, result_to_post = _strip_action_prefix(result)
                    if should_post:
                        db.log_task(conn, task_id, "info", "Silent scheduled job: action taken")
                        if task.conversation_token:
                            post_talk_message = result_to_post
                    else:
                        db.log_task(conn, task_id, "info", "Silent scheduled job: no action needed")

                else:
                    # Non-heartbeat, non-silent task: normal delivery logic
                    # Strip preamble from briefing results (model sometimes adds reasoning)
                    delivery_result = strip_briefing_preamble(result) if task.source_type == "briefing" else result
                    if target in ("talk", "both", "all") and task.conversation_token:
                        post_talk_message = delivery_result
                    if target in ("email", "both", "all"):
                        post_email = True
                    if target in ("ntfy", "all"):
                        post_ntfy = True
                    if target == "istota_file":
                        call_file_handler = True
                        file_handler_success = True

                # Track scheduled job success
                if task.scheduled_job_id:
                    db.reset_scheduled_job_failures(conn, task.scheduled_job_id)
                    # Auto-remove one-time jobs after successful execution
                    job = db.get_scheduled_job(conn, task.scheduled_job_id)
                    if job and job.once:
                        db.delete_scheduled_job(conn, task.scheduled_job_id)
                        logger.info(
                            "One-time job '%s' completed and removed (job_id=%d)",
                            job.name, job.id,
                        )
                        from .cron_loader import remove_job_from_cron_md
                        remove_job_from_cron_md(config, task.user_id, job.name)

        else:
            # Check if we should retry (skip for OOM and cancellation — no point retrying)
            is_oom = "killed (likely out of memory)" in result
            is_cancelled = result == "Cancelled by user"
            if is_cancelled:
                db.update_task_status(conn, task_id, "cancelled", error=result)
                db.log_task(conn, task_id, "info", "Task cancelled by user via !stop")
                # No Talk notification needed — !stop already acknowledged
            elif task.attempt_count < task.max_attempts - 1 and not is_oom:
                # Exponential backoff: 1, 4, 16 minutes
                delay = 1 << (task.attempt_count * 2)
                db.set_task_pending_retry(conn, task_id, result, delay)
                db.log_task(conn, task_id, "warn", f"Task failed, will retry in {delay} minutes: {result[:200]}")
            else:
                db.update_task_status(conn, task_id, "failed", error=result)
                db.log_task(conn, task_id, "error", f"Task failed permanently: {result[:500]}")

                if target in ("talk", "both", "all") and task.conversation_token:
                    # Use user-friendly error message, not raw error
                    friendly_error = _format_error_for_user(result)
                    post_talk_message = f"Sorry, {friendly_error[0].lower()}{friendly_error[1:]}"
                    is_failure_notify = True
                # NOTE: We intentionally do NOT email errors to users.
                # Failed tasks with target="email" or "both" only log the error.
                # Receiving error emails is confusing; users can check Talk or retry.
                if target == "istota_file":
                    call_file_handler = True
                    file_handler_success = False

                # Track scheduled job failure + auto-disable
                if task.scheduled_job_id:
                    fail_count = db.increment_scheduled_job_failures(
                        conn, task.scheduled_job_id, result,
                    )
                    max_failures = config.scheduler.scheduled_job_max_consecutive_failures
                    if max_failures > 0 and fail_count >= max_failures:
                        db.disable_scheduled_job(conn, task.scheduled_job_id)
                        db.log_task(
                            conn, task_id, "warn",
                            f"Scheduled job auto-disabled after {fail_count} consecutive failures",
                        )
                        logger.warning(
                            "Scheduled job %d auto-disabled after %d failures",
                            task.scheduled_job_id, fail_count,
                        )

    # Process deferred operations (subtasks, transaction tracking) on success
    if success and not (
        target in ("talk", "both")
        and task.conversation_token
        and CONFIRMATION_PATTERN.search(result)
    ):
        from .executor import get_user_temp_dir
        user_temp_dir = get_user_temp_dir(config, task.user_id)
        _process_deferred_subtasks(config, task, user_temp_dir)
        _process_deferred_tracking(config, task, user_temp_dir)

    # Deliver results outside DB context to avoid lock conflicts
    if post_talk_message:
        response_msg_id = asyncio.run(post_result_to_talk(
            config, task, post_talk_message, use_reply_threading=True,
        ))
        # Store bot's response message ID for reply tracking
        if response_msg_id and not is_failure_notify:
            try:
                with db.get_db(config.db_path) as conn:
                    db.update_talk_response_id(conn, task_id, response_msg_id)
            except Exception as e:
                logger.debug("Failed to store talk_response_id for task %d: %s", task_id, e)
    if post_email:
        email_result = strip_briefing_preamble(result) if task.source_type == "briefing" else result
        email_ok = asyncio.run(post_result_to_email(config, task, email_result))
        if not email_ok:
            with db.get_db(config.db_path) as conn:
                db.update_task_status(conn, task_id, "failed", error="Email delivery failed")
                db.log_task(conn, task_id, "error", "Task completed but email delivery failed")
    if post_ntfy:
        from .notifications import _send_ntfy
        ntfy_result = strip_briefing_preamble(result) if task.source_type == "briefing" else result
        _send_ntfy(config, task.user_id, ntfy_result, title=f"Task {task_id}")
    if call_file_handler:
        handle_tasks_file_completion(config, task, file_handler_success, result)

    return task_id, success


def _strip_markdown(text: str) -> str:
    """Strip markdown formatting for plain text output (bold, italic, links)."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # **bold**
    text = re.sub(r"\*(.+?)\*", r"\1", text)       # *italic*
    text = re.sub(r"_(.+?)_", r"\1", text)          # _italic_
    text = re.sub(r"==(.+?)==", r"\1", text)         # ==highlight==
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # [text](url)
    return text


async def post_result_to_talk(
    config: Config, task: db.Task, message: str,
    *, use_reply_threading: bool = False,
) -> int | None:
    """Post a result message to Talk. Returns the Talk message ID of the last sent message.

    Long messages are split into multiple parts sent sequentially.
    """
    if not config.nextcloud.url or not task.conversation_token:
        return None

    try:
        client = TalkClient(config)
        parts = split_message(message)
        msg_id = None
        for i, part in enumerate(parts):
            # In group chats, reply to the original message and @mention the user
            # for the first part only so they get a notification.
            # Only applied for final results (use_reply_threading=True), not
            # intermediate progress updates which would be too noisy.
            reply_to = None
            if use_reply_threading and i == 0 and task.is_group_chat:
                reply_to = task.talk_message_id
                part = f"@{task.user_id} {part}"
            response = await client.send_message(
                task.conversation_token, part, reply_to=reply_to,
            )
            msg_id = response.get("ocs", {}).get("data", {}).get("id")
        return msg_id
    except Exception as e:
        # Log but don't fail the task — use Python logger to avoid DB lock issues
        logger.error("Failed to post to Talk (task %s): %s", task.id, e)
        return None


def _parse_email_output(message: str) -> dict:
    """
    Parse Claude Code's email output as JSON.

    Expected format:
        {"subject": "...", "body": "...", "format": "plain"|"html"}

    Handles common Claude quirks:
    - Markdown code fences (```json ... ```)
    - Preamble text before the JSON object
    - Trailing text after the JSON object

    Falls back to treating raw string as plain-text body (backward compatible).
    """
    def _try_parse(text: str) -> dict | None:
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "body" in data and "format" in data:
                fmt = data["format"]
                if fmt not in ("plain", "html"):
                    fmt = "plain"
                return {
                    "subject": data.get("subject"),
                    "body": data["body"],
                    "format": fmt,
                }
        except (json.JSONDecodeError, TypeError, KeyError):
            return None

    text = message.strip()

    # Try 1: parse as-is
    result = _try_parse(text)
    if result:
        return result

    # Try 2: strip markdown code fences
    if "```" in text:
        lines = text.split("\n")
        # Find fenced block
        start = None
        end = None
        for i, line in enumerate(lines):
            if line.strip().startswith("```") and start is None:
                start = i
            elif line.strip() == "```" and start is not None:
                end = i
                break
        if start is not None and end is not None:
            fenced = "\n".join(lines[start + 1:end]).strip()
            result = _try_parse(fenced)
            if result:
                return result

    # Try 3: find outermost { ... } in the message
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace:last_brace + 1]
        result = _try_parse(candidate)
        if result:
            return result

    return {"subject": None, "body": message, "format": "plain"}


async def post_result_to_email(config: Config, task: db.Task, message: str) -> bool:
    """Send task result as email reply, or fresh email for scheduled/briefing jobs.

    Returns True on success, False on failure.
    """
    from .skills.email import send_email

    # Briefing tasks output Talk-formatted text, not JSON — send directly
    if task.source_type == "briefing":
        user_config = config.users.get(task.user_id)
        if not user_config or not user_config.email_addresses:
            logger.warning("No email address for user %s (task %d)", task.user_id, task.id)
            return False
        try:
            email_config = get_email_config(config)
            # Extract briefing type from prompt (e.g. "Generate a morning briefing")
            match = re.search(r"Generate a (\w+) briefing", task.prompt)
            briefing_type = match.group(1).title() if match else ""
            send_email(
                to=user_config.email_addresses[0],
                subject=f"{briefing_type} Briefing".strip(),
                body=_strip_markdown(message),
                config=email_config,
                from_addr=config.email.bot_email,
                content_type="plain",
            )
            return True
        except Exception as e:
            logger.error("Failed to send briefing email (task %s): %s", task.id, e)
            return False

    parsed = _parse_email_output(message)

    with db.get_db(config.db_path) as conn:
        processed_email = db.get_email_for_task(conn, task.id)

    if processed_email:
        # Reply to existing email thread
        try:
            email_config = get_email_config(config)

            # Build References: parent's references + parent's message_id (RFC 5322)
            if processed_email.references and processed_email.message_id:
                references = f"{processed_email.references} {processed_email.message_id}"
            elif processed_email.message_id:
                references = processed_email.message_id
            else:
                references = None

            # Use parsed subject if provided, otherwise keep original
            subject = parsed["subject"] if parsed["subject"] else (processed_email.subject or "")

            reply_to_email(
                to_addr=processed_email.sender_email,
                subject=subject,
                body=parsed["body"],
                config=email_config,
                from_addr=config.email.bot_email,
                in_reply_to=processed_email.message_id,
                references=references,
                content_type=parsed["format"],
            )
            return True
        except Exception as e:
            logger.error("Failed to send email reply (task %s): %s", task.id, e)
            return False
    else:
        # No original email — send fresh email to user (e.g., scheduled job)
        user_config = config.users.get(task.user_id)
        if not user_config or not user_config.email_addresses:
            logger.warning("No email address for user %s (task %d)", task.user_id, task.id)
            return False

        # Use parsed subject if provided, otherwise fall back to prompt excerpt
        subject = parsed["subject"] if parsed["subject"] else f"[{config.bot_name}] {task.prompt[:80]}"

        try:
            email_config = get_email_config(config)
            send_email(
                to=user_config.email_addresses[0],
                subject=subject,
                body=parsed["body"],
                config=email_config,
                from_addr=config.email.bot_email,
                content_type=parsed["format"],
            )
            return True
        except Exception as e:
            logger.error("Failed to send email (task %s): %s", task.id, e)
            return False


def check_briefings(conn, app_config: Config) -> list[int]:
    """
    Check for briefings that should run and queue them as tasks.

    Reads briefing configurations from app_config (config.toml) and tracks
    last_run_at in the database.

    Args:
        conn: Database connection
        app_config: Application config with user briefings

    Returns:
        List of created task IDs
    """
    created_tasks = []

    # Iterate through all users and their briefings (bot config > admin config)
    for user_id, user_config in app_config.users.items():
        briefings = get_briefings_for_user(app_config, user_id)
        if not briefings:
            continue

        user_tz_str = user_config.timezone

        # Get current time in user's timezone
        try:
            user_tz = ZoneInfo(user_tz_str)
        except Exception:
            user_tz = ZoneInfo("UTC")
            user_tz_str = "UTC"

        now = _now(user_tz)

        for briefing in briefings:
            if not briefing.cron:
                continue
            if not briefing.conversation_token and briefing.output in ("talk", "both"):
                continue

            # Check if this briefing should run
            should_run = False
            last_run_at = db.get_briefing_last_run(conn, user_id, briefing.name)

            if last_run_at:
                # Parse last_run_at — DB stores UTC via datetime('now')
                last_run = datetime.fromisoformat(last_run_at)
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=ZoneInfo("UTC"))
                cron = croniter(briefing.cron, last_run.astimezone(user_tz))
                next_run = cron.get_next(datetime)
                should_run = now >= next_run
            else:
                # Never run before - check if we're past the first scheduled time today
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                cron = croniter(briefing.cron, today_start)
                next_run = cron.get_next(datetime)
                should_run = now >= next_run

            if should_run:
                # Build enhanced briefing prompt with pre-fetched data
                prompt = build_briefing_prompt(
                    briefing, user_id, app_config, user_tz_str,
                )

                task_id = db.create_task(
                    conn,
                    prompt=prompt,
                    user_id=user_id,
                    source_type="briefing",
                    conversation_token=briefing.conversation_token,
                    output_target=briefing.output,
                    priority=8,  # Higher priority for briefings
                    queue="background",
                )

                db.set_briefing_last_run(conn, user_id, briefing.name)
                created_tasks.append(task_id)

    return created_tasks


def cleanup_old_temp_files(config: Config, retention_days: int) -> int:
    """
    Delete temp files older than retention_days.

    Iterates into per-user subdirectories under temp_dir.
    All permanent storage should be in Nextcloud, so temp files
    are safe to clean up periodically.

    Returns:
        Number of files deleted.
    """
    if not config.temp_dir.exists():
        return 0

    cutoff = time.time() - (retention_days * 24 * 60 * 60)
    deleted = 0

    def _cleanup_dir(directory: Path) -> int:
        count = 0
        for path in directory.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    count += 1
                elif path.is_dir():
                    # Recurse into user subdirectories
                    count += _cleanup_dir(path)
                    # Remove empty directories
                    try:
                        path.rmdir()  # only succeeds if empty
                    except OSError:
                        pass
            except Exception as e:
                logger.debug(f"Could not process temp path {path}: {e}")
        return count

    deleted = _cleanup_dir(config.temp_dir)
    return deleted


async def run_cleanup_checks(config: Config) -> None:
    """
    Run all cleanup checks for scheduler robustness.
    Call periodically from daemon loop.
    """
    sched = config.scheduler

    with db.get_db(config.db_path) as conn:
        # 1. Expire stale confirmations and notify users via Talk
        expired = db.expire_stale_confirmations(conn, sched.confirmation_timeout_minutes)
        for task_info in expired:
            logger.info(
                f"Expired stale confirmation: task {task_info['id']} "
                f"(user: {task_info['user_id']})"
            )
            # Notify user via Talk if conversation_token is set
            if task_info["conversation_token"] and config.nextcloud.url:
                try:
                    client = TalkClient(config)
                    msg = (
                        "Your pending confirmation request timed out and was cancelled. "
                        "Please submit your request again if you still need this action."
                    )
                    await client.send_message(task_info["conversation_token"], msg)
                except Exception as e:
                    logger.error(f"Failed to notify user about expired confirmation: {e}")

        # 2. Log warnings for stale pending tasks
        stale_tasks = db.get_stale_pending_tasks(conn, sched.stale_pending_warn_minutes)
        for task in stale_tasks:
            logger.warning(
                f"Stale pending task detected: task {task.id} "
                f"(user: {task.user_id}, source: {task.source_type}, "
                f"created: {task.created_at})"
            )

        # 3. Fail ancient pending tasks and notify users
        failed = db.fail_ancient_pending_tasks(conn, sched.stale_pending_fail_hours)
        for task_info in failed:
            logger.warning(
                f"Auto-failed ancient pending task: task {task_info['id']} "
                f"(user: {task_info['user_id']}, source: {task_info['source_type']})"
            )
            # Notify user via Talk if conversation_token is set
            if task_info["conversation_token"] and config.nextcloud.url:
                try:
                    client = TalkClient(config)
                    msg = (
                        "A task you submitted was cancelled because it was pending too long "
                        "without being processed. Please try again or contact support if this "
                        "keeps happening."
                    )
                    await client.send_message(task_info["conversation_token"], msg)
                except Exception as e:
                    logger.error(f"Failed to notify user about failed task: {e}")

        # 4. Clean up old completed tasks
        deleted_count = db.cleanup_old_tasks(conn, sched.task_retention_days)
        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} old task(s)")

    # 5. Clean up old emails from IMAP (outside db context)
    if config.email.enabled and sched.email_retention_days > 0:
        try:
            from .email_poller import cleanup_old_emails
            deleted_emails = cleanup_old_emails(config, sched.email_retention_days)
            if deleted_emails > 0:
                logger.info(f"Deleted {deleted_emails} old email(s) from IMAP inbox")
        except Exception as e:
            logger.error(f"Error cleaning up old emails: {e}")

    # 6. Clean up old feed items
    with db.get_db(config.db_path) as conn:
        deleted_feeds = db.cleanup_old_feed_items(conn, config.scheduler.feed_item_retention_days)
        if deleted_feeds > 0:
            logger.info(f"Cleaned up {deleted_feeds} old feed item(s)")

    # 7. Clean up old temp files
    if sched.temp_file_retention_days > 0:
        try:
            deleted_files = cleanup_old_temp_files(config, sched.temp_file_retention_days)
            if deleted_files > 0:
                logger.info(f"Deleted {deleted_files} old temp file(s)")
        except Exception as e:
            logger.error(f"Error cleaning up temp files: {e}")

    # 8. Clean up old Claude session logs
    if sched.temp_file_retention_days > 0:
        try:
            deleted_logs = cleanup_old_claude_logs(sched.temp_file_retention_days)
            if deleted_logs > 0:
                logger.info(f"Deleted {deleted_logs} old Claude session log(s)")
        except Exception as e:
            logger.error(f"Error cleaning up Claude logs: {e}")


def cleanup_old_claude_logs(retention_days: int) -> int:
    """
    Delete old Claude session logs from ~/.claude/{projects,debug,todos}.

    Returns count of deleted files.
    """
    home = Path(os.environ.get("HOME", "/tmp"))
    claude_dir = home / ".claude"
    if not claude_dir.exists():
        return 0

    cutoff = time.time() - (retention_days * 24 * 60 * 60)
    deleted = 0

    cleanup_specs = [
        (claude_dir / "projects", "*.jsonl"),
        (claude_dir / "debug", "*.txt"),
        (claude_dir / "todos", "*.json"),
    ]

    for base_dir, pattern in cleanup_specs:
        if not base_dir.exists():
            continue
        for path in base_dir.rglob(pattern):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    deleted += 1
            except Exception as e:
                logger.debug("Could not delete claude log %s: %s", path, e)

        # Clean up empty subdirectories (walk bottom-up)
        for dirpath in sorted(base_dir.rglob("*"), reverse=True):
            if dirpath.is_dir():
                try:
                    dirpath.rmdir()  # only succeeds if empty
                except OSError:
                    pass

    return deleted


def _sync_cron_files(conn, app_config: Config) -> None:
    """Sync CRON.md files to DB for all configured users."""
    from .cron_loader import load_cron_jobs, migrate_db_jobs_to_file, sync_cron_jobs_to_db

    for user_id in app_config.users:
        try:
            file_jobs = load_cron_jobs(app_config, user_id)
            if file_jobs is not None:
                if not file_jobs and db.get_user_scheduled_jobs(conn, user_id):
                    # File exists but empty (e.g. seeded template), DB has jobs —
                    # write DB jobs into the file instead of wiping them
                    migrate_db_jobs_to_file(conn, app_config, user_id, overwrite=True)
                else:
                    sync_cron_jobs_to_db(conn, user_id, file_jobs)
            else:
                # No CRON.md — try one-time migration from DB
                migrate_db_jobs_to_file(conn, app_config, user_id)
        except Exception as e:
            logger.error("Error syncing CRON.md for %s: %s", user_id, e)


def check_scheduled_jobs(conn, app_config: Config) -> list[int]:
    """
    Check for scheduled jobs that should run and queue them as tasks.

    Syncs CRON.md files to DB, then reads job definitions from the
    scheduled_jobs table and evaluates cron expressions in each user's timezone.

    Returns:
        List of created task IDs.
    """
    created_tasks = []

    # Sync file-based definitions to DB before evaluating
    _sync_cron_files(conn, app_config)

    jobs = db.get_enabled_scheduled_jobs(conn)
    if not jobs:
        logger.debug("No enabled scheduled jobs found")
        return created_tasks
    logger.debug("Found %d enabled scheduled job(s)", len(jobs))

    # Group by user_id to look up timezone once per user
    jobs_by_user: dict[str, list[db.ScheduledJob]] = {}
    for job in jobs:
        jobs_by_user.setdefault(job.user_id, []).append(job)

    for user_id, user_jobs in jobs_by_user.items():
        # Look up timezone from config; fall back to UTC
        user_config = app_config.users.get(user_id)
        user_tz_str = user_config.timezone if user_config else "UTC"
        try:
            user_tz = ZoneInfo(user_tz_str)
        except Exception:
            user_tz = ZoneInfo("UTC")

        now = datetime.now(user_tz)

        for job in user_jobs:
            should_run = False

            if job.last_run_at:
                last_run = datetime.fromisoformat(job.last_run_at)
                if last_run.tzinfo is None:
                    # DB stores UTC via datetime('now')
                    last_run = last_run.replace(tzinfo=ZoneInfo("UTC"))
                cron = croniter(job.cron_expression, last_run.astimezone(user_tz))
                next_run = cron.get_next(datetime)
                should_run = now >= next_run
                logger.debug(
                    "Job '%s': last_run=%s next_run=%s now=%s should_run=%s",
                    job.name, last_run, next_run, now, should_run,
                )
            else:
                # Use created_at as base so jobs don't fire immediately
                # when the cron time has already passed today
                if job.created_at:
                    base = datetime.fromisoformat(job.created_at)
                    if base.tzinfo is None:
                        # DB stores UTC via datetime('now')
                        base = base.replace(tzinfo=ZoneInfo("UTC"))
                    base = base.astimezone(user_tz)
                else:
                    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
                cron = croniter(job.cron_expression, base)
                next_run = cron.get_next(datetime)
                should_run = now >= next_run
                logger.debug(
                    "Job '%s' (never run): base=%s next_run=%s now=%s should_run=%s",
                    job.name, base, next_run, now, should_run,
                )

            if should_run:
                task_id = db.create_task(
                    conn,
                    prompt=job.prompt,
                    user_id=job.user_id,
                    source_type="scheduled",
                    conversation_token=job.conversation_token,
                    output_target=job.output_target,
                    priority=5,
                    heartbeat_silent=job.silent_unless_action,
                    scheduled_job_id=job.id,
                    command=job.command,
                    queue="background",
                )
                db.set_scheduled_job_last_run(conn, job.id)
                created_tasks.append(task_id)
                logger.info(
                    "Scheduled job '%s' (user: %s) queued as task %d",
                    job.name, job.user_id, task_id,
                )

    return created_tasks


def run_scheduler(config: Config, max_tasks: int | None = None, dry_run: bool = False) -> int:
    """
    Run the scheduler once (for cron-style invocation).
    Returns number of tasks processed.
    """
    processed = 0

    # Hydrate user configs from Nextcloud API
    try:
        hydrate_user_configs(config)
    except Exception as e:
        logger.warning("User config hydration failed: %s", e)

    # Poll Talk conversations
    if config.talk.enabled:
        try:
            from .talk_poller import poll_talk_conversations
            talk_tasks = asyncio.run(poll_talk_conversations(config))
            if talk_tasks:
                logger.info("Queued %d Talk task(s)", len(talk_tasks))
        except Exception as e:
            logger.error("Error polling Talk: %s", e)

    # Check briefings, scheduled jobs, and sleep cycles
    with db.get_db(config.db_path) as conn:
        briefing_tasks = check_briefings(conn, config)
        if briefing_tasks:
            logger.info("Queued %d briefing(s)", len(briefing_tasks))

        scheduled_tasks = check_scheduled_jobs(conn, config)
        if scheduled_tasks:
            logger.info("Queued %d scheduled job(s)", len(scheduled_tasks))

        # Check sleep cycles
        try:
            from .sleep_cycle import check_sleep_cycles
            sleep_users = check_sleep_cycles(conn, config)
            if sleep_users:
                logger.info("Ran sleep cycle for %d user(s): %s", len(sleep_users), ", ".join(sleep_users))
        except Exception as e:
            logger.error("Error running sleep cycles: %s", e)

        # Check channel sleep cycles
        try:
            from .sleep_cycle import check_channel_sleep_cycles
            channel_tokens = check_channel_sleep_cycles(conn, config)
            if channel_tokens:
                logger.info("Ran channel sleep cycle for %d channel(s): %s", len(channel_tokens), ", ".join(channel_tokens))
        except Exception as e:
            logger.error("Error running channel sleep cycles: %s", e)

    # Poll for new emails
    if config.email.enabled:
        from .email_poller import poll_emails

        email_tasks = poll_emails(config)
        if email_tasks:
            logger.info("Queued %d email task(s)", len(email_tasks))

    # Organize shared files (runs before TASKS.md polling so files are in place)
    try:
        from .shared_file_organizer import discover_and_organize_shared_files
        organized = discover_and_organize_shared_files(config)
        if organized:
            logger.info("Organized %d shared file(s)", len(organized))
    except Exception as e:
        logger.error("Error organizing shared files: %s", e)

    # Poll TASKS.md files
    try:
        from .tasks_file_poller import poll_all_tasks_files
        tasks_file_tasks = poll_all_tasks_files(config)
        if tasks_file_tasks:
            logger.info("Queued %d TASKS.md task(s)", len(tasks_file_tasks))
    except Exception as e:
        logger.error("Error polling TASKS.md files: %s", e)

    # Check heartbeats
    try:
        from .heartbeat import check_heartbeats
        with db.get_db(config.db_path) as conn:
            checked_users = check_heartbeats(conn, config)
            if checked_users:
                logger.info("Checked heartbeats for %d user(s)", len(checked_users))
    except Exception as e:
        logger.error("Error checking heartbeats: %s", e)

    # Check scheduled invoices
    try:
        from .invoice_scheduler import check_scheduled_invoices
        with db.get_db(config.db_path) as conn:
            invoice_results = check_scheduled_invoices(conn, config)
            if invoice_results["reminders_sent"] or invoice_results["invoices_generated"] or invoice_results.get("overdue_detected"):
                logger.info(
                    "Invoice scheduler: %d reminder(s), %d invoice(s) generated, %d overdue detected",
                    invoice_results["reminders_sent"], invoice_results["invoices_generated"],
                    invoice_results.get("overdue_detected", 0),
                )
    except Exception as e:
        logger.error("Error checking scheduled invoices: %s", e)

    # Process tasks
    while True:
        result = process_one_task(config, dry_run=dry_run)
        if result is None:
            break

        task_id, success = result
        processed += 1

        if max_tasks and processed >= max_tasks:
            break

    return processed


def _talk_poll_loop(config: Config) -> None:
    """Background thread: continuously polls Talk conversations."""
    from .talk_poller import poll_talk_conversations

    while not _shutdown_requested:
        try:
            talk_tasks = asyncio.run(poll_talk_conversations(config))
            if talk_tasks:
                logger.info("Queued %d Talk task(s)", len(talk_tasks))
        except Exception as e:
            logger.error("Talk poll error: %s", e)
        time.sleep(config.scheduler.talk_poll_interval)


def run_daemon(config: Config) -> None:
    """
    Run the scheduler as a daemon (continuous loop).
    Handles graceful shutdown via SIGTERM/SIGINT.
    """
    global _shutdown_requested

    # Acquire exclusive lock to prevent multiple daemon instances
    lock_path = Path("/tmp/istota-scheduler-daemon.lock")
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("Another scheduler daemon is already running. Exiting.")
        lock_file.close()
        return

    # Write PID to lock file for debugging
    lock_file.write(str(os.getpid()))
    lock_file.flush()

    # Set up signal handlers
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    logger.info("STARTUP Scheduler daemon starting (pid: %d)", os.getpid())
    logger.info("STARTUP Task poll interval: %ds", config.scheduler.poll_interval)
    logger.info("STARTUP Max fg/bg workers: %d/%d", config.scheduler.max_foreground_workers, config.scheduler.max_background_workers)
    logger.info("STARTUP Worker idle timeout: %ds", config.scheduler.worker_idle_timeout)
    logger.info("STARTUP Talk poll interval: %ds", config.scheduler.talk_poll_interval)
    logger.info("STARTUP Talk poll timeout: %ds", config.scheduler.talk_poll_timeout)
    logger.info("STARTUP Email poll interval: %ds", config.scheduler.email_poll_interval)
    logger.info("STARTUP Briefing check interval: %ds", config.scheduler.briefing_check_interval)
    logger.info("STARTUP TASKS.md poll interval: %ds", config.scheduler.tasks_file_poll_interval)
    logger.info("STARTUP Shared file check interval: %ds", config.scheduler.shared_file_check_interval)
    logger.info("STARTUP Heartbeat check interval: %ds", config.scheduler.heartbeat_check_interval)
    logger.info("STARTUP Scheduled job check interval: %ds", config.scheduler.briefing_check_interval)
    logger.info("STARTUP Cleanup interval: %ds", config.scheduler.briefing_check_interval)
    logger.info("STARTUP Feed check interval: %ds", config.scheduler.feed_check_interval)
    logger.info("STARTUP Confirmation timeout: %d min", config.scheduler.confirmation_timeout_minutes)
    logger.info("STARTUP Task retention: %d days", config.scheduler.task_retention_days)
    logger.info("STARTUP Email retention: %d days", config.scheduler.email_retention_days)
    logger.info("STARTUP Temp file retention: %d days", config.scheduler.temp_file_retention_days)

    # Hydrate user configs from Nextcloud API (display name, email, timezone)
    try:
        hydrate_user_configs(config)
    except Exception as e:
        logger.warning("User config hydration failed: %s", e)

    # Ensure user directories exist for all configured users (runs migration + README seeding)
    for user_id in config.users:
        try:
            ensure_user_directories_v2(config, user_id)
        except Exception as e:
            logger.warning("Failed to ensure directories for %s: %s", user_id, e)

    # Start Talk polling in background thread so it runs independently of task processing
    if config.talk.enabled:
        talk_thread = threading.Thread(
            target=_talk_poll_loop, args=(config,), daemon=True, name="talk-poller",
        )
        talk_thread.start()
        logger.info("STARTUP Started Talk polling thread")

    # Create worker pool for per-user concurrent task processing
    pool = WorkerPool(config)

    last_email_poll = 0.0
    last_briefing_check = 0.0
    last_tasks_file_poll = 0.0
    last_shared_file_check = 0.0
    last_scheduled_job_check = 0.0
    last_cleanup_check = 0.0
    last_sleep_cycle_check = 0.0
    last_channel_sleep_cycle_check = 0.0
    last_heartbeat_check = 0.0
    last_invoice_schedule_check = 0.0
    last_feed_check = 0.0

    while not _shutdown_requested:
        # Dispatch worker threads first — minimizes latency for pending tasks
        try:
            pool.dispatch()
        except Exception as e:
            logger.error("Error dispatching workers: %s", e)

        now = time.time()

        # Check briefings periodically
        if now - last_briefing_check >= config.scheduler.briefing_check_interval:
            try:
                with db.get_db(config.db_path) as conn:
                    briefing_tasks = check_briefings(conn, config)
                    if briefing_tasks:
                        logger.info("Queued %d briefing(s)", len(briefing_tasks))
            except Exception as e:
                logger.error("Error checking briefings: %s", e)
            last_briefing_check = now

        # Check scheduled jobs periodically (same interval as briefings)
        if now - last_scheduled_job_check >= config.scheduler.briefing_check_interval:
            try:
                with db.get_db(config.db_path) as conn:
                    scheduled_tasks = check_scheduled_jobs(conn, config)
                    if scheduled_tasks:
                        logger.info("Queued %d scheduled job(s)", len(scheduled_tasks))
            except Exception as e:
                logger.error("Error checking scheduled jobs: %s", e)
            last_scheduled_job_check = now

        # Check sleep cycles periodically (same interval as briefings)
        if now - last_sleep_cycle_check >= config.scheduler.briefing_check_interval:
            try:
                from .sleep_cycle import check_sleep_cycles
                with db.get_db(config.db_path) as conn:
                    sleep_users = check_sleep_cycles(conn, config)
                    if sleep_users:
                        logger.info("Ran sleep cycle for %d user(s): %s", len(sleep_users), ", ".join(sleep_users))
            except Exception as e:
                logger.error("Error running sleep cycles: %s", e)
            last_sleep_cycle_check = now

        # Check channel sleep cycles periodically (same interval as briefings)
        if now - last_channel_sleep_cycle_check >= config.scheduler.briefing_check_interval:
            try:
                from .sleep_cycle import check_channel_sleep_cycles
                with db.get_db(config.db_path) as conn:
                    channel_tokens = check_channel_sleep_cycles(conn, config)
                    if channel_tokens:
                        logger.info("Ran channel sleep cycle for %d channel(s): %s", len(channel_tokens), ", ".join(channel_tokens))
            except Exception as e:
                logger.error("Error running channel sleep cycles: %s", e)
            last_channel_sleep_cycle_check = now

        # Poll emails periodically
        if config.email.enabled and now - last_email_poll >= config.scheduler.email_poll_interval:
            try:
                from .email_poller import poll_emails
                email_tasks = poll_emails(config)
                if email_tasks:
                    logger.info("Queued %d email task(s)", len(email_tasks))
            except Exception as e:
                logger.error("Error polling emails: %s", e)
            last_email_poll = now

        # Organize shared files periodically (before TASKS.md polling)
        if now - last_shared_file_check >= config.scheduler.shared_file_check_interval:
            try:
                from .shared_file_organizer import discover_and_organize_shared_files
                organized = discover_and_organize_shared_files(config)
                if organized:
                    logger.info("Organized %d shared file(s)", len(organized))
            except Exception as e:
                logger.error("Error organizing shared files: %s", e)
            last_shared_file_check = now

        # Poll TASKS.md files periodically
        if now - last_tasks_file_poll >= config.scheduler.tasks_file_poll_interval:
            try:
                from .tasks_file_poller import poll_all_tasks_files
                tasks_file_tasks = poll_all_tasks_files(config)
                if tasks_file_tasks:
                    logger.info("Queued %d TASKS.md task(s)", len(tasks_file_tasks))
            except Exception as e:
                logger.error("Error polling TASKS.md files: %s", e)
            last_tasks_file_poll = now

        # Run cleanup checks periodically (same interval as briefing checks)
        if now - last_cleanup_check >= config.scheduler.briefing_check_interval:
            try:
                asyncio.run(run_cleanup_checks(config))
            except Exception as e:
                logger.error("Error running cleanup checks: %s", e)
            last_cleanup_check = now

        # Check heartbeats periodically
        if now - last_heartbeat_check >= config.scheduler.heartbeat_check_interval:
            try:
                from .heartbeat import check_heartbeats
                with db.get_db(config.db_path) as conn:
                    checked_users = check_heartbeats(conn, config)
                    if checked_users:
                        logger.debug("Checked heartbeats for %d user(s)", len(checked_users))
            except Exception as e:
                logger.error("Error checking heartbeats: %s", e)
            last_heartbeat_check = now

        # Check scheduled invoices periodically (same interval as briefings)
        if now - last_invoice_schedule_check >= config.scheduler.briefing_check_interval:
            try:
                from .invoice_scheduler import check_scheduled_invoices
                with db.get_db(config.db_path) as conn:
                    invoice_results = check_scheduled_invoices(conn, config)
                    if invoice_results["reminders_sent"] or invoice_results["invoices_generated"]:
                        logger.info(
                            "Invoice scheduler: %d reminder(s), %d invoice(s) generated",
                            invoice_results["reminders_sent"], invoice_results["invoices_generated"],
                        )
            except Exception as e:
                logger.error("Error checking scheduled invoices: %s", e)
            last_invoice_schedule_check = now

        # Poll feeds periodically
        if config.site.enabled and now - last_feed_check >= config.scheduler.feed_check_interval:
            try:
                from .feed_poller import check_feeds
                check_feeds(config)
            except Exception as e:
                logger.error("Error checking feeds: %s", e)
            last_feed_check = now

        # Sleep before next poll cycle
        time.sleep(config.scheduler.poll_interval)

    # Shutdown workers before releasing lock
    pool.shutdown()

    # Release lock on shutdown
    fcntl.flock(lock_file, fcntl.LOCK_UN)
    lock_file.close()

    logger.info("Shutdown complete.")


def main():
    """Entry point for scheduler script."""
    import argparse

    from .logging_setup import setup_logging

    parser = argparse.ArgumentParser(description="Istota task scheduler")
    parser.add_argument("-c", "--config", help="Path to config file")
    parser.add_argument("--daemon", "-d", action="store_true", help="Run as daemon (continuous loop)")
    parser.add_argument("--max-tasks", type=int, help="Maximum tasks to process (single run mode)")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually execute tasks")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()

    config = load_config(Path(args.config) if args.config else None)

    # Configure logging based on config and flags
    setup_logging(config, verbose=args.verbose, daemon_mode=args.daemon)

    if args.daemon:
        if args.dry_run:
            logger.warning("--dry-run is ignored in daemon mode")
        run_daemon(config)
    else:
        processed = run_scheduler(config, max_tasks=args.max_tasks, dry_run=args.dry_run)
        logger.info("Processed %d task(s)", processed)


if __name__ == "__main__":
    main()
