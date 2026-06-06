"""Task scheduler - processes pending tasks and briefings."""

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import random
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

logger = logging.getLogger("istota.scheduler")

from . import db
from .brain import make_brain
from .consumers import (
    LogChannelSubscriber,
    PushNotificationSubscriber,
    TalkEventSubscriber,
)
from .db_health import CheckReport, check_and_repair
from .events import EventWriter
from .skills.briefing import (
    build_briefing_prompt,
    get_briefings_for_user,
    parse_briefing_json,
    strip_briefing_preamble,
    strip_markdown as _strip_markdown,
)
from .config import Config, load_config
from .executor import (
    detect_malformed_result,
    discover_calendars_for_task,
    execute_task,
    is_transient_api_error,
    parse_api_error,
)
from .nextcloud_api import hydrate_user_configs
from .notifications import send_notification
from .talk import TalkClient, split_message
from .email_poller import get_email_config
from .skills.email import reply_to_email
from .storage import ensure_user_directories_v2
from .tasks_file_poller import handle_tasks_file_completion

# Deferred-op handlers were extracted to a sibling module; re-export the
# names so existing tests and call-sites that import them from this module
# keep working. Callers that touch this module directly (process_one_task,
# the retry path) reference these names unqualified, so the re-export is
# load-bearing.
from .scheduler_deferred import (  # noqa: F401  -- re-exported for back-compat
    _KNOWN_DEFERRED_SUFFIXES,
    _load_deferred_json,
    _process_deferred_health_ops,
    _process_deferred_kg_ops,
    _process_deferred_kv_ops,
    _process_deferred_sent_emails,
    _process_deferred_subtasks,
    _process_deferred_tracking,
    _process_deferred_user_alerts,
    _purge_deferred_files_for_retry,
    _warn_unconsumed_deferred_files,
)

def _now(tz=None):
    """Current time — thin wrapper for testability."""
    return datetime.now(tz)


def _is_stale_fire(
    name: str,
    next_run: datetime,
    now_naive: datetime,
    threshold_minutes: int,
) -> bool:
    """Return True if `next_run` is more than `threshold_minutes` behind `now_naive`.

    Suppresses thundering-herd catch-up after a long daemon outage. Callers
    bump `last_run_at` to "now" when this returns True so croniter resumes
    cleanly from the next future fire-time instead of looping on the same
    stale next_run. 0 disables the gate (legacy unconditional catch-up).
    """
    if threshold_minutes <= 0:
        return False
    staleness_min = (now_naive - next_run).total_seconds() / 60
    if staleness_min <= threshold_minutes:
        return False
    logger.warning(
        "Skipping stale fire of '%s' (missed by %.1f min, threshold %d min)",
        name, staleness_min, threshold_minutes,
    )
    return True


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
    "*Hmm...*",
    "*Heard, chef...*",
    "*Investigating...*",
    "*One sec...*",
    "*Copy that...*",
    "*Roger...*",
    "*Considering...*",
    "*Thinkifying...*",
    "*Braining...*",
    "*Improvising...*",
    "*Jamming...*",
    "*Riffing...*",
    "*Grooving...*",
    "*Beboppin'...*",
    "*Noodling...*",
    "*Syncopating...*",
    "*Comping...*",
    "*Soloing...*",
    # Cephalopod
    "*Inking...*",
    "*Tentacling...*",
    "*Suckering...*",
    "*Jetting...*",
    "*Unfurling...*",
    "*Chromatophoring...*",
    "*Squidding...*",
    "*Grasping...*",
    "*Probing...*",
    "*Siphoning...*",
    # Cheeky
    "*Instigating...*",
    "*Scheming...*",
    "*Concocting...*",
    "*Percolating...*",
    "*Marinating...*",
    "*Hatching...*",
    "*Sleuthing...*",
    "*Finagling...*",
    "*Wrangling...*",
    "*Tinkering...*",
    "*Rummaging...*",
    "*Conjuring...*",
    "*Fermenting...*",
    "*Machinating...*",
    "*Gallivanting...*",
]


_POLICY_REFUSAL_KEYWORDS = ("safety", "policy", "content", "refused", "harm", "blocked")

_FROM_HEADER_PATTERN = re.compile(r"(?:^|\n)From:\s*(.+?)(?:\n|$)")

# Grace margin added to task_timeout_minutes before a 'running' task is treated
# as stuck (worker presumed dead) and reclaimed. A healthy worker self-kills at
# the timeout and writes its result; the margin covers that write. Without it,
# the reclaim window (formerly a flat 15 min) sits below the 30-min timeout, so
# a slow-but-healthy task — notably the in-process native brain, which has no
# killable PID — gets reclaimed and duplicated (ISSUE-112).
_STUCK_RUNNING_GRACE_MINUTES = 5


def _stuck_running_minutes(sched) -> int:
    """Fallback stuck threshold (minutes) for a task that never heart-beat."""
    return sched.task_timeout_minutes + _STUCK_RUNNING_GRACE_MINUTES


@contextlib.contextmanager
def _task_heartbeat(config: Config, task_id: int):
    """Ping the task's liveness while its body runs (ISSUE-112).

    A background daemon thread touches ``last_heartbeat`` every
    ``worker_heartbeat_seconds`` so stuck-task reclaim can tell a slow-but-alive
    worker from a dead one — the native brain runs in-process with no killable
    PID and no subprocess to die, so without this a long task looks identical to
    a crashed one. The first ping fires immediately, so liveness is recorded as
    soon as the body starts; the thread is stopped on exit (incl. on exception),
    which lets reclaim fire promptly once a worker really dies.
    """
    interval = config.scheduler.worker_heartbeat_seconds
    if interval <= 0:
        yield  # heartbeat disabled
        return

    stop = threading.Event()

    def _loop():
        while True:
            try:
                with db.get_db(config.db_path) as conn:
                    db.touch_task_heartbeat(conn, task_id)
            except Exception:
                logger.debug("heartbeat ping failed for task %s", task_id, exc_info=True)
            if stop.wait(interval):
                return

    thread = threading.Thread(target=_loop, name=f"heartbeat-{task_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)


def _is_policy_refusal(error_text: str) -> bool:
    """Check if a task failure is an API policy/safety refusal (non-retryable)."""
    parsed = parse_api_error(error_text)
    if not parsed:
        return False
    if parsed["status_code"] != 400:
        return False
    msg = (parsed.get("message") or "").lower()
    return any(kw in msg for kw in _POLICY_REFUSAL_KEYWORDS)


def _post_policy_refusal_alert(
    config: "Config", task: "db.Task", error_text: str,
) -> None:
    """Post an alert to the user's alerts channel when content triggers an API policy refusal.

    For email tasks, extracts the sender from the prompt's `From:` header so the user can
    see who tripped the filter. For other sources, falls back to the conversation token.
    """
    parsed = parse_api_error(error_text)
    api_msg = (parsed or {}).get("message") or "Unknown"

    sender = None
    if task.source_type == "email" and task.prompt:
        m = _FROM_HEADER_PATTERN.search(task.prompt)
        if m:
            sender = m.group(1).strip()

    if task.source_type == "email" and sender:
        message = (
            f"⚠️ **Inbound email blocked** (task #{task.id})\n\n"
            f"Email from **{sender}** triggered the API safety filter "
            f"and was not processed.\n\n"
            f"Reason: {api_msg}"
        )
    else:
        source_label = task.conversation_token or task.source_type or "unknown"
        message = (
            f"⚠️ **Task blocked by API safety filter** (task #{task.id})\n\n"
            f"Content from {source_label} triggered a policy refusal "
            f"and was not processed.\n\n"
            f"Reason: {api_msg}"
        )

    try:
        send_notification(config, task.user_id, message, surface="talk")
    except Exception as e:
        logger.warning(
            "Failed to post policy refusal alert for task %d (user=%s): %s",
            task.id, task.user_id, e,
        )


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
            return "Can't authenticate with Anthropic — locked out of my own brain. This needs human intervention."
        elif status == 400 and _is_policy_refusal(error_text):
            return "Content triggered the API safety filter and couldn't be processed. Check the alerts channel for details."
        else:
            return "Something went wrong talking to Anthropic. The deep stared back. Try again?"

    # Non-API errors: strip technical details, keep it friendly
    if "killed (likely out of memory)" in error_text:
        return "Ran out of memory — tried to hold too much in all eight arms at once. Try something simpler?"
    if "timed out" in error_text.lower():
        return "Drifted too deep and timed out. Maybe break this into smaller pieces?"

    # Generic fallback - don't expose raw error
    return "Something went sideways and I'm not entirely sure what. Resurfacing — try again?"


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
                    # File may be in a user's Talk folder (NC stores shared files
                    # in the sender's data dir). Check NC data dir if available.
                    nc_data = Path("/mnt/nc-data")
                    found = False
                    if nc_data.is_dir():
                        filename = att.split("/", 1)[1] if "/" in att else att
                        for user_dir in nc_data.iterdir():
                            candidate = user_dir / "files" / "Talk" / filename
                            if candidate.exists():
                                local_paths.append(str(candidate))
                                logger.debug(f"Talk attachment via nc-data: {att} -> {candidate}")
                                found = True
                                break
                    if not found:
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


async def edit_talk_message(
    config: Config, task: db.Task, message_id: int, message: str,
) -> bool:
    """Edit a Talk message in-place. Returns True on success, False on failure."""
    if not config.nextcloud.url or not task.conversation_token:
        return False
    try:
        client = TalkClient(config)
        await client.edit_message(task.conversation_token, message_id, message)
        return True
    except Exception as e:
        logger.debug("Edit message %d failed: %s", message_id, e)
        return False


# ---------------------------------------------------------------------------
# Log channel — verbose per-user task execution log
# ---------------------------------------------------------------------------

# Per-process cache: conversation_token → displayName
_channel_name_cache: dict[str, str] = {}


async def _resolve_channel_name(config: Config, conversation_token: str) -> str:
    """Resolve a conversation token to its display name via Talk API.

    Results are cached for the lifetime of the process.
    """
    if conversation_token in _channel_name_cache:
        return _channel_name_cache[conversation_token]
    try:
        client = TalkClient(config)
        info = await client.get_conversation_info(conversation_token)
        name = info.get("displayName", conversation_token)
        _channel_name_cache[conversation_token] = name
        return name
    except Exception:
        logger.debug("Failed to resolve channel name for %s", conversation_token)
        _channel_name_cache[conversation_token] = conversation_token
        return conversation_token


def _log_channel_source_label(task: db.Task, channel_name: str | None) -> tuple[str, str]:
    """Return (task_id_prefix, source_suffix) for log channel messages."""
    source = channel_name if task.conversation_token and channel_name else task.source_type
    return f"**[#{task.id}]**", source


def _deduplicate_descriptions(descriptions: list[str]) -> list[str]:
    """Collapse consecutive identical descriptions with a count suffix."""
    if not descriptions:
        return []
    result = []
    prev = descriptions[0]
    count = 1
    for desc in descriptions[1:]:
        if desc == prev:
            count += 1
        else:
            result.append(f"{prev} ×{count}" if count > 1 else prev)
            prev = desc
            count = 1
    result.append(f"{prev} ×{count}" if count > 1 else prev)
    return result


def _format_log_channel_body(
    prefix: str | tuple[str, str], descriptions: list[str], *, done: bool = False,
    success: bool = True, error: str | None = None,
    skills: list[str] | None = None,
    model: str | None = None, effort: str | None = None,
) -> str:
    """Format a log channel message with accumulated tool descriptions."""
    if isinstance(prefix, tuple):
        task_prefix, source = prefix
    else:
        task_prefix, source = prefix, ""
    total = len(descriptions)
    count = f"({total} action{'s' if total != 1 else ''})" if total else "(no tool calls)"
    if done:
        status = "✅ Done" if success else "❌ Failed"
    else:
        status = "⏳ Running"
    header = f"{task_prefix} {status} {count} - {source}" if source else f"{task_prefix} {status} {count}"
    spec = " ".join(p for p in (model, effort) if p)
    if spec:
        header = f"{header} ({spec})"
    lines = [header, ""]
    if skills:
        lines.append(f"Skills: {', '.join(skills)}")
        lines.append("")
    for desc in _deduplicate_descriptions(descriptions):
        lines.append(desc)
    if done and error:
        lines.append(f"Error: {error[:200]}")
    return "\n".join(lines)


def _finalize_log_channel(
    config: Config, task: db.Task, log_channel: str, prefix: str,
    log_callback, success: bool, error: str | None = None,
    skills: list[str] | None = None,
    model: str | None = None, effort: str | None = None,
):
    """Post/edit the final summary to the log channel."""
    descriptions = getattr(log_callback, "all_descriptions", []) if log_callback else []
    log_msg_id = getattr(log_callback, "log_msg_id", [None])[0] if log_callback else None

    body = _format_log_channel_body(
        prefix, descriptions, done=True, success=success, error=error,
        skills=skills, model=model, effort=effort,
    )

    try:
        if log_msg_id is not None:
            # Edit existing message with final state
            asyncio.run(edit_talk_message(
                config,
                db.Task(
                    id=task.id, status="running", source_type=task.source_type,
                    user_id=task.user_id, prompt="", conversation_token=log_channel,
                ),
                log_msg_id, body,
            ))
        elif descriptions:
            # No existing message (shouldn't happen, but fallback)
            client = TalkClient(config)
            asyncio.run(client.send_message(
                log_channel, body,
                reference_id=f"istota:log:{task.id}",
            ))
        else:
            # No tool calls at all — post using pre-computed body (includes skills)
            client = TalkClient(config)
            asyncio.run(client.send_message(
                log_channel, body,
                reference_id=f"istota:log:{task.id}",
            ))
    except Exception as e:
        logger.debug("Log channel finalize failed for task %d: %s", task.id, e)


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


def _talk_target_for_delivery(config: Config, task: db.Task) -> str | None:
    """Resolve the Talk room to deliver this task's notifications to.

    Tasks carry two related fields:
      - conversation_token: doubles as the email-thread grouping key for
        email-source tasks (synthetic 16-char hex hash) and as the Talk room
        for talk-source tasks. Used by context/memory lookups.
      - talk_delivery_token: the real Talk room for notifications. Always a
        valid Talk token (or NULL).

    Delivery prefers talk_delivery_token. Falls back to conversation_token,
    which is correct for talk-source tasks. Legacy email-source tasks that
    pre-date the talk_delivery_token column may carry only a synthetic
    conversation_token; for those we fall back to the user's resolved
    notification channel (alerts > briefing > auto-detected DM).
    """
    if task.talk_delivery_token:
        return task.talk_delivery_token
    token = task.conversation_token
    if task.source_type != "email" or not token:
        return token
    # Legacy fallback: pre-migration email tasks with a synthetic 16-char-hex
    # conversation_token need redirection to the user's resolved channel,
    # because the synthetic token is not a real Talk room.
    from .email_poller import is_synthetic_email_thread_token
    if not is_synthetic_email_thread_token(token):
        return token
    from .notifications import resolve_conversation_token
    resolved = resolve_conversation_token(config, task.user_id)
    return resolved or token


def _notify_confirmed_email_result(
    config: Config, task: db.Task, result: str,
) -> bool:
    """Post the bot's email reply to the alerts channel after a confirmed email task.

    When an untrusted sender's email goes through the confirmation gate, the
    user approves it in the alerts channel. After the bot processes and replies,
    this closes the loop by showing the user what was sent.

    Returns True if notification was posted, False otherwise.
    """
    if task.source_type != "email" or task.confirmation_prompt is None:
        return False

    # If output_target already includes Talk, the user sees the result in
    # their conversation — no need to duplicate it in the alerts channel.
    target = task.output_target or ""
    if target in ("both", "all", "talk"):
        return False

    # Look up the sender from the processed_emails record
    sender = "the sender"
    with db.get_db(config.db_path) as conn:
        email_record = db.get_email_for_task(conn, task.id)
        if email_record:
            sender = email_record.sender_email

    # Truncate long results for the notification
    max_chars = 2000
    body = result if len(result) <= max_chars else result[:max_chars] + "\n[...]"

    message = f"Email reply sent to {sender} (task #{task.id}):\n\n{body}"

    from .notifications import send_notification
    return send_notification(config, task.user_id, message, surface="talk")


def _deliver_deferred_email_output(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> None:
    """Deliver or clean up deferred email output files not handled by the normal path.

    The normal email delivery path (post_result_to_email via `post_email` flag)
    handles tasks whose output_target includes "email"/"both"/"all". This
    function handles two gap cases:

    1. source_type="email" but output_target doesn't include email (e.g. an
       emissary reply routed to Talk) — deliver via post_result_to_email,
       which will find the processed_email record and reply correctly.
    2. Non-email source (e.g. Talk user who asked the agent to email someone)
       where the agent used `email output` instead of `email send` — warn and
       delete, because there's no processed_email record and the scheduler
       would send to the wrong recipient.
    """
    target = task.output_target or ""
    if target in ("email", "both", "all"):
        # Normal path will deliver via post_email flag — nothing to do here.
        return
    path = user_temp_dir / f"task_{task.id}_email_output.json"
    if not path.exists():
        return

    if task.source_type == "email":
        # Email-sourced task with non-email output_target (e.g. emissary reply
        # with output_target="talk"). The processed_email record exists, so
        # post_result_to_email can reply correctly.
        logger.info(
            "Delivering deferred email output for task %d (source=%s, target=%s)",
            task.id, task.source_type, target,
        )
        email_ok = asyncio.run(post_result_to_email(config, task, ""))
        if not email_ok:
            logger.error(
                "Failed to deliver deferred email output for task %d", task.id,
            )
    else:
        # Non-email source — agent used `email output` instead of `email send`.
        # No processed_email record, so we can't deliver to the right recipient.
        logger.warning(
            "Orphaned deferred email output file for task %d (source=%s): "
            "Claude used `email output` instead of `email send`. "
            "The email was NOT delivered. Removing file.",
            task.id, task.source_type,
        )
        path.unlink(missing_ok=True)


def _purge_obsolete_skill_jobs(conn, skill_index: dict) -> None:
    """Delete scheduled_jobs rows AND fail pending tasks rows whose
    ``skill`` field no longer exists in the skill index.

    Symmetric with cron_loader's CRON.md-orphan deletion, but for
    auto-seeded skill-task rows. The seeders re-create scheduled_jobs
    rows for skills that still exist on the next tick. Pending tasks
    rows are marked failed (not deleted) so audit / delivery state is
    preserved; rename is operator-driven and rare.
    """
    cur = conn.execute(
        "SELECT id, name, user_id, skill FROM scheduled_jobs "
        "WHERE skill IS NOT NULL"
    )
    for row in cur.fetchall():
        skill = row["skill"] if hasattr(row, "keys") else row[3]
        if skill not in skill_index:
            logger.warning(
                "Removing obsolete skill scheduled_job '%s' user=%s skill=%s "
                "(skill no longer exists in index)",
                row["name"], row["user_id"], skill,
            )
            conn.execute("DELETE FROM scheduled_jobs WHERE id = ?", (row["id"],))

    cur = conn.execute(
        "SELECT id, user_id, skill FROM tasks "
        "WHERE skill IS NOT NULL AND status IN ('pending', 'locked')"
    )
    for row in cur.fetchall():
        skill = row["skill"] if hasattr(row, "keys") else row[2]
        if skill not in skill_index:
            logger.warning(
                "Failing pending skill task #%d user=%s skill=%s "
                "(skill no longer exists in index)",
                row["id"], row["user_id"], skill,
            )
            conn.execute(
                "UPDATE tasks SET status='failed', error=?, "
                "completed_at=datetime('now'), updated_at=datetime('now') "
                "WHERE id = ?",
                (f"unknown skill: {skill}", row["id"]),
            )
    conn.commit()


def _run_garmin_sync_inprocess(
    task: db.Task, config: Config, skill_args: list[str],
) -> tuple[bool, str]:
    """Run ``health garmin-sync`` in the daemon thread.

    The garmin engine reads + writes encrypted secrets (oauth blob,
    rotated SDK tokens, error flag, last_sync). The subprocess path
    strips ``ISTOTA_SECRET_KEY`` by design, so the engine can neither
    decrypt the stored tokens nor persist mid-run refreshes from there.
    The web ``/garmin/sync`` endpoint already runs the same engine
    in-process; this is the cron-driven equivalent.
    """
    from istota.health import garmin_sync as gs
    from istota.health import resolve_for_user
    from istota.health._loader import UserNotFoundError

    days_back = 2
    tail = skill_args[1:]
    for i, arg in enumerate(tail):
        if arg == "--days-back" and i + 1 < len(tail):
            try:
                days_back = max(1, int(tail[i + 1]))
            except (TypeError, ValueError):
                pass
            break

    if config.db_path is None:
        return False, "garmin sync: framework db_path unavailable"

    try:
        ctx = resolve_for_user(task.user_id, config)
    except UserNotFoundError as exc:
        return False, f"garmin sync: {exc}"

    # Live DB timezone so the "yesterday" window tracks travel (ISSUE-099);
    # "UTC" is the engine's effective default, same as the old None.
    user_tz = config.resolve_user_timezone(task.user_id)

    try:
        res = gs.sync_garmin(
            ctx, Path(config.db_path),
            days_back=days_back, user_tz=user_tz,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("garmin sync (in-process) raised for user=%s", task.user_id)
        return False, f"garmin sync: {exc}"

    payload = res.to_dict()
    payload["status"] = "error" if res.auth_error else "ok"
    if res.auth_error:
        payload["error"] = "token_expired"
    result_text = json.dumps(payload)
    return (not res.auth_error), result_text


def _execute_skill_task(
    task: db.Task, config: Config,
) -> tuple[bool, str]:
    """Execute an auto-seeded skill-task in a single subprocess.

    Phase 1.3 of the unified credential resolution refactor: cron-driven
    `_module.<name>.*` jobs run as ``istota.skills.<skill>`` subprocesses
    with credentials pre-resolved via :func:`build_skill_env` on the
    trusted side. The master Fernet key never leaves the daemon.

    Skill-tasks are not arbitrary shell, so they are not admin-gated.
    """
    from .executor import build_clean_env, get_user_temp_dir
    from .skills._env import EnvContext, build_skill_env, dispatch_setup_env_hooks
    from .skills._loader import load_skill_index

    skill_name = task.skill or ""
    try:
        skill_args = json.loads(task.skill_args or "[]")
    except (json.JSONDecodeError, ValueError):
        return False, f"invalid skill_args JSON: {task.skill_args!r}"
    if not isinstance(skill_args, list) or not all(
        isinstance(a, str) for a in skill_args
    ):
        return False, "skill_args must be a JSON list of strings"

    skill_index = load_skill_index(
        config.skills_dir, config.bundled_skills_dir,
    )
    if skill_name not in skill_index:
        return False, f"unknown skill: {skill_name}"

    # In-process dispatch for skill-tasks that need read/write access to
    # the encrypted secrets store. The subprocess path strips
    # ``ISTOTA_SECRET_KEY`` by design (executor.build_clean_env), so
    # any skill that reaches into ``secrets_store`` directly — and the
    # garmin sync engine reads + writes multiple entries (oauth blob,
    # rotated tokens, error flag, last_sync) — cannot run there. Mirrors
    # the in-process call the web ``/garmin/sync`` endpoint already makes.
    if skill_name == "health" and skill_args[:1] == ["garmin-sync"]:
        return _run_garmin_sync_inprocess(task, config, skill_args)

    timeout = config.scheduler.task_timeout_minutes * 60
    user_temp_dir = get_user_temp_dir(config, task.user_id)
    user_temp_dir.mkdir(parents=True, exist_ok=True)

    with db.get_db(config.db_path) as conn:
        user_resources = db.get_user_resources(conn, task.user_id)
    user_cfg = config.get_user(task.user_id)

    ctx = EnvContext(
        config=config,
        task=task,
        user_resources=user_resources,
        user_config=user_cfg,
        user_temp_dir=user_temp_dir,
        is_admin=config.is_admin(task.user_id),
        discovered_calendars=discover_calendars_for_task(task, config),
    )

    env = build_clean_env(config)
    env["ISTOTA_TASK_ID"] = str(task.id)
    env["ISTOTA_USER_ID"] = task.user_id
    env["ISTOTA_DEFERRED_DIR"] = str(user_temp_dir)
    env["ISTOTA_EXPERIMENTAL_FEATURES"] = ",".join(config.experimental.features)
    if config.config_path:
        env["ISTOTA_CONFIG_PATH"] = str(config.config_path)
    if config.db_path:
        env["ISTOTA_DB_PATH"] = str(config.db_path)
    if config.nextcloud_mount_path:
        env["NEXTCLOUD_MOUNT_PATH"] = str(config.nextcloud_mount_path)
    if task.conversation_token:
        env["ISTOTA_CONVERSATION_TOKEN"] = task.conversation_token

    # Resolve declarative env from the full skill_index — co-declared
    # vars (e.g. NC_URL on both ``files`` and ``nextcloud``) must reach
    # the subprocess regardless of which skill the task names. No proxy
    # split: skill-tasks run a trusted CLI, not an LLM, so credentials
    # flow directly. ``build_skill_env`` warns on real value conflicts.
    env.update(build_skill_env(list(skill_index), skill_index, ctx))

    # Run setup_env hooks (C1). Without this, declarative env specs
    # marked ``from: "setup_env"`` (notably ``HEALTH_DB_PATH``) resolve
    # to None in build_skill_env — the health skill's scheduled
    # Garmin sync would fail every cron tick with "HEALTH_DB_PATH not
    # set". Hooks self-gate, so dispatching the full skill_index is
    # safe.
    env.update(dispatch_setup_env_hooks(list(skill_index), skill_index, ctx))

    # Per-user timezone (used by sync engines like Garmin that need to
    # compute the user's "yesterday" in their local TZ rather than UTC).
    # Resolved from the live user_profiles DB row so it tracks travel
    # without a daemon restart (ISSUE-099).
    env["ISTOTA_USER_TZ"] = config.resolve_user_timezone(task.user_id)

    cmd = [sys.executable, "-m", f"istota.skills.{skill_name}"] + skill_args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(config.temp_dir),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return (
            False,
            f"Skill timed out after {config.scheduler.task_timeout_minutes} minutes",
        )
    except Exception as e:
        return False, f"Skill execution error: {e}"

    if proc.returncode == 0:
        result = proc.stdout.strip() if proc.stdout else "(no output)"
        # Module-skill subprocesses (feeds, money) catch their own errors
        # and print `{"status":"error","error":"…"}` while exiting 0;
        # treat that envelope as failure (defense in depth — the facades
        # also call sys.exit(1) on the error envelope).
        if result.startswith("{"):
            try:
                parsed = json.loads(result)
            except (json.JSONDecodeError, ValueError):
                parsed = None
            if isinstance(parsed, dict) and parsed.get("status") == "error":
                err_msg = parsed.get("error") or "skill reported status=error"
                return False, str(err_msg)
        return True, result
    error = proc.stderr.strip() if proc.stderr else f"Exit code {proc.returncode}"
    return False, error


def _execute_command_task(
    task: db.Task, config: Config,
) -> tuple[bool, str]:
    """Execute a shell command task via subprocess.

    Returns (success, result) — same interface as execute_task().
    """
    # Defense in depth — cron_loader rejects command-type CRON.md entries
    # from non-admins at sync time, but a stale row could have been inserted
    # by an earlier admin or a direct DB write. Auto-seeded module skill
    # tasks (feeds, money) now go through ``_execute_skill_task``; only
    # operator-defined CRON.md ``command:`` rows remain on this path.
    if not config.is_admin(task.user_id):
        return False, "command-type tasks are admin-only"

    timeout = config.scheduler.task_timeout_minutes * 60

    from .executor import build_stripped_env, get_user_temp_dir
    from .skills._env import EnvContext, build_skill_env, dispatch_setup_env_hooks
    from .skills._loader import load_skill_index
    env = build_stripped_env()
    env["ISTOTA_TASK_ID"] = str(task.id)
    env["ISTOTA_USER_ID"] = task.user_id
    env["ISTOTA_EXPERIMENTAL_FEATURES"] = ",".join(config.experimental.features)
    # Propagate the config path so module skills (feeds, money) loading
    # istota config from a fresh subprocess find the same file the daemon
    # did — the subprocess cwd is `config.temp_dir`, which doesn't contain
    # the relative `config/config.toml` candidate.
    if config.config_path:
        env["ISTOTA_CONFIG_PATH"] = str(config.config_path)
    if config.db_path:
        env["ISTOTA_DB_PATH"] = str(config.db_path)
    if config.nextcloud_mount_path:
        env["NEXTCLOUD_MOUNT_PATH"] = str(config.nextcloud_mount_path)
    if task.conversation_token:
        env["ISTOTA_CONVERSATION_TOKEN"] = task.conversation_token

    # Resolve credential / connection env vars from skill manifests
    # (NC_URL/USER/PASS, CALDAV_*, etc.) instead of hardcoding them.
    # Same trusted resolution path the skill-task dispatcher uses; the
    # operator's command may invoke any istota-skill CLI, so we expose
    # the union over the full skill_index. CalDAV vars are gated on
    # discovered calendars to mirror the LLM path.
    user_temp_dir = get_user_temp_dir(config, task.user_id)
    user_temp_dir.mkdir(parents=True, exist_ok=True)
    with db.get_db(config.db_path) as conn:
        user_resources = db.get_user_resources(conn, task.user_id)
    skill_index = load_skill_index(config.skills_dir, config.bundled_skills_dir)
    ctx = EnvContext(
        config=config,
        task=task,
        user_resources=user_resources,
        user_config=config.get_user(task.user_id),
        user_temp_dir=user_temp_dir,
        is_admin=config.is_admin(task.user_id),
        discovered_calendars=discover_calendars_for_task(task, config),
    )
    for k, v in build_skill_env(list(skill_index), skill_index, ctx).items():
        if k not in env:
            env[k] = v
    # Run setup_env hooks. Without this, declarative env specs marked
    # ``from: "setup_env"`` (notably ``LOCATION_DB_PATH``, ``HEALTH_DB_PATH``)
    # resolve to None in build_skill_env — operator-defined CRON.md
    # command rows that shell out to skill CLIs needing those vars would
    # fail silently. Mirrors the _execute_skill_task path. Hook values
    # win over the daemon's ambient env because they are computed
    # per-user; a stray LOCATION_DB_PATH inherited from systemd would
    # point at the wrong user's DB.
    env.update(dispatch_setup_env_hooks(list(skill_index), skill_index, ctx))
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
        # Module-skill subprocesses (feeds, money, …) catch their own errors
        # and print `{"status":"error","error":"…"}` to stdout while exiting 0,
        # which would otherwise look like a successful run. Treat that envelope
        # as failure so retries / alerts kick in instead of silently rotting.
        if result.startswith("{"):
            try:
                parsed = json.loads(result)
            except (json.JSONDecodeError, ValueError):
                parsed = None
            if isinstance(parsed, dict) and parsed.get("status") == "error":
                err_msg = parsed.get("error") or "command reported status=error"
                return False, str(err_msg)
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
            stuck_running_minutes=_stuck_running_minutes(config.scheduler),
            heartbeat_stuck_minutes=config.scheduler.worker_stuck_minutes,
        )
        if not task:
            return None

        task_id = task.id
        db.log_task(conn, task_id, "info", f"Task claimed by {worker_id}")

        # Update to running
        db.update_task_status(conn, task_id, "running")

        # Get user resources
        user_resources = db.get_user_resources(conn, task.user_id)

    # Log channel setup — resolve before execution starts
    user_cfg = config.get_user(task.user_id)
    log_channel = user_cfg.log_channel if user_cfg else ""
    if task.skip_log_channel:
        log_channel = ""
    log_channel_prefix = ""
    log_callback = None
    if log_channel and config.nextcloud.url and not dry_run:
        # Resolve source channel name for the log prefix
        channel_name = None
        if task.conversation_token:
            try:
                channel_name = asyncio.run(
                    _resolve_channel_name(config, task.conversation_token),
                )
            except Exception:
                channel_name = task.conversation_token
        log_channel_prefix = _log_channel_source_label(task, channel_name)

    # Clean up stale deferred email output from a previous execution (e.g.
    # confirmation flow: first run writes a draft via `email output`, re-run
    # sends via `email send` — the stale file would cause a double-send).
    if task.confirmation_prompt is not None:
        from .executor import get_user_temp_dir
        _stale = get_user_temp_dir(config, task.user_id) / f"task_{task.id}_email_output.json"
        if _stale.exists():
            logger.debug("Removing stale email output file from prior execution of task %d", task.id)
            _stale.unlink(missing_ok=True)

    # Command and skill tasks skip Talk ack, attachment download, and
    # resource loading (cron-driven, no live user behind a Talk session).
    # The brain path builds an EventWriter and subscribes the in-process
    # consumers (Talk / log channel / push). ack_msg_id and the subscribers
    # are referenced again during delivery, so they live at this scope.
    event_writer: EventWriter | None = None
    talk_sub: TalkEventSubscriber | None = None
    log_callback: LogChannelSubscriber | None = None
    ack_msg_id = None
    # Ping liveness for the whole execution so stuck-task reclaim can tell a
    # slow-but-alive worker from a dead one (ISSUE-112). Covers the skill,
    # command, and brain paths; stops on exit even if execution raises.
    with _task_heartbeat(config, task_id):
        if task.skill:
            success, result = _execute_skill_task(task, config)
            actions_taken = None
            execution_trace = None
        elif task.command:
            success, result = _execute_command_task(task, config)
            actions_taken = None
            execution_trace = None
        else:
            # One event writer per task; subscribers fan its stream out to
            # their surfaces. SSE / admin consumers are NOT subscribers —
            # they poll the task_events table the writer persists to.
            event_writer = EventWriter(
                task_id, str(config.db_path),
                enabled=config.scheduler.event_log_enabled,
            )

            # Send ack message + wire the Talk subscriber for Talk tasks.
            is_rerun = task.attempt_count > 0 or task.confirmation_prompt is not None
            if task.source_type == "talk" and task.conversation_token and not dry_run:
                ack_text = f"`#{task.id}` *Retrying…*" if is_rerun else f"`#{task.id}` {random.choice(PROGRESS_MESSAGES)}"
                ack_msg_id = asyncio.run(post_result_to_talk(
                    config, task, ack_text,
                    reference_id=f"istota:task:{task.id}:ack",
                ))
                if ack_msg_id is None:
                    logger.warning(
                        "Ack message posted but no message ID returned for task %d "
                        "(progress edits will no-op)",
                        task.id,
                    )
                if config.scheduler.progress_updates:
                    talk_sub = TalkEventSubscriber(config, task, ack_msg_id)
                    event_writer.subscribe(talk_sub)

            # Log channel subscriber (no rate limiting, streams every tool call).
            if log_channel and log_channel_prefix:
                log_callback = LogChannelSubscriber(
                    config, task, log_channel, log_channel_prefix,
                )
                event_writer.subscribe(log_callback)

            # Push notification subscriber, gated by source type.
            if task.source_type in config.scheduler.push_notification_sources and not dry_run:
                event_writer.subscribe(PushNotificationSubscriber(
                    config, task,
                    threshold_seconds=config.scheduler.push_notification_threshold_seconds,
                ))

            # Download Talk attachments to local filesystem before execution
            if task.source_type == "talk" and task.attachments:
                local_attachments = download_talk_attachments(config, task.attachments)
                # Create modified task with local paths
                task = replace(task, attachments=local_attachments)

            # Execute the task (outside the db context to avoid long locks)
            success, result, actions_taken, execution_trace = execute_task(
                task, config, user_resources, dry_run=dry_run, event_writer=event_writer,
            )

    # Resolve the Talk room for delivering this task's notifications.
    # For email-source tasks with a synthetic thread-hash conversation_token,
    # this falls back to the user's resolved alerts/DM channel — otherwise
    # follow-up notifications would silently no-op.
    talk_token = _talk_target_for_delivery(config, task)

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

    # Guard: detect API errors masquerading as successful results
    # (Claude Code may exit 0 with API error text as output)
    if success and parse_api_error(result):
        logger.warning(
            "Task %d: result contains API error despite success flag, treating as failure",
            task_id,
        )
        success = False

    # Guard: detect malformed model output (leaked tool-call XML syntax)
    if success:
        malformed_reason = detect_malformed_result(result, output_target=target)
        if malformed_reason:
            logger.warning(
                "Task %d: malformed result detected (%s), treating as failure",
                task_id, malformed_reason,
            )
            success = False
            result = f"Malformed output: {malformed_reason}"

    # Log result quality metrics
    if result:
        _qm_tool_count = 0
        if actions_taken:
            try:
                _qm_tool_count = len(json.loads(actions_taken))
            except (json.JSONDecodeError, TypeError):
                pass
        logger.info(
            "Task %d result metrics: success=%s, chars=%d, tools=%d",
            task_id, success, len(result), _qm_tool_count,
        )

    with db.get_db(config.db_path) as conn:
        if success:
            # Check if the result is a confirmation request
            is_confirmation_request = (
                target in ("talk", "both")
                and talk_token
                and CONFIRMATION_PATTERN.search(result)
            )

            if is_confirmation_request:
                # Set task to pending confirmation instead of completing
                db.set_task_confirmation(conn, task_id, result)
                db.log_task(conn, task_id, "info", "Task awaiting user confirmation")
                post_talk_message = result
            else:
                db.update_task_status(conn, task_id, "completed", result=result, actions_taken=actions_taken, execution_trace=execution_trace)
                db.log_task(conn, task_id, "info", "Task completed successfully")

                # Index conversation for memory search (non-critical).
                # Skip silent scheduled jobs: high-volume retrieve-and-render
                # crons whose conversations have no recall value but inflate
                # memory_chunks (and the vec/FTS indexes derived from it).
                if (
                    config.memory_search.enabled
                    and config.memory_search.auto_index_conversations
                    and not task.heartbeat_silent
                ):
                    try:
                        from .memory.search import index_conversation as _index_conv
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
                        if talk_token:
                            post_talk_message = result_to_post
                    else:
                        db.log_task(conn, task_id, "info", "Silent scheduled job: no action needed")

                else:
                    # Non-heartbeat, non-silent task: normal delivery logic
                    if task.source_type == "briefing":
                        # Parse structured JSON output; fall back to raw text
                        parsed_briefing = parse_briefing_json(result)
                        if parsed_briefing:
                            delivery_result = parsed_briefing["body"]
                        else:
                            delivery_result = strip_briefing_preamble(result)
                    else:
                        delivery_result = result
                    if target in ("talk", "both", "all") and talk_token:
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
            # Check if we should retry (skip for OOM, cancellation, and policy refusals)
            is_oom = "killed (likely out of memory)" in result
            is_cancelled = result == "Cancelled by user"
            is_policy = _is_policy_refusal(result)
            if is_cancelled:
                db.update_task_status(conn, task_id, "cancelled", error=result)
                db.log_task(conn, task_id, "info", "Task cancelled by user via !stop")
                # No Talk notification needed — !stop already acknowledged
            elif is_policy:
                # Policy refusals are non-retryable: same content will be rejected again.
                # Mark failed and post an alert so the user sees what was blocked.
                db.update_task_status(conn, task_id, "failed", error=result)
                db.log_task(
                    conn, task_id, "warn",
                    f"Task failed: API policy refusal (not retried): {result[:200]}",
                )
                _post_policy_refusal_alert(config, task, result)
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
            elif task.attempt_count < task.max_attempts - 1 and not is_oom:
                # Exponential backoff: 1, 4, 16 minutes
                delay = 1 << (task.attempt_count * 2)
                db.set_task_pending_retry(conn, task_id, result, delay)
                db.log_task(conn, task_id, "warn", f"Task failed, will retry in {delay} minutes: {result[:200]}")
                # ISSUE-074: clear any deferred-op files this attempt accumulated
                # so the next attempt starts with a clean slate. Producers append
                # to these files, so without this, eventual success would replay
                # the failed attempt's ops alongside the successful one's.
                from .executor import get_user_temp_dir
                _purge_deferred_files_for_retry(
                    task, get_user_temp_dir(config, task.user_id),
                )
                # Same hazard for the event log: the task keeps its id across
                # retries, so a fresh EventWriter's seq=1 would collide with
                # this attempt's surviving rows on UNIQUE(task_id, seq). Clear
                # the slate so the log reflects the final attempt only.
                db.delete_task_events(conn, task_id)
            else:
                db.update_task_status(conn, task_id, "failed", error=result)
                db.log_task(conn, task_id, "error", f"Task failed permanently: {result[:500]}")

                if task.source_type in ("briefing", "scheduled"):
                    # Suppress user-facing error delivery for automated tasks.
                    # Errors are logged to DB and log_channel; no need to confuse users.
                    db.log_task(conn, task_id, "info", "Suppressed error delivery for automated task")
                elif target in ("talk", "both", "all") and talk_token:
                    # Use user-friendly error message, not raw error
                    friendly_error = _format_error_for_user(result)
                    post_talk_message = f"🐙 {friendly_error}"
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

    # Emit terminal task events + notify subscribers (brain path only). On a
    # retry-eligible failure the task isn't done — emit nothing terminal; the
    # event rows for this attempt were deleted in the retry branch so the next
    # attempt's seq counter starts clean (UNIQUE(task_id, seq) collision fix).
    if event_writer is not None:
        is_confirmation_request = bool(
            success
            and target in ("talk", "both")
            and talk_token
            and CONFIRMATION_PATTERN.search(result)
        )
        is_cancelled = (not success) and result == "Cancelled by user"
        is_policy = (not success) and _is_policy_refusal(result)
        is_oom = (not success) and "killed (likely out of memory)" in result
        will_retry = (
            (not success)
            and not is_cancelled
            and not is_policy
            and not is_oom
            and task.attempt_count < task.max_attempts - 1
        )
        if not will_retry:
            if is_confirmation_request:
                event_writer.emit("confirmation", {"prompt": result[:8000]})
            elif success:
                event_writer.emit("result", {
                    "text": result[:8000], "truncated": len(result) > 8000,
                })
            elif is_cancelled:
                event_writer.emit("cancelled")
            else:
                event_writer.emit("error", {
                    "message": result[:500], "stop_reason": "error",
                })
            event_writer.emit("done", {
                "stop_reason": "completed" if success else "error",
                "duration_seconds": round(event_writer.elapsed_seconds(), 1),
            })
            event_writer.finish()

    # Process deferred operations (subtasks, transaction tracking) on success
    if success and not (
        target in ("talk", "both")
        and talk_token
        and CONFIRMATION_PATTERN.search(result)
    ):
        from .executor import get_user_temp_dir
        user_temp_dir = get_user_temp_dir(config, task.user_id)
        _process_deferred_subtasks(config, task, user_temp_dir)
        _process_deferred_tracking(config, task, user_temp_dir)
        _process_deferred_sent_emails(config, task, user_temp_dir)
        _process_deferred_kv_ops(config, task, user_temp_dir)
        _process_deferred_kg_ops(config, task, user_temp_dir)
        _process_deferred_health_ops(config, task, user_temp_dir)
        _process_deferred_user_alerts(config, task, user_temp_dir)
        _deliver_deferred_email_output(config, task, user_temp_dir)
        _warn_unconsumed_deferred_files(task, user_temp_dir)
        _notify_confirmed_email_result(config, task, result)

    # Save briefing digest for deduplication in the next run
    if success and task.source_type == "briefing":
        from .skills.briefing import save_briefing_digest
        parsed_briefing = parse_briefing_json(result)
        digest_text = parsed_briefing["body"] if parsed_briefing else strip_briefing_preamble(result)
        save_briefing_digest(
            task.user_id, config, digest_text,
            conversation_token=task.conversation_token,
        )

    # The ack message is left as-is — it shows the last tool call as a compact
    # execution summary. Error / cancelled status edits are handled live by the
    # Talk subscriber's terminal-event handling.

    # Finalize log channel message with completion status
    if log_channel and log_channel_prefix:
        error_msg = result if not success else None
        # Read selected skills from DB (set during execute_task, not on local task object)
        selected_skills = None
        if config.scheduler.log_channel_show_skills:
            try:
                with db.get_db(config.db_path) as _conn:
                    refreshed = db.get_task(_conn, task_id)
                    if refreshed and refreshed.selected_skills:
                        selected_skills = json.loads(refreshed.selected_skills)
            except Exception:
                pass
        from .executor import _resolve_effort
        resolved_model = (task.model or "").strip() or config.model or None
        resolved_effort = _resolve_effort(task, config) or None
        _finalize_log_channel(
            config, task, log_channel, log_channel_prefix,
            log_callback, success, error=error_msg,
            skills=selected_skills,
            model=resolved_model, effort=resolved_effort,
        )

    # Deliver results outside DB context to avoid lock conflicts. The final
    # result is always a separate Talk message (the ack carries progress); the
    # Talk subscriber never posts the result, so no dedup is needed.
    response_msg_id = None
    if post_talk_message:
        response_msg_id = asyncio.run(post_result_to_talk(
            config, task, post_talk_message, use_reply_threading=True,
            reference_id=f"istota:task:{task.id}:result",
            target_token=talk_token,
        ))
    # Store bot's response message ID for reply tracking
    if response_msg_id and not is_failure_notify:
        try:
            with db.get_db(config.db_path) as conn:
                db.update_talk_response_id(conn, task_id, response_msg_id)
        except Exception as e:
            logger.debug("Failed to store talk_response_id for task %d: %s", task_id, e)

    # Cache the result so it's immediately available for context building.
    # The result is always posted as its own message, so its real Talk ID is
    # the cache key. The upsert preserves :result tags so the poller won't
    # overwrite them.
    cache_msg_id = response_msg_id

    if success and talk_token and not is_failure_notify and cache_msg_id:
        try:
            with db.get_db(config.db_path) as conn:
                cache_msg = {
                    "id": cache_msg_id,
                    "actorId": config.talk.bot_username,
                    "actorDisplayName": config.talk.bot_username,
                    "actorType": "users",
                    "message": post_talk_message or result,
                    "messageType": "comment",
                    "messageParameters": {},
                    "timestamp": int(time.time()),
                    "referenceId": f"istota:task:{task.id}:result",
                    "deleted": False,
                }
                db.upsert_talk_messages(conn, talk_token, [cache_msg])
        except Exception as e:
            logger.warning("Failed to cache result message for task %d: %s", task_id, e)
    if post_email:
        if task.source_type == "briefing":
            pb = parse_briefing_json(result)
            email_result = pb["body"] if pb else strip_briefing_preamble(result)
        else:
            email_result = result
        email_ok = asyncio.run(post_result_to_email(config, task, email_result))
        if not email_ok:
            with db.get_db(config.db_path) as conn:
                db.update_task_status(conn, task_id, "failed", error="Email delivery failed")
                db.log_task(conn, task_id, "error", "Task completed but email delivery failed")
    if post_ntfy:
        from .notifications import _send_ntfy
        if task.source_type == "briefing":
            pb = parse_briefing_json(result)
            ntfy_result = pb["body"] if pb else strip_briefing_preamble(result)
        else:
            ntfy_result = result
        _send_ntfy(config, task.user_id, ntfy_result, title=f"Task {task_id}")
    if call_file_handler:
        handle_tasks_file_completion(config, task, file_handler_success, result)

    return task_id, success


async def post_result_to_talk(
    config: Config, task: db.Task, message: str,
    *, use_reply_threading: bool = False,
    reference_id: str | None = None,
    target_token: str | None = None,
) -> int | None:
    """Post a result message to Talk. Returns the Talk message ID of the last sent message.

    Long messages are split into multiple parts sent sequentially.

    `target_token` overrides `task.conversation_token` for the actual post —
    use it when the task's stored token isn't a real Talk room (see
    `_talk_target_for_delivery` for the email-source synthetic-token case).
    """
    token = target_token or task.conversation_token
    if not config.nextcloud.url or not token:
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
                token, part, reply_to=reply_to,
                reference_id=reference_id,
            )
            msg_id = response.get("ocs", {}).get("data", {}).get("id")
        return msg_id
    except Exception as e:
        # Log but don't fail the task — use Python logger to avoid DB lock issues
        logger.error("Failed to post to Talk (task %s): %s: %r", task.id, type(e).__name__, e)
        return None


def _parse_email_output(message: str) -> dict | None:
    """
    Parse Claude Code's email output as JSON.

    Expected format:
        {"subject": "...", "body": "...", "format": "plain"|"html"}

    Handles common Claude quirks:
    - Markdown code fences (```json ... ```)
    - Preamble text before the JSON object
    - Trailing text after the JSON object

    Returns None if no structured email JSON is found — this prevents
    double-sending when Claude already sent the email via `email send`.
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

    # Try 4: normalize Unicode smart quotes to ASCII and retry.
    # Models sometimes silently replace ASCII quotes with smart quotes
    # (U+201C/U+201D/U+2018/U+2019) when echoing JSON, which breaks parsing.
    _SMART_QUOTE_MAP = {
        "\u201c": '"',  # left double
        "\u201d": '"',  # right double
        "\u2018": "'",  # left single
        "\u2019": "'",  # right single
    }
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace:last_brace + 1]
        for smart, ascii_char in _SMART_QUOTE_MAP.items():
            candidate = candidate.replace(smart, ascii_char)
        result = _try_parse(candidate)
        if result:
            logger.warning("Email JSON required smart-quote normalization to parse")
            return result

    # No structured email JSON found.  Log a warning if it looks like broken
    # JSON — helps diagnose transcription corruption.  Return None so the
    # caller knows there is no structured output (prevents double-send when
    # Claude already sent the email directly via `email send`).
    if first_brace != -1 and '"format"' in text:
        logger.warning(
            "Email output looks like malformed JSON but could not be parsed"
        )
    return None


def _load_deferred_email_output(config: Config, task: db.Task) -> dict | None:
    """Load email output from a deferred JSON file written by the email output tool.

    Returns parsed dict with subject/body/format keys, or None if no file exists.
    """
    from .executor import get_user_temp_dir
    user_temp_dir = get_user_temp_dir(config, task.user_id)
    path = user_temp_dir / f"task_{task.id}_email_output.json"
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text())
        path.unlink(missing_ok=True)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Bad deferred email output file for task %d: %s", task.id, e)
        path.unlink(missing_ok=True)
        return None

    if not isinstance(data, dict) or "body" not in data or "format" not in data:
        logger.warning("Deferred email output file for task %d missing required fields", task.id)
        return None

    fmt = data["format"]
    if fmt not in ("plain", "html"):
        fmt = "plain"

    return {
        "subject": data.get("subject"),
        "body": data["body"],
        "format": fmt,
    }


def _record_sent_email(
    config: Config,
    task: db.Task,
    message_id: str,
    to_addr: str,
    subject: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> None:
    """Record an outbound email for emissary thread matching (non-critical)."""
    try:
        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id=task.user_id,
                message_id=message_id,
                to_addr=to_addr,
                subject=subject,
                task_id=task.id,
                in_reply_to=in_reply_to,
                references=references,
                conversation_token=task.conversation_token,
                talk_delivery_token=task.talk_delivery_token,
            )
    except Exception as e:
        logger.warning("Failed to record sent email for task %d: %s", task.id, e)


async def post_result_to_email(config: Config, task: db.Task, message: str) -> bool:
    """Send task result as email reply, or fresh email for scheduled/briefing jobs.

    Returns True on success, False on failure.
    """
    from .skills.email import send_email

    # Prefer deferred email output file (tool-based, no transcription risk)
    # over inline JSON parsing (legacy, subject to smart-quote corruption).
    # If neither source provides structured output, fall back to legacy briefing
    # path (raw model output stripped of markdown) for briefing tasks, or skip
    # sending for other tasks (Claude likely sent directly via `email send`).
    parsed = _load_deferred_email_output(config, task) or _parse_email_output(message)

    if parsed is None and task.source_type == "briefing":
        # Legacy path: model output is Talk-formatted text, send directly
        user_config = config.users.get(task.user_id)
        if not user_config or not user_config.email_addresses:
            logger.warning("No email address for user %s (task %d)", task.user_id, task.id)
            return False
        try:
            email_config = get_email_config(config)
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
    if parsed is None:
        logger.info(
            "No structured email output for task %d; skipping scheduler delivery "
            "(email was likely sent directly during execution)",
            task.id,
        )
        return True

    # Safety net: strip markdown from briefing plain text emails (briefing content
    # is generated with Talk formatting; strip it for email delivery)
    if task.source_type == "briefing" and parsed["format"] == "plain":
        parsed["body"] = _strip_markdown(parsed["body"])

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

            sent_message_id = reply_to_email(
                to_addr=processed_email.sender_email,
                subject=subject,
                body=parsed["body"],
                config=email_config,
                from_addr=config.email.bot_email,
                in_reply_to=processed_email.message_id,
                references=references,
                content_type=parsed["format"],
            )
            _record_sent_email(
                config, task, sent_message_id,
                to_addr=processed_email.sender_email,
                subject=subject,
                in_reply_to=processed_email.message_id,
                references=references,
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
            sent_message_id = send_email(
                to=user_config.email_addresses[0],
                subject=subject,
                body=parsed["body"],
                config=email_config,
                from_addr=config.email.bot_email,
                content_type=parsed["format"],
            )
            _record_sent_email(
                config, task, sent_message_id,
                to_addr=user_config.email_addresses[0],
                subject=subject,
            )
            return True
        except Exception as e:
            logger.error("Failed to send email (task %s): %s", task.id, e)
            return False


def check_briefings(db_path, app_config: Config) -> list[int]:
    """
    Check for briefings that should run and queue them as tasks.

    Uses three phases to avoid holding DB locks during slow network I/O:
    1. Short DB read to check which briefings are due
    2. Network pre-fetch (market data, newsletters) with NO DB connection
    3. Short DB write to create tasks

    Args:
        db_path: Path to the database file
        app_config: Application config with user briefings

    Returns:
        List of created task IDs
    """
    # Phase 1: Short DB read — check which briefings are due
    due_briefings: list[tuple[str, str, "BriefingConfig"]] = []

    with db.get_db(db_path) as conn:
        for user_id, user_config in app_config.users.items():
            briefings = get_briefings_for_user(app_config, user_id)
            if not briefings:
                continue

            # Resolve from the live user_profiles DB row (reusing conn) so a
            # web-UI timezone change moves the briefing schedule without a
            # daemon restart (ISSUE-099).
            user_tz_str = app_config.resolve_user_timezone(user_id, conn=conn)

            try:
                user_tz = ZoneInfo(user_tz_str)
            except Exception:
                user_tz = ZoneInfo("UTC")
                user_tz_str = "UTC"

            now = _now(user_tz)
            # Use naive wall-clock times for croniter to avoid DST bugs.
            # croniter miscomputes next fire time when a tz-aware datetime
            # crosses a DST boundary (e.g. PST→PDT), causing double-fires.
            now_naive = now.replace(tzinfo=None)

            for briefing in briefings:
                if not briefing.cron:
                    continue
                if not briefing.conversation_token and briefing.output in ("talk", "both"):
                    continue

                should_run = False
                last_run_at = db.get_briefing_last_run(conn, user_id, briefing.name)

                if last_run_at:
                    last_run = datetime.fromisoformat(last_run_at)
                    if last_run.tzinfo is None:
                        last_run = last_run.replace(tzinfo=ZoneInfo("UTC"))
                    base = last_run.astimezone(user_tz).replace(tzinfo=None)
                    cron = croniter(briefing.cron, base)
                    next_run = cron.get_next(datetime)
                    should_run = now_naive >= next_run
                else:
                    today_start = now_naive.replace(hour=0, minute=0, second=0, microsecond=0)
                    cron = croniter(briefing.cron, today_start)
                    next_run = cron.get_next(datetime)
                    should_run = now_naive >= next_run

                if should_run and _is_stale_fire(
                    f"briefing {user_id}/{briefing.name}",
                    next_run, now_naive,
                    app_config.scheduler.cron_max_staleness_minutes,
                ):
                    db.set_briefing_last_run(conn, user_id, briefing.name)
                    continue

                if should_run:
                    due_briefings.append((user_id, user_tz_str, briefing))

    if not due_briefings:
        return []

    # Phase 2: Network pre-fetch — NO DB connection held
    # build_briefing_prompt does yfinance, FinViz, IMAP fetches which can
    # take minutes if endpoints are slow/down. Doing this outside the DB
    # transaction prevents "database is locked" errors for other threads.
    prepared: list[tuple[str, "BriefingConfig", str]] = []
    for user_id, user_tz_str, briefing in due_briefings:
        prompt = build_briefing_prompt(
            briefing, user_id, app_config, user_tz_str,
        )
        prepared.append((user_id, briefing, prompt))

    # Phase 3: Short DB write — create tasks and update last_run
    created_tasks = []
    with db.get_db(db_path) as conn:
        for user_id, briefing, prompt in prepared:
            task_id = db.create_task(
                conn,
                prompt=prompt,
                user_id=user_id,
                source_type="briefing",
                conversation_token=briefing.conversation_token,
                output_target=briefing.output,
                priority=8,
                queue="background",
            )
            db.set_briefing_last_run(conn, user_id, briefing.name)
            created_tasks.append(task_id)

    return created_tasks


def check_briefing_triggers(db_path, config: Config) -> list[int]:
    """Check for briefing trigger files from the NC app and create tasks.

    Trigger files are written by the NC app to request an immediate briefing
    run. Each file is ``{triggers_dir}/briefing_{user_id}_{briefing_name}.json``
    containing ``{"user_id": "...", "briefing_name": "..."}``.

    Returns list of created task IDs.
    """
    if config.config_path is None:
        return []

    triggers_dir = config.config_path.parent / "triggers"
    if not triggers_dir.is_dir():
        return []

    created_tasks = []
    for trigger_file in triggers_dir.glob("briefing_*.json"):
        try:
            trigger = json.loads(trigger_file.read_text())
            user_id = trigger.get("user_id", "")
            briefing_name = trigger.get("briefing_name", "")

            if not user_id or not briefing_name:
                logger.warning("Invalid trigger file %s: missing user_id or briefing_name", trigger_file)
                trigger_file.unlink()
                continue

            user_config = config.get_user(user_id)
            if not user_config:
                logger.warning("Trigger for unknown user %s, skipping", user_id)
                trigger_file.unlink()
                continue

            # Find the matching briefing
            briefings = get_briefings_for_user(config, user_id)
            briefing = next((b for b in briefings if b.name == briefing_name), None)
            if not briefing:
                logger.warning("Trigger for unknown briefing %s/%s, skipping", user_id, briefing_name)
                trigger_file.unlink()
                continue

            # Live DB timezone so a web-UI change is honored without restart
            # (ISSUE-099); the briefing-trigger path holds no conn here.
            user_tz_str = config.resolve_user_timezone(user_id)

            # Build and queue the briefing task
            prompt = build_briefing_prompt(briefing, user_id, config, user_tz_str)
            with db.get_db(db_path) as conn:
                task_id = db.create_task(
                    conn,
                    prompt=prompt,
                    user_id=user_id,
                    source_type="briefing",
                    conversation_token=briefing.conversation_token,
                    output_target=briefing.output,
                    priority=8,
                    queue="background",
                )
            created_tasks.append(task_id)
            logger.info("Triggered briefing %s for %s (task %d)", briefing_name, user_id, task_id)
        except Exception as e:
            logger.error("Error processing trigger %s: %s", trigger_file, e)
        finally:
            # Always delete the trigger file after processing
            try:
                trigger_file.unlink(missing_ok=True)
            except Exception:
                pass

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
                    # Remove empty directories, but only once the directory
                    # itself has gone untouched past the retention window.
                    # execute_task creates an empty per-user temp dir and writes
                    # its prompt file a few seconds later; without this age gate
                    # a concurrent cleanup tick would rmdir that still-empty dir
                    # mid-task and break the write (the temp-dir race).
                    try:
                        if path.stat().st_mtime < cutoff:
                            path.rmdir()  # only succeeds if empty
                    except OSError:
                        pass
            except Exception as e:
                logger.debug(f"Could not process temp path {path}: {e}")
        return count

    deleted = _cleanup_dir(config.temp_dir)
    return deleted


def check_db_health(config: Config) -> list[CheckReport]:
    """Sweep ``PRAGMA quick_check`` + ``REINDEX`` across all known SQLite DBs.

    Covers the framework DB and every configured user's module DBs
    (feeds, health, location, money). Each check is independent —
    one failed open or one disabled module doesn't stop the sweep.

    Returns the per-DB :class:`CheckReport` list so callers (tests,
    operator tooling) can inspect outcomes. The scheduler tick ignores
    the return value; results are already logged.
    """
    reports: list[CheckReport] = []

    # 1. Framework DB (local disk, but cheap to check and worth confirming).
    reports.append(check_and_repair(config.db_path, label="framework"))

    # 2. Per-user module DBs on the Nextcloud mount. Probe the filesystem
    #    rather than calling each module's resolver: resolvers raise for
    #    disabled-module, missing-mount, missing-user, etc., and we don't
    #    want any of those to skip a *file* that's actually on disk and
    #    might be corrupt.
    mount = getattr(config, "nextcloud_mount_path", None)
    if mount is None:
        return reports

    from .storage import get_user_bot_path  # local import: avoids cycle

    for user_id in config.users:
        try:
            bot_path = get_user_bot_path(user_id, config.bot_dir_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "db_health_user_path_failed user=%s err=%s", user_id, exc,
            )
            continue
        user_root = Path(mount) / bot_path.lstrip("/")
        for module in ("feeds", "health", "location", "money"):
            db_path = user_root / module / "data" / f"{module}.db"
            reports.append(
                check_and_repair(db_path, label=f"{module}:{user_id}")
            )

    return reports


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

        # 1b. Recover stuck locked/running tasks (mirrors claim_task recovery
        # but runs even when no tasks are being claimed)
        stuck = db.fail_stuck_locked_running_tasks(
            conn, sched.max_retry_age_minutes,
            stuck_running_minutes=_stuck_running_minutes(sched),
            heartbeat_stuck_minutes=sched.worker_stuck_minutes,
        )
        for task_info in stuck:
            logger.warning(
                "Recovered stuck task: task %d (user: %s, status: %s)",
                task_info["id"], task_info["user_id"],
                task_info.get("source_type", "unknown"),
            )

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

    # 6. Clean up talk message cache
    with db.get_db(config.db_path) as conn:
        deleted_msgs = db.cleanup_old_talk_messages(conn, sched.talk_cache_max_per_conversation)
        if deleted_msgs > 0:
            logger.info(f"Cleaned up {deleted_msgs} old talk message(s)")

    # 7. Clean up old temp files
    if sched.temp_file_retention_days > 0:
        try:
            deleted_files = cleanup_old_temp_files(config, sched.temp_file_retention_days)
            if deleted_files > 0:
                logger.info(f"Deleted {deleted_files} old temp file(s)")
        except Exception as e:
            logger.error(f"Error cleaning up temp files: {e}")

    # 8. Clean up old location pings (per-user location.db)
    if sched.location_ping_retention_days > 0:
        from . import location as _location  # noqa: PLC0415

        total_pings = 0
        # Reuse one framework-DB conn for the per-user is_module_enabled
        # checks to avoid opening N+ short-lived sqlite connections per
        # cleanup tick (one of the FD-churn paths that produced EMFILE).
        with db.get_db(config.db_path) as fw_conn:
            enabled_users = _location.list_users(config, conn=fw_conn)
            for uid in enabled_users:
                try:
                    ctx = _location.resolve_for_user(uid, config, conn=fw_conn)
                except _location.UserNotFoundError:
                    continue
                try:
                    with _location.connect(ctx.db_path) as conn:
                        deleted = _location.db.cleanup_old_pings(
                            conn, sched.location_ping_retention_days,
                        )
                        conn.commit()
                    total_pings += deleted
                except Exception:
                    logger.exception(
                        "Failed to clean up location pings for user=%s", uid,
                    )
        if total_pings > 0:
            logger.info(f"Cleaned up {total_pings} old location ping(s)")

    # 8b. Reconcile visits from pings (batch cleanup of state-machine drift)
    if config.location.reconcile_enabled:
        try:
            _reconcile_visits_for_all_users(config)
        except Exception as e:
            logger.error(f"Error reconciling visits: {e}")

    # 9. Clean up old Claude session logs
    if sched.temp_file_retention_days > 0:
        try:
            deleted_logs = cleanup_old_claude_logs(sched.temp_file_retention_days)
            if deleted_logs > 0:
                logger.info(f"Deleted {deleted_logs} old Claude session log(s)")
        except Exception as e:
            logger.error(f"Error cleaning up Claude logs: {e}")


def _reconcile_visits_for_all_users(config: Config) -> None:
    """Re-derive the visits table for each user with the location module
    enabled.

    Operates on a window ending ``reconcile_buffer_minutes`` before now
    so the currently-open visit is never rewritten. Only closed visits
    in the window are replaced.

    Per-user file scope: every user with an enabled location module is
    a candidate. The legacy ``[[resources]]`` overland filter was dead
    code post-modules-refactor (the overland token moved into the
    secrets table) and is gone.
    """
    from . import location as _location  # noqa: PLC0415

    loc = config.location
    now = datetime.now(timezone.utc)
    until = now - timedelta(minutes=loc.reconcile_buffer_minutes)
    since = until - timedelta(hours=loc.reconcile_lookback_hours)
    since_s = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_s = until.strftime("%Y-%m-%dT%H:%M:%SZ")

    # One framework-DB conn for the module-enabled lookups across users —
    # avoids the FD churn that triggered EMFILE on prod.
    with db.get_db(config.db_path) as fw_conn:
        enabled_users = _location.list_users(config, conn=fw_conn)
        for uid in enabled_users:
            try:
                ctx = _location.resolve_for_user(uid, config, conn=fw_conn)
            except _location.UserNotFoundError:
                continue
            try:
                with _location.connect(ctx.db_path) as conn:
                    n = _location.db.reconcile_visits(
                        conn, since_s, until_s,
                        grace_minutes=loc.reconcile_grace_minutes,
                        min_pings=loc.reconcile_min_pings,
                        min_dwell_sec=loc.reconcile_min_dwell_sec,
                        accuracy_threshold_m=loc.accuracy_threshold_m,
                    )
                    conn.commit()
                if n:
                    logger.info(
                        "Reconciled %d visit(s) for user=%s window=[%s,%s)",
                        n, uid, since_s, until_s,
                    )
            except Exception:
                logger.exception(
                    "Visit reconciliation failed for user=%s", uid,
                )


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
    from .cron_loader import (
        _MODULE_JOB_PREFIX,
        load_cron_jobs,
        migrate_db_jobs_to_file,
        sync_cron_jobs_to_db,
    )

    for user_id in app_config.users:
        try:
            file_jobs = load_cron_jobs(app_config, user_id)
            if file_jobs is not None:
                # Count only user-defined DB jobs when deciding whether to
                # migrate-to-file; module-managed jobs don't belong in CRON.md.
                user_db_jobs = [
                    j for j in db.get_user_scheduled_jobs(conn, user_id)
                    if not j.name.startswith(_MODULE_JOB_PREFIX)
                ]
                if not file_jobs and user_db_jobs:
                    # File exists but empty (e.g. seeded template), DB has jobs —
                    # write DB jobs into the file instead of wiping them
                    migrate_db_jobs_to_file(conn, app_config, user_id, overwrite=True)
                else:
                    sync_cron_jobs_to_db(
                        conn, user_id, file_jobs,
                        is_admin=app_config.is_admin(user_id),
                    )
            else:
                # No CRON.md — try one-time migration from DB
                migrate_db_jobs_to_file(conn, app_config, user_id)
        except Exception as e:
            logger.error("Error syncing CRON.md for %s: %s", user_id, e)

    # Sync module-managed jobs (e.g. money's run_scheduled).
    # These are not user-editable via CRON.md; their definitions come from
    # the module's jobs.py and the user's istota resource entry.
    try:
        _sync_money_module_jobs(conn, app_config)
    except Exception as e:
        logger.error("Error syncing money module jobs: %s", e)

    try:
        _sync_feeds_module_jobs(conn, app_config)
    except Exception as e:
        logger.error("Error syncing feeds module jobs: %s", e)

    try:
        _sync_health_module_jobs(conn, app_config)
    except Exception as e:
        logger.error("Error syncing health module jobs: %s", e)


def _sync_module_jobs(
    conn,
    app_config: Config,
    *,
    module_name: str,
    module_prefix: str,
    resolve_for_user,
    jobs_for_user,
    user_not_found_exc: type[Exception],
    on_first_seed=None,
) -> None:
    """Seed/refresh ``{module_prefix}*`` scheduled jobs from each user's module config.

    Shared engine for ``_sync_money_module_jobs`` and
    ``_sync_feeds_module_jobs``. Idempotent: existing rows are updated when
    cron or command differs; obsolete rows are deleted; users with the
    module disabled have all their ``{module_prefix}*`` rows cleaned up.

    ``on_first_seed(conn, user_id, job_dict)`` — optional callback invoked
    once after each freshly-inserted row, before commit. Feeds uses this to
    queue an immediate one-shot poll for the ``run_scheduled`` row so newly
    provisioned users don't wait up to 5 minutes for the first tick.
    """
    for user_id in app_config.users:
        uc = app_config.users.get(user_id)
        if uc is None:
            continue

        if not app_config.is_module_enabled(user_id, module_name, conn=conn):
            # Drop any stale module rows
            conn.execute(
                "DELETE FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
                (user_id, f"{module_prefix}%"),
            )
            continue

        try:
            module_ctx = resolve_for_user(user_id, app_config, conn=conn)
        except user_not_found_exc as e:
            logger.warning(
                "Could not resolve %s config for %s: %s", module_name, user_id, e,
            )
            continue

        wanted = jobs_for_user(module_ctx, user_id)
        wanted_by_name = {j["name"]: j for j in wanted}

        # Rescue auto-disabled module rows. Two failure waves stuck rows
        # in the past:
        #   1. legacy command-task shape + non-admin user → admin-gate
        #      ("command-type tasks are admin-only"), fixed by cc0bd54.
        #   2. migrated skill-task shape, but `claim_task` didn't return
        #      `skill`/`skill_args` so the task fell through to the LLM
        #      path with an empty prompt, fixed by 027eb1a.
        #
        # Wave 1 left a known last_error string we could match. Wave 2's
        # last_error varies (timeouts, malformed output, …), so we drop
        # the error-string predicate and trust two structural signals
        # instead: ``_module.*`` rows have no operator-pause UI, so any
        # row with ``consecutive_failures > 0`` was auto-disabled rather
        # than operator-disabled, and the ``last_run_at < now - 1h`` gate
        # prevents a rescue→fail→rescue loop if a row is genuinely broken
        # (auto-disable still kicks in within ~25min of rescue at */5;
        # the 1h cooldown caps the rescue rate).
        rescued = conn.execute(
            "UPDATE scheduled_jobs "
            "SET enabled = 1, consecutive_failures = 0, last_error = NULL "
            "WHERE user_id = ? AND name LIKE ? AND enabled = 0 "
            "AND consecutive_failures > 0 "
            "AND (last_run_at IS NULL "
            "     OR last_run_at < datetime('now', '-1 hour'))",
            (user_id, f"{module_prefix}%"),
        ).rowcount
        if rescued:
            logger.info(
                "Re-enabled %d auto-disabled module job(s) for user %s",
                rescued, user_id,
            )

        existing_rows = list(conn.execute(
            "SELECT id, name, cron_expression, command, skill, skill_args, "
            "skip_log_channel "
            "FROM scheduled_jobs WHERE user_id = ? AND name LIKE ?",
            (user_id, f"{module_prefix}%"),
        ))
        existing_by_name = {row[1]: row for row in existing_rows}

        for name, j in wanted_by_name.items():
            row = existing_by_name.get(name)
            if row is None:
                conn.execute(
                    "INSERT INTO scheduled_jobs "
                    "(user_id, name, cron_expression, prompt, command, "
                    "skill, skill_args, enabled, skip_log_channel) "
                    "VALUES (?, ?, ?, '', NULL, ?, ?, 1, 1)",
                    (user_id, name, j["cron"], j["skill"], j["skill_args"]),
                )
                logger.info(
                    "Seeded module job '%s' for user %s", name, user_id,
                )
                if on_first_seed is not None:
                    on_first_seed(conn, user_id, j)
            else:
                legacy_command = row[3] is not None
                drift = (
                    row[2] != j["cron"]
                    or legacy_command  # legacy command shape — migrate
                    or row[4] != j["skill"]
                    or row[5] != j["skill_args"]
                    or not bool(row[6])
                )
                if drift:
                    # Don't bump last_run_at here — backfilling skip_log_channel
                    # on existing rows would otherwise defer the next scheduled
                    # run by one full cron interval (up to 24h for daily jobs).
                    extra_sql = ""
                    if legacy_command:
                        # Non-admin users hit the admin gate on the old
                        # command-task path and got auto-disabled. The migration
                        # to skill-task shape removes that failure mode, so
                        # rescue the row's enabled/failure state in the same
                        # update.
                        extra_sql = (
                            ", enabled = 1, consecutive_failures = 0, "
                            "last_error = NULL"
                        )
                    conn.execute(
                        "UPDATE scheduled_jobs "
                        "SET cron_expression = ?, command = NULL, "
                        "skill = ?, skill_args = ?, skip_log_channel = 1"
                        f"{extra_sql} "
                        "WHERE id = ?",
                        (j["cron"], j["skill"], j["skill_args"], row[0]),
                    )
                    logger.info(
                        "Updated module job '%s' for user %s%s",
                        name, user_id,
                        " (rescued from auto-disable)" if legacy_command else "",
                    )

        for name, row in existing_by_name.items():
            if name not in wanted_by_name:
                conn.execute("DELETE FROM scheduled_jobs WHERE id = ?", (row[0],))
                logger.info(
                    "Removed obsolete module job '%s' for user %s",
                    name, user_id,
                )

    conn.commit()


def _sync_money_module_jobs(conn, app_config: Config) -> None:
    """Seed/refresh ``_module.money.*`` scheduled jobs from each user's money config."""
    try:
        from istota.money import UserNotFoundError, resolve_for_user
        from istota.money.jobs import MODULE_PREFIX, jobs_for_user
    except ImportError:
        # money extra not installed
        return

    _sync_module_jobs(
        conn, app_config,
        module_name="money",
        module_prefix=MODULE_PREFIX,
        resolve_for_user=resolve_for_user,
        jobs_for_user=jobs_for_user,
        user_not_found_exc=UserNotFoundError,
    )


def _sync_feeds_module_jobs(conn, app_config: Config) -> None:
    """Seed/refresh ``_module.feeds.*`` scheduled jobs for users with feeds enabled."""
    try:
        from istota.feeds import UserNotFoundError, resolve_for_user
        from istota.feeds.jobs import MODULE_PREFIX, jobs_for_user
    except ImportError:
        # feeds extra not installed
        return

    run_scheduled_name = f"{MODULE_PREFIX}run_scheduled"

    def _queue_initial_poll(conn, user_id: str, j: dict) -> None:
        # First-time seed: queue an immediate one-shot poll so newly
        # provisioned users see their seeded subscriptions populate
        # without waiting up to 5 minutes for the first cron tick.
        if j["name"] != run_scheduled_name:
            return
        task_id = db.create_task(
            conn,
            prompt="",
            user_id=user_id,
            source_type="scheduled",
            priority=5,
            skip_log_channel=True,
            skill=j["skill"],
            skill_args=j["skill_args"],
            queue="background",
        )
        logger.info(
            "Queued initial feeds poll for user %s as task %d",
            user_id, task_id,
        )

    _sync_module_jobs(
        conn, app_config,
        module_name="feeds",
        module_prefix=MODULE_PREFIX,
        resolve_for_user=resolve_for_user,
        jobs_for_user=jobs_for_user,
        user_not_found_exc=UserNotFoundError,
        on_first_seed=_queue_initial_poll,
    )


def _sync_health_module_jobs(conn, app_config: Config) -> None:
    """Seed/refresh ``_module.health.*`` scheduled jobs for users with the
    health module enabled and a Garmin connection. ``jobs_for_user``
    returns an empty list for users without stored Garmin tokens, so
    those users have their stale ``_module.health.*`` rows removed.

    On first seed (a freshly-inserted job row) we also queue a one-shot
    30-day backfill task so the user sees their history populate without
    waiting up to 6h for the first scheduled tick.
    """
    try:
        from istota.health import UserNotFoundError, resolve_for_user
        from istota.health.jobs import (
            GARMIN_SYNC_JOB,
            MODULE_PREFIX,
            jobs_for_user,
        )
    except ImportError:
        return

    backfill_name = GARMIN_SYNC_JOB.name

    def _queue_initial_backfill(conn, user_id: str, j: dict) -> None:
        if j["name"] != backfill_name:
            return
        # Resolve the freshly-inserted job row's id so the backfill task
        # is linked back to it via ``scheduled_job_id`` (M2). Without
        # this the failure-tracking path doesn't fire on the backfill,
        # so a permanently-broken Garmin connection would silently fail
        # the initial 30-day pull and never show up in the operator's
        # auto-disable counter.
        row = conn.execute(
            "SELECT id FROM scheduled_jobs WHERE user_id = ? AND name = ?",
            (user_id, j["name"]),
        ).fetchone()
        scheduled_job_id = row[0] if row else None

        task_id = db.create_task(
            conn,
            prompt="",
            user_id=user_id,
            source_type="scheduled",
            priority=5,
            skip_log_channel=True,
            skill="health",
            skill_args=json.dumps(["garmin-sync", "--days-back", "30"]),
            queue="background",
            scheduled_job_id=scheduled_job_id,
        )
        logger.info(
            "Queued initial Garmin backfill for user %s as task %d (job_id=%s)",
            user_id, task_id, scheduled_job_id,
        )

    _sync_module_jobs(
        conn, app_config,
        module_name="health",
        module_prefix=MODULE_PREFIX,
        resolve_for_user=resolve_for_user,
        jobs_for_user=jobs_for_user,
        user_not_found_exc=UserNotFoundError,
        on_first_seed=_queue_initial_backfill,
    )


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
        # Live DB timezone (reusing conn) so a web-UI change moves the job
        # schedule without a daemon restart (ISSUE-099); falls back to UTC.
        user_tz_str = app_config.resolve_user_timezone(user_id, conn=conn)
        try:
            user_tz = ZoneInfo(user_tz_str)
        except Exception:
            user_tz = ZoneInfo("UTC")

        now = _now(user_tz)
        # Use naive wall-clock times for croniter to avoid DST bugs.
        # croniter miscomputes next fire time when a tz-aware datetime
        # crosses a DST boundary (e.g. PST→PDT), causing double-fires.
        now_naive = now.replace(tzinfo=None)

        for job in user_jobs:
            should_run = False

            if job.last_run_at:
                last_run = datetime.fromisoformat(job.last_run_at)
                if last_run.tzinfo is None:
                    # DB stores UTC via datetime('now')
                    last_run = last_run.replace(tzinfo=ZoneInfo("UTC"))
                base = last_run.astimezone(user_tz).replace(tzinfo=None)
                cron = croniter(job.cron_expression, base)
                next_run = cron.get_next(datetime)
                should_run = now_naive >= next_run
                logger.debug(
                    "Job '%s': last_run=%s next_run=%s now=%s should_run=%s",
                    job.name, last_run, next_run, now_naive, should_run,
                )
            else:
                # Use created_at as base so jobs don't fire immediately
                # when the cron time has already passed today
                if job.created_at:
                    base = datetime.fromisoformat(job.created_at)
                    if base.tzinfo is None:
                        # DB stores UTC via datetime('now')
                        base = base.replace(tzinfo=ZoneInfo("UTC"))
                    base = base.astimezone(user_tz).replace(tzinfo=None)
                else:
                    base = now_naive.replace(hour=0, minute=0, second=0, microsecond=0)
                cron = croniter(job.cron_expression, base)
                next_run = cron.get_next(datetime)
                should_run = now_naive >= next_run
                logger.debug(
                    "Job '%s' (never run): base=%s next_run=%s now=%s should_run=%s",
                    job.name, base, next_run, now_naive, should_run,
                )

            if should_run and _is_stale_fire(
                f"job {job.user_id}/{job.name}",
                next_run, now_naive,
                app_config.scheduler.cron_max_staleness_minutes,
            ):
                db.set_scheduled_job_last_run(conn, job.id)
                continue

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
                    skip_log_channel=job.skip_log_channel,
                    scheduled_job_id=job.id,
                    command=job.command,
                    skill=job.skill,
                    skill_args=job.skill_args,
                    queue="background",
                    # Resolve aliases (role / provider) to canonical IDs at
                    # task-creation time so the DB stays canonical, matching
                    # the talk-poller's !model prefix path. Executor also
                    # resolves as defense-in-depth, so legacy rows still work.
                    model=make_brain(app_config.brain).resolve_model_name(job.model) or None,
                    effort=job.effort or None,
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

    # Check briefings (manages own DB connections to avoid holding locks during network I/O)
    briefing_tasks = check_briefings(config.db_path, config)
    if briefing_tasks:
        logger.info("Queued %d briefing(s)", len(briefing_tasks))

    # Check scheduled jobs and sleep cycles
    with db.get_db(config.db_path) as conn:
        scheduled_tasks = check_scheduled_jobs(conn, config)
        if scheduled_tasks:
            logger.info("Queued %d scheduled job(s)", len(scheduled_tasks))

        # Check sleep cycles
        try:
            from .memory.sleep_cycle import check_sleep_cycles
            sleep_users = check_sleep_cycles(conn, config)
            if sleep_users:
                logger.info("Ran sleep cycle for %d user(s): %s", len(sleep_users), ", ".join(sleep_users))
        except Exception as e:
            logger.error("Error running sleep cycles: %s", e)

        # Check channel sleep cycles
        try:
            from .memory.sleep_cycle import check_channel_sleep_cycles
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
    logger.info("STARTUP DB health check interval: %ds", config.scheduler.db_health_check_interval)
    logger.info("STARTUP Scheduled job check interval: %ds", config.scheduler.briefing_check_interval)
    logger.info("STARTUP Cleanup interval: %ds", config.scheduler.briefing_check_interval)
    logger.info("STARTUP Confirmation timeout: %d min", config.scheduler.confirmation_timeout_minutes)
    logger.info("STARTUP Task retention: %d days", config.scheduler.task_retention_days)
    logger.info("STARTUP Email retention: %d days", config.scheduler.email_retention_days)
    logger.info("STARTUP Temp file retention: %d days", config.scheduler.temp_file_retention_days)

    # Security status checks
    # Linux + bubblewrap is the only supported deployment configuration.
    # Other configurations are for development only and provide no isolation guarantees.
    from .executor import _bwrap_available
    multi_user = len(config.users) > 1
    if config.security.sandbox_enabled and not _bwrap_available():
        if multi_user:
            logger.warning(
                "SECURITY UNSUPPORTED CONFIGURATION: sandbox_enabled but bubblewrap unavailable "
                "with %d users configured — no filesystem isolation between users. "
                "Linux + bubblewrap is the only supported multi-user deployment.",
                len(config.users),
            )
        else:
            logger.warning(
                "SECURITY Sandbox enabled but bubblewrap unavailable (single-user, dev-only configuration)"
            )
    elif config.security.sandbox_enabled:
        logger.info("SECURITY Sandbox enabled with bubblewrap")
    else:
        if multi_user:
            logger.warning(
                "SECURITY UNSUPPORTED CONFIGURATION: sandbox_enabled=false with %d users configured — "
                "no isolation between users. Linux + bubblewrap is the only supported multi-user deployment.",
                len(config.users),
            )
        else:
            logger.warning(
                "SECURITY Sandbox explicitly disabled — no isolation guarantees (dev-only configuration)"
            )
    logger.info("SECURITY Skill proxy: %s", "enabled" if config.security.skill_proxy_enabled else "disabled")
    logger.info("SECURITY Network proxy: %s", "enabled" if config.security.network.enabled else "disabled")

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

    # One-time / idempotent: copy tier-2 credentials declared in TOML resource
    # extras (monarch_email/password, karakeep api_key, …) into the encrypted
    # secrets table so the web UI can read them. Skipped when ISTOTA_SECRET_KEY
    # is unset; later starts skip rows that already exist.
    try:
        from . import secrets_store  # noqa: PLC0415

        secrets_store.import_from_user_configs(config.db_path, config.users)
    except Exception as e:  # noqa: BLE001
        logger.warning("Secrets import skipped: %s", e)

    # Phase 6: migrate per-user TOML profile fields into the user_profiles
    # table on first run. Idempotent — only writes rows that don't exist.
    try:
        from . import user_profiles as _up  # noqa: PLC0415

        _up.import_from_user_configs(config.db_path, config.users)
        # Re-apply DB rows onto config.users so the in-memory Config reflects
        # the freshly-imported values (the load_config call earlier in the
        # process saw an empty table). Cheap; only touches users that already
        # have rows.
        from .config import _apply_user_profiles  # noqa: PLC0415

        _apply_user_profiles(config)
    except Exception as e:  # noqa: BLE001
        logger.warning("user_profiles migration skipped: %s", e)

    # Phase 7b: migrate per-user TOML briefings into the briefing_configs
    # table on first run. Idempotent — only writes rows whose
    # (user_id, name) pair doesn't already exist. Re-applies the overlay
    # so the in-memory config reflects DB-managed briefings.
    try:
        from . import user_briefings as _ub  # noqa: PLC0415

        _ub.import_from_user_configs(config.db_path, config.users)
        from .config import _apply_user_briefings  # noqa: PLC0415

        _apply_user_briefings(config)
    except Exception as e:  # noqa: BLE001
        logger.warning("user_briefings migration skipped: %s", e)

    # Phase 1.3: purge orphan skill scheduled_jobs / pending skill tasks
    # whose skill name no longer exists in the index (e.g. operator
    # renamed `feeds` → `feed_reader`). Runs once per startup; the
    # seeders re-populate fresh rows on the next sync tick.
    try:
        from .skills._loader import load_skill_index as _lsi  # noqa: PLC0415

        _idx = _lsi(config.skills_dir, config.bundled_skills_dir)
        with db.get_db(config.db_path) as conn:
            _purge_obsolete_skill_jobs(conn, _idx)
    except Exception as e:  # noqa: BLE001
        logger.warning("Skill-job purge skipped: %s", e)

    # Start Talk polling in background thread so it runs independently of task processing
    if config.talk.enabled:
        talk_thread = threading.Thread(
            target=_talk_poll_loop, args=(config,), daemon=True, name="talk-poller",
        )
        talk_thread.start()
        logger.info("STARTUP Started Talk polling thread")

    # Create worker pool for per-user concurrent task processing
    pool = WorkerPool(config)

    # Initialize status writer
    from .status_writer import init_status_writer, write_status
    init_status_writer()

    last_email_poll = 0.0
    last_briefing_check = 0.0
    last_tasks_file_poll = 0.0
    last_shared_file_check = 0.0
    last_scheduled_job_check = 0.0
    last_cleanup_check = 0.0
    last_sleep_cycle_check = 0.0
    last_channel_sleep_cycle_check = 0.0
    last_heartbeat_check = 0.0
    last_db_health_check = 0.0
    last_status_write = 0.0
    last_trigger_check = 0.0

    while not _shutdown_requested:
        # Dispatch worker threads first — minimizes latency for pending tasks
        try:
            pool.dispatch()
        except Exception as e:
            logger.error("Error dispatching workers: %s", e)

        now = time.time()

        # Check briefings periodically (manages own DB connections to avoid
        # holding locks during slow network pre-fetching)
        if now - last_briefing_check >= config.scheduler.briefing_check_interval:
            try:
                briefing_tasks = check_briefings(config.db_path, config)
                if briefing_tasks:
                    logger.info("Queued %d briefing(s)", len(briefing_tasks))
            except Exception as e:
                logger.error("Error checking briefings: %s", e)
            last_briefing_check = now

        # Check briefing triggers from NC app (every 30s)
        if now - last_trigger_check >= config.scheduler.tasks_file_poll_interval:
            try:
                triggered = check_briefing_triggers(config.db_path, config)
                if triggered:
                    logger.info("Processed %d briefing trigger(s)", len(triggered))
            except Exception as e:
                logger.error("Error checking briefing triggers: %s", e)
            last_trigger_check = now

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
                from .memory.sleep_cycle import check_sleep_cycles
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
                from .memory.sleep_cycle import check_channel_sleep_cycles
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

        # Write status file periodically (every 60s)
        if now - last_status_write >= 60:
            try:
                with db.get_db(config.db_path) as conn:
                    fg_pending = sum(
                        db.count_pending_tasks_for_user_queue(conn, uid, "foreground")
                        for uid in db.get_users_with_pending_fg_queue_tasks(conn)
                    )
                    bg_pending = sum(
                        db.count_pending_tasks_for_user_queue(conn, uid, "background")
                        for uid in db.get_users_with_pending_bg_queue_tasks(conn)
                    )
                write_status(config, pool.active_count, fg_pending, bg_pending)
            except Exception as e:
                logger.error("Error writing status: %s", e)
            last_status_write = now

        # Sweep SQLite DBs (framework + per-user modules) for index corruption
        # once per ``db_health_check_interval`` (default 24h). Self-heals with
        # REINDEX; unrepairable damage is logged at ERROR. Runs immediately on
        # the first tick of a fresh daemon so we don't wait 24h to surface
        # latent corruption after a deploy.
        if now - last_db_health_check >= config.scheduler.db_health_check_interval:
            try:
                check_db_health(config)
            except Exception as e:  # noqa: BLE001
                logger.error("Error running DB health checks: %s", e)
            last_db_health_check = now

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
