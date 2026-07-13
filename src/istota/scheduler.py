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
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from croniter import croniter

logger = logging.getLogger("istota.scheduler")
# Dedicated logger so operators can isolate the periodic health line from the
# noisy general scheduler logger (`journalctl … | grep scheduler_stats`).
_SCHEDULER_STATS_LOGGER = logging.getLogger("istota.scheduler.stats")
# Warn at most once when psutil is unavailable rather than on every emit.
_psutil_unavailable_warned = False

# Source types the system generates on its own (not user-submitted). Used to
# suppress the "A task you submitted was cancelled" notice when these age out —
# notifying their output channel turns one wedged worker into a notification
# flood (a `* * * * *` cron aging out 130+ backed-up runs).
_AUTOMATED_SOURCE_TYPES = frozenset({"scheduled", "briefing", "heartbeat", "subtask"})

from . import db
from .brain import make_brain
from .consumers import (
    LogChannelSubscriber,
    PushNotificationSubscriber,
    TalkEventSubscriber,
)
from .db_health import CheckReport, check_and_repair
from .events import EventWriter, PROGRESS_MESSAGES
from .skills.briefing import (
    get_briefings_for_user,
    parse_briefing_json,
    strip_briefing_preamble,
)
from .config import Config, load_config
from .executor import (
    detect_malformed_result,
    discover_calendars_for_task,
    execute_task,
    is_transient_api_error,
    parse_api_error,
)
from .async_runtime import reset_async_runtime, run_coro
from .nextcloud_api import hydrate_user_configs
from .notifications import effective_log_destinations, send_notification
from .transport import (
    Destination,
    make_registry,
    parse_output_target,
    plan_has_surface,
    resolve_delivery_plan,
)
from .transport.registry import _surface_for_source_type
from .storage import ensure_user_directories_v2

# Deferred-op handlers were extracted to a sibling module; re-export the
# names so existing tests and call-sites that import them from this module
# keep working. Callers that touch this module directly (process_one_task,
# the retry path) reference these names unqualified, so the re-export is
# load-bearing.
from .scheduler_deferred import (  # noqa: F401  -- re-exported for back-compat
    _KNOWN_DEFERRED_SUFFIXES,
    _load_deferred_json,
    _process_deferred_garmin_import,
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


def recover_orphaned_tasks_on_startup(config: Config) -> None:
    """Reclaim tasks abandoned mid-execution by a dead prior daemon instance.

    Runs once at startup under the flock, before any worker spawns, so every
    ``running``/``locked`` row is an orphan (see ``db.recover_orphaned_tasks``).
    This recovers a scheduler restart in seconds instead of waiting out
    ``worker_stuck_minutes``; the time-based reclaim in ``run_cleanup_checks``
    then only has to cover the rarer worker-died-but-daemon-survived case.

    For orphans that won't re-run (cancelled / failed) we emit a terminal event
    frame so a watching web client gets immediate closure instead of a hung
    spinner. The ``EventWriter`` resumes ``seq`` above any partial events the
    dead attempt streamed, and runs with no subscribers — the SSE/snapshot
    clients pick the frame up by polling the ``task_events`` table. Released
    orphans emit nothing: their re-run streams a fresh ``task_started`` and the
    client resumes from its cursor (the documented retry-continuity path).
    """
    with db.get_db(config.db_path) as conn:
        recovered = db.recover_orphaned_tasks(
            conn, config.scheduler.max_retry_age_minutes,
        )
    if not recovered:
        return

    counts = {"released": 0, "cancelled": 0, "failed": 0}
    for info in recovered:
        action = info["action"]
        counts[action] = counts.get(action, 0) + 1
        if action == "released":
            continue  # re-run emits its own task_started; nothing to emit here

        writer = EventWriter(
            info["id"], str(config.db_path),
            enabled=config.scheduler.event_log_enabled,
        )
        if action == "cancelled":
            writer.emit("cancelled")
            writer.emit("done", {"stop_reason": "cancelled", "duration_seconds": 0})
        else:  # failed
            writer.emit("error", {
                "message": "Interrupted by a scheduler restart and not retried.",
                "stop_reason": "error",
            })
            writer.emit("done", {"stop_reason": "error", "duration_seconds": 0})
        writer.finish()

    logger.warning(
        "STARTUP Recovered %d orphaned task(s) from a prior instance "
        "(released=%d, cancelled=%d, failed=%d)",
        len(recovered), counts["released"], counts["cancelled"], counts["failed"],
    )


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
        send_notification(config, task.user_id, message, purpose="alert")
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

    Delegates to `db.scheduled_assistant_body` (single source of truth, shared
    with the transcript backfill) — None there means "don't post".
    """
    body = db.scheduled_assistant_body(True, result)
    if body is None:
        return False, result
    return True, body


def _store_scheduled_room_turn(conn, task, body: str) -> None:
    """Mirror a scheduled (cron) job's room post into the canonical messages
    store so the room's web view renders it (ISSUE-133).

    Interactive talk/web turns are stored by the task-success branch directly,
    but a cron post is ``source_type="scheduled"`` and was excluded — so a bot
    notice to a Talk room (a location alert, the daily money sync) posted to
    Talk and never reached the web mirror of that room. The web transcript
    reader already renders ``origin_surface='scheduled'`` assistant rows (the
    synthetic cron prompt stays hidden — there is no user-authored turn), so
    storing the posted body here is the whole producer half of the fix.

    Self-gating: only fires for ``scheduled`` tasks with a conversation token
    whose room exists in the registry. A token with no room row isn't a
    web-visible room, so there is nothing to mirror into; briefing / talk / web
    source types are handled elsewhere and no-op here. Idempotent across retries
    (``store_turn_message`` dedups on ``(room, role, task_id)``)."""
    if task.source_type != "scheduled" or not task.conversation_token:
        return
    if db.get_room(conn, task.conversation_token) is None:
        return
    db.store_turn_message(
        conn, task.conversation_token, role="assistant",
        body=body, task_id=task.id, origin_surface="scheduled",
    )


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
    """Edit a Talk message in-place. Returns True on success, False on failure.

    Thin shim over ``TalkTransport.edit`` — the surface logic (and the
    ``TalkClient`` construction) lives in ``transport/talk/``."""
    if not config.nextcloud.url or not task.conversation_token:
        return False
    from .transport.talk import TalkTransport
    try:
        await TalkTransport(config).edit(task.conversation_token, message_id, message)
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
    """Resolve a Talk conversation token to its display name.

    Thin process-cache wrapper over ``TalkTransport.resolve_channel_name`` — the
    OCS read itself lives behind the transport seam now. Only meaningful for
    Talk-origin tasks; callers gate on the origin surface before invoking.
    Results are cached for the lifetime of the process.
    """
    if conversation_token in _channel_name_cache:
        return _channel_name_cache[conversation_token]
    from .transport.talk import TalkTransport
    name = await TalkTransport(config).resolve_channel_name(conversation_token)
    _channel_name_cache[conversation_token] = name
    return name


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
    config: Config, task: db.Task, log_dests: list[Destination], prefix: str,
    log_callback, success: bool, error: str | None = None,
    skills: list[str] | None = None,
    model: str | None = None, effort: str | None = None,
):
    """Post/edit the final summary to every resolved log destination.

    Edit-capable surfaces that streamed during the run get their in-flight
    message edited to the final state; edit-capable surfaces with no prior
    message (no tool calls) and all non-edit surfaces get a single fresh
    delivery of the footer. Each destination is delivered through its registered
    transport; one failing destination never aborts the others or the task.
    """
    descriptions = getattr(log_callback, "all_descriptions", []) if log_callback else []
    delivery_state = getattr(log_callback, "delivery_state", {}) if log_callback else {}

    body = _format_log_channel_body(
        prefix, descriptions, done=True, success=success, error=error,
        skills=skills, model=model, effort=effort,
    )

    registry = make_registry(config)
    for dest in log_dests:
        transport = registry.get(dest.surface)
        if transport is None:
            continue
        msg_id = delivery_state.get((dest.surface, dest.channel))
        try:
            if transport.capabilities.supports_edit and msg_id is not None:
                run_coro(transport.edit(dest.channel, msg_id, body))
            else:
                run_coro(transport.deliver(
                    dest.channel, body, task=task,
                    reference_id=f"istota:log:{task.id}",
                ))
        except Exception as e:
            logger.debug(
                "Log channel finalize failed for task %d dest %s: %s",
                task.id, dest.surface, e,
            )


def _count_pending(config: Config, user_id: str, queue_type: str) -> int:
    """Cheap claimable-task read for the idle pre-check (mirrors dispatch()).

    Uses the claimability-aware count so a follow-up gated behind an active task
    in the same room reads as 0 — the idle worker keeps sleeping cheaply instead
    of busy-polling claim_task until the gate clears.

    Reads with a short busy_timeout so a locked DB reads as "no work" (the worker
    keeps idling) rather than blocking; the next idle poll retries.
    """
    timeout_ms = config.scheduler.main_loop_read_timeout_ms or None
    try:
        with db.get_db(config.db_path, busy_timeout_ms=timeout_ms) as conn:
            return db.count_claimable_tasks_for_user_queue(conn, user_id, queue_type)
    except sqlite3.OperationalError as exc:
        logger.warning("idle_precheck_db_locked user=%s queue=%s err=%s", user_id, queue_type, exc)
        return 0


def _worker_idle_wait(
    user_id: str,
    queue_type: str,
    config: Config,
    stop_event: threading.Event,
    should_stop: Callable[[], bool],
    run_one: Callable[[], "tuple[int, bool] | None"],
    pending_count: Callable[[], int],
) -> "tuple[int, bool] | None":
    """Park an idle worker, re-checking for work on a fine cadence.

    Polls for a pending task every ``worker_idle_poll_interval`` until
    ``worker_idle_timeout`` of *continuous* emptiness elapses, then returns
    ``None`` so the caller exits the worker. Returns the first non-None
    ``run_one()`` result as soon as a task is claimed, so the caller can loop
    back into its fast path with a fresh deadline.

    The fine-cadence path mirrors ``_dispatch_sleep``: it sleeps in
    ``time.sleep`` slices (so the fake-clock tests can drive it) and checks both
    ``stop_event`` (per-worker stop) and ``should_stop`` (global shutdown)
    before and after every slice, bounding stop/shutdown latency to one
    ``worker_idle_poll_interval``. The legacy branch instead uses a single
    interruptible ``stop_event.wait`` (instant wake on stop), exactly matching
    pre-phase-2 behaviour; its stop latency is bounded by that one coarse wait.

    The deadline tracks *continuous* emptiness: it is set once on entry and is
    only reset by the caller re-entering after a genuine task. Losing a claim
    race (``run_one`` returns ``None`` after a positive ``pending_count``) does
    not reset it, so two idle workers ping-ponging empty queues cannot keep each
    other alive forever.
    """
    idle_poll = config.scheduler.worker_idle_poll_interval
    idle_timeout = config.scheduler.worker_idle_timeout
    poll_interval = config.scheduler.poll_interval

    # Legacy parity: a single coarse, interruptible wait + single recheck —
    # exactly the pre-phase-2 behaviour, including instant wake on stop (the
    # wait keys on stop_event, which pool.shutdown sets via request_stop). Opt
    # back in with worker_idle_poll_interval <= 0 or >= worker_idle_timeout.
    if idle_poll <= 0 or idle_poll >= idle_timeout:
        if stop_event.wait(timeout=min(poll_interval, idle_timeout)):
            return None  # per-worker stop / global shutdown
        if should_stop():
            return None
        return run_one()

    deadline = time.monotonic() + idle_timeout
    while not should_stop() and not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(idle_poll, remaining))
        if should_stop() or stop_event.is_set():
            return None
        # Cheap pre-check before the expensive claim. pending_count is a
        # claimability-aware indexed read (the same count dispatch() uses);
        # process_one_task -> claim_task additionally executes stale-lock /
        # stuck-task maintenance UPDATEs on every call, so only pay that when
        # there is plausibly something we can actually claim. Because the count
        # mirrors claim_task's per-channel gate, a follow-up parked behind an
        # active task in this room reads as 0 here — we keep sleeping cheaply
        # instead of busy-polling claim_task until the gate clears.
        try:
            if pending_count() == 0:
                continue
        except Exception:  # noqa: BLE001
            # A transient SQLite/FUSE read failure must not kill the worker
            # mid-idle; fall through to run_one (which has its own error
            # handling) rather than skip a possibly-present task.
            pass
        result = run_one()
        if result is not None:
            return result
        # Lost the race to dispatch()/another worker, or nothing after all —
        # keep polling against the SAME deadline (the queue is still empty).
    return None


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

                # No tasks available — linger, re-checking on a fine cadence
                # until a new task arrives (claimed within ~one idle poll) or the
                # cumulative idle timeout elapses. dispatch() may scan the same
                # user concurrently while we linger; that overlap is harmless —
                # claim_task is atomic (UPDATE ... RETURNING), so at most one of
                # us wins and the loser simply gets None.
                idle_result = _worker_idle_wait(
                    self.user_id, self.queue_type, self.config,
                    self._stop_event, lambda: _shutdown_requested,
                    run_one=lambda: process_one_task(
                        self.config, user_id=self.user_id, queue=self.queue_type,
                    ),
                    pending_count=lambda: _count_pending(
                        self.config, self.user_id, self.queue_type,
                    ),
                )
                if idle_result is not None:
                    task_id, success = idle_result
                    status = "completed" if success else "failed"
                    logger.info(
                        "Worker %s/%s: task %d %s",
                        self.user_id, self.queue_type, task_id, status,
                    )
                    continue

                # Idle timeout reached — exit; dispatch() re-spawns a worker on
                # the next pending task for this user (phase-1 sub-tick cadence).
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
        # Short busy_timeout: this scan is pure reads, so a DB locked past the
        # budget means "skip this dispatch tick" (dispatch runs again in ~0.5s)
        # rather than blocking the main loop for 30s and tripping the watchdog.
        timeout_ms = self.config.scheduler.main_loop_read_timeout_ms or None
        try:
            with db.get_db(self.config.db_path, busy_timeout_ms=timeout_ms) as conn:
                fg_users = db.get_users_with_pending_fg_queue_tasks(conn)
                bg_users = db.get_users_with_pending_bg_queue_tasks(conn)
                # Pre-fetch *claimable* task counts for users that may need multiple
                # workers. Claimable (not raw pending) so a follow-up gated behind an
                # active task in the same room counts as 0 — dispatch won't spawn a
                # doomed extra worker that can only busy-poll claim_task until the
                # gate clears. A task in a different, ungated room still counts, so
                # legitimate parallelism is unaffected.
                fg_pending = {uid: db.count_claimable_tasks_for_user_queue(conn, uid, "foreground") for uid in fg_users}
                bg_pending = {uid: db.count_claimable_tasks_for_user_queue(conn, uid, "background") for uid in bg_users}
        except sqlite3.OperationalError as exc:
            logger.warning("dispatch_scan_db_locked err=%s (skipping tick)", exc)
            return

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
    from .email_support import is_synthetic_email_thread_token
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
    from .transport import parse_output_target
    if plan_has_surface(parse_output_target(task.output_target), "talk"):
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
    return send_notification(config, task.user_id, message, purpose="notification")


def _deliver_deferred_email_output(
    config: Config, task: db.Task, user_temp_dir: Path,
) -> None:
    """Deliver or clean up deferred email output files not handled by the normal path.

    The normal email delivery path (post_result_to_email via `post_email` flag)
    handles tasks whose output_target includes an "email" leg. This
    function handles two gap cases:

    1. source_type="email" but output_target doesn't include email (e.g. an
       emissary reply routed to Talk) — deliver via post_result_to_email,
       which will find the processed_email record and reply correctly.
    2. Non-email source (e.g. Talk user who asked the agent to email someone)
       where the agent used `email output` instead of `email send` — warn and
       delete, because there's no processed_email record and the scheduler
       would send to the wrong recipient.
    """
    from .transport import parse_output_target
    if plan_has_surface(parse_output_target(task.output_target), "email"):
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
            "Delivering deferred email output for task %d (source=%s, output_target=%s)",
            task.id, task.source_type, task.output_target,
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


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL a subprocess and every descendant in its process group."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # Group already gone, or no getpgid (e.g. not a session leader) —
        # fall back to killing the direct child.
        try:
            proc.kill()
        except OSError:
            pass


def _run_capture(
    cmd, *, timeout: float, cwd: str, env: dict, shell: bool = False,
) -> subprocess.CompletedProcess:
    """Run a subprocess capturing stdout/stderr, killing the whole process
    *group* on timeout.

    ``subprocess.run(timeout=…)`` only SIGKILLs the direct child, then calls
    ``communicate()`` again to reap it — which blocks indefinitely when an
    orphaned grandchild inherited the stdout/stderr pipe. A CRON ``command:``
    that backgrounds a child (or a skill CLI that shells out) therefore wedges
    its worker past the timeout, and because the per-task heartbeat thread keeps
    pinging, the stuck-running reaper never reclaims it — the location-alert task
    held its only background slot for 6+ hours that way. ``start_new_session``
    puts the child in its own process group so the timeout can ``os.killpg`` the
    whole tree, releasing the pipe. Re-raises ``TimeoutExpired`` so callers
    handle the deadline exactly as before; otherwise returns a CompletedProcess
    so call sites keep using ``.returncode`` / ``.stdout`` / ``.stderr``.
    """
    proc = subprocess.Popen(
        cmd, shell=shell, cwd=cwd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        # The group is dead now, so this drains the pipes and reaps without
        # blocking; bound it anyway so a pathological case can't hang the worker.
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


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
        proc = _run_capture(
            cmd,
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
        proc = _run_capture(
            task.command,
            timeout=timeout,
            cwd=str(config.temp_dir),
            env=env,
            shell=True,
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


def _drain_deferred_ops(config: Config, task: db.Task, result: str) -> None:
    """Replay a completed task's deferred-op files (memory / kv / KG / health /
    subtasks / tracking / sent-emails / email-output) and warn on unconsumed
    files. The single source of truth for the post-success drain — shared by
    ``process_one_task`` and ``run_task_inline`` so the two can't drift.
    """
    from .executor import get_user_temp_dir
    user_temp_dir = get_user_temp_dir(config, task.user_id)
    _process_deferred_subtasks(config, task, user_temp_dir)
    _process_deferred_tracking(config, task, user_temp_dir)
    _process_deferred_sent_emails(config, task, user_temp_dir)
    _process_deferred_kv_ops(config, task, user_temp_dir)
    _process_deferred_kg_ops(config, task, user_temp_dir)
    _process_deferred_health_ops(config, task, user_temp_dir)
    _process_deferred_garmin_import(config, task, user_temp_dir)
    _process_deferred_user_alerts(config, task, user_temp_dir)
    _deliver_deferred_email_output(config, task, user_temp_dir)
    _warn_unconsumed_deferred_files(task, user_temp_dir)
    _notify_confirmed_email_result(config, task, result)


def run_task_inline(
    config: Config,
    task: db.Task,
    *,
    event_writer: "EventWriter | None" = None,
    workspace_dir: "Path | None" = None,
) -> tuple[bool, str]:
    """Execute a task to completion in-process and finalize it, with no claim,
    ack, transport push, or retry.

    Runs ``execute_task`` (streaming the brain's events through ``event_writer``
    so a subscriber can render them), emits the terminal ``result`` / ``error``
    / ``cancelled`` + ``done`` events, updates the task status, and drains the
    deferred-op files. This is the "run a task to completion locally" core the
    REPL (and a future ``istota task -x``) reuses — so the deferred-op drain and
    terminal-event emission can't drift from the daemon path.

    Returns ``(success, result_text)``.
    """
    with db.get_db(config.db_path) as conn:
        user_resources = db.get_user_resources(conn, task.user_id)

    success, result, actions_taken, execution_trace = execute_task(
        task, config, user_resources,
        event_writer=event_writer, workspace_dir=workspace_dir,
    )

    # Same success-guards the daemon applies: API errors masquerading as
    # success, and malformed (leaked tool-call XML) output.
    if success and parse_api_error(result):
        success = False
    if success:
        malformed = detect_malformed_result(result, output_target=task.output_target)
        if malformed:
            success = False
            result = f"Malformed output: {malformed}"

    is_cancelled = (not success) and result == "Cancelled by user"

    if event_writer is not None:
        if is_cancelled:
            event_writer.emit("cancelled")
        elif success:
            event_writer.emit("result", {
                "text": result[:8000], "truncated": len(result) > 8000,
            })
        else:
            event_writer.emit("error", {"message": result[:500], "stop_reason": "error"})
        event_writer.emit("done", {
            "stop_reason": "completed" if success else "error",
            "duration_seconds": round(event_writer.elapsed_seconds(), 1),
            **({"model": task.model_used} if task.model_used else {}),
        })
        event_writer.finish()
        # Prune ephemeral text_delta rows for stream surfaces (repl, web) once
        # the terminal event has fired — the in-process subscriber already
        # rendered them live, so retaining the rows only bloats the log.
        from .transport.registry import task_is_stream_surface
        if task_is_stream_surface(config, task):
            with db.get_db(config.db_path) as _prune_conn:
                db.delete_task_events_by_kind(_prune_conn, task.id, "text_delta")
                db.delete_task_events_by_kind(_prune_conn, task.id, "thinking")

    status = "completed" if success else ("cancelled" if is_cancelled else "failed")
    with db.get_db(config.db_path) as conn:
        db.update_task_status(
            conn, task.id, status,
            result=result if success else None,
            error=None if success else result,
            actions_taken=actions_taken, execution_trace=execution_trace,
        )

    if success:
        _drain_deferred_ops(config, task, result)

    return success, result


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

    # Log channel setup — resolve before execution starts. The verbose log is
    # opt-in and routes to any user-routable transport destination (talk default
    # / email / ntfy / comma list); effective_log_destinations returns [] when
    # the user configured neither a log route nor a legacy log_channel.
    log_dests: list[Destination] = []
    if not task.skip_log_channel:
        log_dests = effective_log_destinations(config, task.user_id)
    log_channel_prefix = ""
    log_callback = None
    if log_dests and not dry_run:
        # Resolve the *source* channel name for the log prefix — only when the
        # task originated on Talk (an OCS display-name lookup is meaningless for
        # email / repl origins, which fall back to source_type).
        channel_name = None
        if task.conversation_token and _surface_for_source_type(task.source_type) == "talk":
            try:
                channel_name = run_coro(
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

            # Send ack message + wire the progress subscriber for surfaces that
            # support a progress ack. The capability check is the transport seam;
            # the source_type == "talk" guard preserves today's behaviour (only
            # interactive Talk tasks get an editable ack — briefings, scheduled
            # jobs, and subtasks that also resolve to the Talk surface do not).
            is_rerun = task.attempt_count > 0 or task.confirmation_prompt is not None
            _delivery_transport = make_registry(config).for_task(task)
            _supports_ack = bool(
                _delivery_transport
                and _delivery_transport.capabilities.supports_progress_ack
            )
            if (_supports_ack and task.source_type == "talk"
                    and task.conversation_token and not dry_run):
                ack_text = f"`#{task.id}` *Retrying…*" if is_rerun else f"`#{task.id}` *{random.choice(PROGRESS_MESSAGES)}*"
                ack_msg_id = run_coro(post_result_to_talk(
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

            # Log channel subscriber (no rate limiting, streams every tool call
            # to edit-capable destinations).
            if log_dests and log_channel_prefix:
                log_callback = LogChannelSubscriber(
                    config, task, log_dests, log_channel_prefix,
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

    # Duplicate-execution guard (ISSUE-112 follow-up). A slow-but-alive worker
    # whose heartbeat lapsed past the stuck threshold can have its task reclaimed
    # and re-run by a second worker while it's still executing — two answers for
    # one task id. We can't key on locked_by: get_worker_id() has no slot, so two
    # workers in the same process for the same user share an id. attempt_count is
    # the reliable token — the stuck-running release bumps it on every reclaim. If
    # the row's attempt_count has advanced past what we claimed, another worker
    # superseded us; abandon our result rather than deliver a duplicate or
    # double-apply deferred ops. The superseding worker owns delivery + the
    # terminal event frame (its EventWriter resumes seq), so we just bail.
    if not dry_run:
        with db.get_db(config.db_path) as conn:
            _current = db.get_task(conn, task_id)
        if _current is None or _current.attempt_count != task.attempt_count:
            logger.warning(
                "Task %d superseded mid-run (claimed attempt=%s, now=%s) — "
                "discarding this worker's result without delivering",
                task_id, task.attempt_count,
                _current.attempt_count if _current else "deleted",
            )
            return (task_id, False)

    # Resolve the delivery plan: the single source of truth for where this
    # task's result goes. Replaces the hardcoded output_target fan-out — the
    # plan parses task.output_target descriptors (talk/email/ntfy/istota_file/
    # stream, comma-separated, surface[:channel]), normalizes legacy both/all,
    # infers the source_type default when unset, and resolves Talk channels via
    # the synthetic-email-token fallback.
    registry = make_registry(config)
    plan = resolve_delivery_plan(config, task, registry)
    _talk_dest = next((d for d in plan if d.surface == "talk"), None)
    # talk_token: the plan's Talk channel when Talk is a destination (honours an
    # explicit talk:<token>; equals _talk_target_for_delivery for the inferred
    # case). Falls back to the unconditional resolution when Talk is NOT in the
    # plan, because the heartbeat_silent branch delivers to Talk regardless of
    # output_target (it bypasses the plan entirely, matching prior behaviour).
    talk_token = (
        _talk_dest.channel if _talk_dest else _talk_target_for_delivery(config, task)
    )
    plan_talk = _talk_dest is not None
    # A mirror Talk leg (room fan-out from a non-Talk origin, e.g. a web-origin
    # task mirrored to its bound Talk room) must not carry the confirmation
    # prompt — confirmations stay on the originating surface (open question 7).
    _talk_is_mirror = bool(_talk_dest and getattr(_talk_dest, "mirror", False))
    plan_email = plan_has_surface(plan, "email")
    plan_ntfy = plan_has_surface(plan, "ntfy")
    plan_file = plan_has_surface(plan, "istota_file")
    plan_web = plan_has_surface(plan, "web")
    # A *push* web destination is a foreign task (e.g. an email reply) routing
    # INTO a room — it must be delivered via WebTransport.deliver. An own-origin
    # web task resolves its web leg to a stream no-op (its result event already
    # covers the room over SSE), so it is absent here and never double-posts.
    web_push_dests = [
        d for d in plan if d.surface == "web" and d.kind == "push"
    ]

    # Track if we need to call istota_file handler after db connection closes.
    # The transport derives success from the task's terminal status at delivery.
    call_file_handler = False
    post_ntfy = False
    post_web = False

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

    # Guard: detect malformed model output (leaked tool-call XML syntax).
    # Strict mode applies when the result will render in Talk, i.e. Talk is a
    # resolved destination (the inferred default included). Passing a "talk"
    # descriptor when Talk is in the plan keeps the prior strict/lenient split.
    if success:
        _malformed_target = "talk" if plan_talk else None
        malformed_reason = detect_malformed_result(result, output_target=_malformed_target)
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

    # A confirmation prompt is answerable on an interactive surface: a Talk
    # reply (Talk is a resolved destination, and not the "all" broadcast
    # fan-out, which pushes ntfy), or the web /chat confirm endpoint — but only
    # for a task whose OWN origin is web (source_type="web"), whose confirmation
    # rides its task_events SSE stream and is answered by POST
    # /chat/tasks/{id}/confirm. A *foreign* task merely pushed into a web room
    # (e.g. an email reply routed there) has no such SSE the room tails and no
    # answerable affordance, so it must not gate — it completes and the question
    # text is delivered to the room as a normal result (the user answers by
    # replying). Computed once and reused by the status, event-emission, and
    # deferred-op-skip branches below.
    _own_origin_web = plan_web and task.source_type == "web"
    _confirmable_surface = (plan_talk and talk_token and not plan_ntfy) or _own_origin_web
    is_confirmation_request = bool(
        success
        and _confirmable_surface
        and CONFIRMATION_PATTERN.search(result)
    )

    with db.get_db(config.db_path) as conn:
        if success:
            if is_confirmation_request:
                # Set task to pending confirmation instead of completing
                db.set_task_confirmation(conn, task_id, result)
                db.log_task(conn, task_id, "info", "Task awaiting user confirmation")
                # Talk confirmations post the prompt to the room; web/stream
                # confirmations surface it via the `confirmation` task event
                # (emitted below) and must not cross-post to Talk. A mirror Talk
                # leg is excluded — a web-origin confirmation stays on web.
                if plan_talk and talk_token and not _talk_is_mirror:
                    post_talk_message = result
            else:
                db.update_task_status(conn, task_id, "completed", result=result, actions_taken=actions_taken, execution_trace=execution_trace)
                db.log_task(conn, task_id, "info", "Task completed successfully")

                # Persist the assistant turn into the canonical messages store
                # for room-surface tasks (Talk/web), so the unified history
                # reader stays caught up and the web transcript renders this
                # turn from messages. Idempotent; the trace stays in `tasks`.
                if (
                    task.source_type in ("talk", "web")
                    and task.conversation_token
                    and db.get_room(conn, task.conversation_token) is not None
                ):
                    db.store_turn_message(
                        conn, task.conversation_token, role="assistant",
                        body=result, task_id=task_id,
                        origin_surface=task.source_type,
                    )

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
                            _store_scheduled_room_turn(conn, task, result_to_post)
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
                    if plan_talk and talk_token:
                        post_talk_message = delivery_result
                        _store_scheduled_room_turn(conn, task, delivery_result)
                    if plan_email:
                        post_email = True
                    if plan_ntfy:
                        post_ntfy = True
                    if web_push_dests:
                        post_web = True
                    if plan_file:
                        call_file_handler = True

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
                # The event log is intentionally NOT wiped here: keeping it lets
                # a watching web client survive the retry (its resume cursor
                # stays valid) and see a "retrying" notice. The next attempt's
                # EventWriter resumes seq via get_max_task_event_seq, so there's
                # no UNIQUE(task_id, seq) collision. The retry notice itself is
                # emitted from the terminal-events block below (outside this DB
                # transaction, so the writer's own connection can't contend).
            else:
                db.update_task_status(conn, task_id, "failed", error=result)
                db.log_task(conn, task_id, "error", f"Task failed permanently: {result[:500]}")

                if task.source_type in ("briefing", "scheduled"):
                    # Suppress user-facing error delivery for automated tasks.
                    # Errors are logged to DB and log_channel; no need to confuse users.
                    db.log_task(conn, task_id, "info", "Suppressed error delivery for automated task")
                elif plan_talk and talk_token:
                    # Use user-friendly error message, not raw error
                    friendly_error = _format_error_for_user(result)
                    post_talk_message = f"🐙 {friendly_error}"
                    is_failure_notify = True
                # NOTE: We intentionally do NOT email errors to users.
                # Failed tasks routed to email/ntfy only log the error.
                # Receiving error emails is confusing; users can check Talk or retry.
                if plan_file:
                    call_file_handler = True

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
    # On a retry-eligible failure the task isn't done — emit a "retrying" notice
    # (not a terminal frame) instead, so a watching web client sees why it's
    # still working rather than a silent spinner. The log is no longer wiped, so
    # this notice and the next attempt's events (seq resumed) reach the client.
    if event_writer is not None:
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
        if will_retry:
            # Mirror the backoff the retry branch set (1, 4, 16 min). Reuses the
            # progress_text kind — the frontend already renders it as the live
            # progress line; it shows during the backoff gap, then the next
            # attempt's task_started replaces it with the fresh ack verb.
            delay = 1 << (task.attempt_count * 2)
            event_writer.emit("progress_text", {
                "text": f"⏳ Attempt failed — retrying in {delay} min…",
            })
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
                **({"model": task.model_used} if task.model_used else {}),
            })
            event_writer.finish()
            # Prune the ephemeral text_delta rows now the canonical result/
            # confirmation/error has landed (web chat streaming). The deltas were
            # a cosmetic live preview; steady state retains zero. Web is the only
            # stream surface that flows through process_one_task (repl runs
            # inline), so plan_web is the gate — push tasks never wrote any.
            if plan_web:
                with db.get_db(config.db_path) as _prune_conn:
                    db.delete_task_events_by_kind(_prune_conn, task_id, "text_delta")
                    db.delete_task_events_by_kind(_prune_conn, task_id, "thinking")

    # Process deferred operations (subtasks, transaction tracking) on success,
    # unless the task is awaiting confirmation (drain after the user confirms).
    if success and not is_confirmation_request:
        _drain_deferred_ops(config, task, result)

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
    if log_dests and log_channel_prefix:
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
            config, task, log_dests, log_channel_prefix,
            log_callback, success, error=error_msg,
            skills=selected_skills,
            model=resolved_model, effort=resolved_effort,
        )

    # Deliver results outside DB context to avoid lock conflicts. The final
    # result is always a separate Talk message (the ack carries progress); the
    # Talk subscriber never posts the result, so no dedup is needed.
    response_msg_id = None
    if post_talk_message:
        # For a web-origin mirror leg, repost the user's question (attributed)
        # before the reply so the Talk transcript isn't an orphaned answer. Pure
        # Talk-surface post — never persisted to the canonical messages store.
        if _talk_is_mirror and task.source_type == "web" and task.prompt:
            run_coro(post_result_to_talk(
                config, task, _format_mirror_user_repost(config, task),
                reference_id=f"istota:task:{task.id}:prompt",
                target_token=talk_token,
            ))
        response_msg_id = run_coro(post_result_to_talk(
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

    # Record the mirror's Talk post id in the assistant message's external_ids
    # ledger (loop-prevention infra — dormant for v1 since Talk self-filters bot
    # posts by author, but makes the no-echo invariant explicit for a future
    # surface that doesn't).
    if response_msg_id and _talk_is_mirror and task.conversation_token:
        try:
            with db.get_db(config.db_path) as conn:
                row = conn.execute(
                    "SELECT id FROM messages WHERE room_token = ? AND task_id = ? "
                    "AND role = 'assistant' LIMIT 1",
                    (task.conversation_token, task_id),
                ).fetchone()
                if row:
                    db.set_message_external_id(
                        conn, row["id"], "talk", str(response_msg_id),
                    )
        except Exception as e:
            logger.debug("Failed to record mirror external_id for task %d: %s", task_id, e)

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
        from .transport._types import DeliveryOptions
        if task.source_type == "briefing":
            pb = parse_briefing_json(result)
            ntfy_result = pb["body"] if pb else strip_briefing_preamble(result)
        else:
            ntfy_result = result
        ntfy_transport = registry.get("ntfy")
        if ntfy_transport is not None:
            run_coro(ntfy_transport.deliver(
                "", ntfy_result, task=task,
                options=DeliveryOptions(title=f"Task {task_id}"),
            ))
    if call_file_handler:
        # The transport re-reads the task's terminal status to derive success.
        file_transport = registry.get("istota_file")
        if file_transport is not None:
            run_coro(file_transport.deliver("", result, task=task))
    if post_web:
        # A foreign task (e.g. an email reply) routed into a web room: push the
        # result as an unsolicited room message via WebTransport.deliver. The
        # own-origin web case never reaches here (its leg resolved to stream).
        web_transport = registry.get("web")
        if web_transport is not None:
            if task.source_type == "briefing":
                pb = parse_briefing_json(result)
                web_result = pb["body"] if pb else strip_briefing_preamble(result)
            else:
                web_result = result
            for dest in web_push_dests:
                run_coro(web_transport.deliver(dest.channel, web_result, task=task))

    return task_id, success


def _format_mirror_user_repost(config: Config, task: db.Task) -> str:
    """Attributed repost of a web-origin user turn for a Talk mirror leg.

    The bot can't post as the user in Talk, so a web-origin turn mirrored into a
    bound Talk room would otherwise show an orphaned bot answer with no visible
    question. The bot reposts the question (attributed) as its own message, then
    its reply in the next post. This is a pure Talk-surface artifact — it is
    never written to the canonical `messages` store, so web history / context is
    unaffected.
    """
    uc = config.get_user(task.user_id)
    display = uc.display_name if uc and uc.display_name else task.user_id
    return f"💬 {display} (via web):\n{task.prompt}"


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

    Thin shim over ``TalkTransport.deliver`` — splitting, group-chat threading,
    and the ``TalkClient`` construction live in ``transport/talk/``.
    """
    from .transport.talk import TalkTransport
    token = target_token or task.conversation_token
    return await TalkTransport(config).deliver(
        token, message, task=task,
        threaded=use_reply_threading, reference_id=reference_id,
    )


async def post_result_to_email(config: Config, task: db.Task, message: str) -> bool:
    """Send a task result as an email reply / fresh email. Returns True on success.

    Thin shim over the email transport — structured-output parsing, thread-reply
    routing, and sent-email recording live in ``transport/email/outbound.py``
    (mirrors ``post_result_to_talk`` / ``TalkTransport.deliver``). Calls the
    bool-returning ``deliver_email_result`` directly rather than
    ``EmailTransport.deliver`` because the scheduler's callers check the success
    flag, which the ``Transport.deliver`` protocol (``int | None``) discards for
    a surface with no message-id concept."""
    from .transport.email import deliver_email_result
    return await deliver_email_result(config, task, message)


def _deferred_briefing_placeholder(briefing_name: str) -> str:
    """Stand-in prompt stored on a deferred briefing task (ISSUE-143).

    The real prompt is built in the executor at worker-pickup time. This
    placeholder is only what an inspector (`istota show <id>`) sees on the
    stored row, and the fallback the executor keeps if the briefing config
    can't be resolved or its prompt build fails.
    """
    return f"Generate the '{briefing_name}' briefing."


def check_briefings(db_path, app_config: Config) -> list[int]:
    """
    Check for briefings that should run and queue them as tasks.

    The slow network pre-fetch that builds the briefing prompt (news, yfinance,
    FinViz, IMAP) is NOT done here — it is deferred to the executor when a
    background worker picks the task up (ISSUE-143). This keeps the scheduler
    dispatch thread free: a slow or unreachable briefing upstream can no longer
    stall `pool.dispatch()` and starve task processing for every room. The task
    carries only the briefing identity (`briefing_name`); the worker resolves
    the live config and builds the prompt.

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
                # Skip a briefing with no deliverable route: its only
                # destination(s) are bare Talk (no inline channel) and no
                # conversation_token is configured. Grammar-aware over the full
                # output_target descriptor (talk / both / all / email,talk /
                # talk:<room> / …) — an email/ntfy leg or an inline talk:<room>
                # keeps it alive (the bare Talk leg then DMs at delivery).
                _dests = parse_output_target(briefing.output)
                _talk_needs_room = any(
                    d.surface == "talk" and not d.channel for d in _dests
                )
                _has_other_route = any(
                    d.surface != "talk" or d.channel for d in _dests
                )
                if (
                    _talk_needs_room
                    and not briefing.conversation_token
                    and not _has_other_route
                ):
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

    # Phase 2: Short DB write — create tasks and update last_run. The prompt
    # is built later, in the executor, off the dispatch thread (ISSUE-143).
    created_tasks = []
    with db.get_db(db_path) as conn:
        for user_id, _user_tz_str, briefing in due_briefings:
            task_id = db.create_task(
                conn,
                prompt=_deferred_briefing_placeholder(briefing.name),
                user_id=user_id,
                source_type="briefing",
                conversation_token=briefing.conversation_token,
                output_target=briefing.output,
                priority=8,
                queue="background",
                briefing_name=briefing.name,
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

            # Queue the briefing task; the prompt is built in the executor off
            # the dispatch thread (ISSUE-143), same as the cron path.
            with db.get_db(db_path) as conn:
                task_id = db.create_task(
                    conn,
                    prompt=_deferred_briefing_placeholder(briefing.name),
                    user_id=user_id,
                    source_type="briefing",
                    conversation_token=briefing.conversation_token,
                    output_target=briefing.output,
                    priority=8,
                    queue="background",
                    briefing_name=briefing.name,
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

    # 2. Per-user module DBs. These now live on LOCAL disk at
    #    config.module_db_path(user_id, module) (off the Nextcloud mount so WAL
    #    is safe). Probe each resolved path directly rather than calling the
    #    module resolvers: resolvers raise for disabled-module / missing-user /
    #    missing-mount, and we don't want any of those to skip a *file* that is
    #    actually on disk and might be corrupt.
    for user_id in config.users:
        for module in ("feeds", "health", "location", "money"):
            try:
                db_path = config.module_db_path(user_id, module)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "db_health_module_path_failed user=%s module=%s err=%s",
                    user_id, module, exc,
                )
                continue
            reports.append(
                check_and_repair(db_path, label=f"{module}:{user_id}")
            )

    return reports


def _operator_alert_user(config: Config) -> str | None:
    """Pick a user to receive operator-level scheduler alerts.

    Prefers the first admin user (sorted for determinism); falls back to the
    first configured user. ``None`` when no users are configured.
    """
    if config.admin_users:
        return sorted(config.admin_users)[0]
    if config.users:
        return sorted(config.users)[0]
    return None


def _send_operator_alert(config: Config, user_id: str, message: str, *, timeout: float = 30.0) -> None:
    """Send an operator alert without letting a hung Talk delivery stall the
    caller. `send_notification` ultimately runs on the persistent asyncio loop
    with no timeout, so on the single-threaded main loop a wedged Nextcloud would
    block dispatch indefinitely (ISSUE-143 class) — and a backup-destination
    outage is exactly when Talk is likely also degraded. Run the send on a
    short-lived daemon thread and only wait `timeout`; if it's still going we
    return and let it finish (or die) in the background."""
    def _do() -> None:
        try:
            send_notification(config, user_id, message, purpose="alert")
        except Exception as exc:  # noqa: BLE001
            logger.error("operator_alert_failed err=%s", exc)

    t = threading.Thread(target=_do, name="operator-alert", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        logger.error("operator_alert_timed_out after %ss — send still running in background", timeout)


def _alert_backup_problems(config: Config, results: list[dict]) -> None:
    """Fire one operator alert when a backup run reports any errored or suspect
    (row-count collapse) DB. Best-effort — never raises into the loop."""
    problems = [r for r in results if r.get("status") in ("error", "suspect")]
    if not problems:
        return
    user = _operator_alert_user(config)
    if not user:
        return
    lines = "\n".join(f"• {r['label']}: {r['status']}" for r in problems)
    message = (
        f"⚠️ DB backup problem — {len(problems)} database(s) failed or were "
        f"quarantined on the latest snapshot:\n{lines}\n"
        "A 'suspect' DB was empty/unreadable vs. the prior good snapshot and was "
        "kept aside as .suspect; the prior good copy is preserved. Check the live DB."
    )
    _send_operator_alert(config, user, message)


def _maybe_alert_backup_stale(
    config: Config, now: float, persisted: float, already_alerted: bool
) -> bool:
    """Alert once when the persisted backup clock is older than 2x the interval
    (backups have silently stopped). Re-arms on recovery. Returns the new
    already_alerted state. Gated on a prior successful run (`persisted > 0`) so a
    fresh deploy that simply hasn't backed up yet doesn't false-alarm."""
    if not (config.scheduler.db_backup_enabled and config.scheduler.db_backup_interval):
        return already_alerted
    stale_after = 2 * config.scheduler.db_backup_interval
    if persisted > 0 and now - persisted >= stale_after:
        if already_alerted:
            return True
        age_h = int((now - persisted) / 3600)
        interval_h = config.scheduler.db_backup_interval // 3600
        logger.error(
            "db_backup_stale last_run=%.0f age_s=%.0f — backups appear stopped",
            persisted, now - persisted,
        )
        user = _operator_alert_user(config)
        if user:
            _send_operator_alert(
                config, user,
                f"⚠️ DB backups appear to have stopped — no successful snapshot in "
                f"{age_h}h (interval is {interval_h}h). Check the scheduler and the "
                "backup destination.",
            )
        return True
    return False


class LoopWatchdog:
    """Defense-in-depth monitor for a stalled scheduler main loop (ISSUE-143).

    The dispatch loop is single-threaded: if `pool.dispatch()` or an
    unanticipated per-cycle check blocks (a slow network call that slipped onto
    the loop thread, a wedged DB sweep), task dispatch stops for every room with
    no other signal — the failure mode ISSUE-143 describes. This watchdog runs on
    its own daemon thread, watches a last-tick timestamp the loop bumps each
    iteration, and logs an ERROR plus fires one operator alert when the loop has
    gone silent for longer than ``stall_seconds``. It re-arms once the loop
    recovers, so a transient stall pages once rather than on every check.

    Some loop checks are *known* to block for minutes by design (the nightly
    sleep cycle runs synchronous LLM extraction per user; the DB-health sweep
    walks every per-user DB). Those would otherwise trip the watchdog every
    night. The loop wraps them in ``with watchdog.suspended():`` so the watchdog
    only fires on *unexpected* stalls — the regressions this is meant to catch.
    (Those checks do still pause dispatch while they run; moving them off the
    loop thread is tracked separately.)
    """

    def __init__(self, config: Config, stall_seconds: int):
        self._config = config
        self._stall_seconds = stall_seconds
        self._last_tick = time.time()
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        self._alerted = False
        self._suspended = False

    def tick(self) -> None:
        """Record a live loop iteration; re-arm after a recovery."""
        self._last_tick = time.time()
        if self._alerted:
            self._alerted = False
            logger.info("scheduler main loop recovered from stall")

    @contextlib.contextmanager
    def suspended(self):
        """Pause stall detection around a known-long synchronous check.

        Resets the tick on both entry and exit so the long operation is not
        counted as a stall and the first post-resume iteration starts clean.
        """
        self._suspended = True
        self._last_tick = time.time()
        try:
            yield
        finally:
            self._suspended = False
            self._last_tick = time.time()

    def start(self) -> None:
        if self._stall_seconds <= 0:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="loop-watchdog",
        )
        self._thread.start()
        logger.info(
            "STARTUP Started loop-stall watchdog (threshold %ds)",
            self._stall_seconds,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        # Poll at ~1/4 the stall window, clamped to a sane [5, 30]s band.
        interval = max(5.0, min(30.0, self._stall_seconds / 4))
        while not self._stop.wait(interval):
            if self._suspended:
                continue
            stalled_for = time.time() - self._last_tick
            if stalled_for >= self._stall_seconds and not self._alerted:
                self._alerted = True
                logger.error(
                    "scheduler main loop stalled: no dispatch tick for %.0fs "
                    "(threshold %ds) — task processing is blocked for all rooms",
                    stalled_for, self._stall_seconds,
                )
                self._fire_alert(stalled_for)

    def _fire_alert(self, stalled_for: float) -> None:
        user_id = _operator_alert_user(self._config)
        if not user_id:
            return

        # Deliver off the watchdog thread: the alert path itself goes through the
        # persistent asyncio loop (run_coro, timeout=None), and if *that* is what
        # is wedged a synchronous send would block the watchdog forever. A daemon
        # thread bounds the watchdog's exposure.
        def _deliver():
            try:
                send_notification(
                    self._config, user_id,
                    f"⚠️ Scheduler main loop stalled — no dispatch tick for "
                    f"{stalled_for:.0f}s. Task processing is blocked for all rooms.",
                    purpose="alert",
                )
            except Exception:  # noqa: BLE001
                logger.debug("loop-stall alert delivery failed", exc_info=True)

        threading.Thread(target=_deliver, daemon=True, name="loop-watchdog-alert").start()


def _emit_scheduler_stats(config: Config, pool: "WorkerPool | None") -> None:
    """Emit one ``scheduler_stats`` health line for the long-running daemon.

    Process-wide signal designed to surface resource leaks (the kind that ate
    the host in ISSUE-101) within single-digit minutes instead of days. Shape
    is space-separated ``key=value`` pairs, matching the devbox-proxy audit
    format, on the dedicated ``istota.scheduler.stats`` logger::

        scheduler_stats threads=42 fds=87 rss_mb=312 tasks_running=2 workers_active=3

    Cheap (sub-millisecond) and defensive: every collector degrades rather than
    raising. psutil-derived fields (fds, rss_mb) are omitted when psutil is
    unavailable (a single startup-style WARN, not one per emit); a DB hiccup
    yields ``tasks_running=?``; a missing pool yields ``workers_active=0``. The
    whole body is wrapped so a stats failure can never kill the daemon loop.
    """
    global _psutil_unavailable_warned
    try:
        parts = [f"threads={threading.active_count()}"]

        # fds + rss_mb via psutil. Each field is collected independently and
        # omitted on *any* failure (not just ImportError) — the line must still
        # emit. This matters most under fd exhaustion (the ISSUE-101 class):
        # psutil.num_fds() does os.listdir("/proc/self/fd"), which raises
        # OSError(EMFILE) precisely when the leak this line exists to catch is
        # at its worst. Dropping the whole line there would blind the operator
        # exactly when threads= is the signal they need.
        proc = None
        try:
            import psutil  # noqa: PLC0415  -- optional dep, lazy by design

            proc = psutil.Process()
        except ImportError:
            if not _psutil_unavailable_warned:
                logger.warning(
                    "scheduler_stats: psutil unavailable — "
                    "omitting fds/rss_mb from the health line",
                )
                _psutil_unavailable_warned = True
        except Exception:  # noqa: BLE001  -- AccessDenied / NoSuchProcess etc.
            pass

        if proc is not None:
            try:
                parts.append(f"fds={proc.num_fds()}")
            except Exception:  # noqa: BLE001  -- e.g. EMFILE under fd exhaustion
                pass
            try:
                parts.append(f"rss_mb={int(proc.memory_info().rss / 1024 / 1024)}")
            except Exception:  # noqa: BLE001
                pass

        # Running-task denominator. Never let a locked / mid-repair DB abort
        # the emit — degrade to '?' instead.
        try:
            with db.get_db(config.db_path) as conn:
                running = db.count_running_tasks(conn)
            parts.append(f"tasks_running={running}")
        except Exception:  # noqa: BLE001
            parts.append("tasks_running=?")

        workers_active = pool.active_count if pool is not None else 0
        parts.append(f"workers_active={workers_active}")

        _SCHEDULER_STATS_LOGGER.info("scheduler_stats " + " ".join(parts))
    except Exception as exc:  # noqa: BLE001  -- stats must never crash the loop
        # Route to the stats logger so a consumer filtering by logger name (not
        # just grepping the message) sees the gap rather than silent absence.
        _SCHEDULER_STATS_LOGGER.warning(
            "scheduler_stats emit failed: %s", exc, exc_info=True,
        )


def run_cleanup_checks(config: Config) -> None:
    """
    Run all cleanup checks for scheduler robustness.
    Call periodically from daemon loop.

    Synchronous: the rare Talk notices (expired-confirmation / failed-ancient)
    go through the sync ``send_notification`` dispatcher, which routes Talk
    through the persistent loop. The body otherwise does blocking DB / IMAP /
    filesystem cleanup that must NOT run on the persistent asyncio loop.
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
                    msg = (
                        "Your pending confirmation request timed out and was cancelled. "
                        "Please submit your request again if you still need this action."
                    )
                    send_notification(
                        config, task_info["user_id"], msg,
                        conversation_token=task_info["conversation_token"],
                    )
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
            # Notify only for tasks the user actually submitted. Automated tasks
            # (scheduled jobs, briefings, heartbeats, subtasks) pile up on their
            # own when the queue wedges; notifying their output channel turns one
            # stuck worker into a per-minute "task cancelled" flood — the message
            # ("A task you submitted…") isn't even true for them.
            if task_info["source_type"] in _AUTOMATED_SOURCE_TYPES:
                continue
            # Notify user via Talk if conversation_token is set
            if task_info["conversation_token"] and config.nextcloud.url:
                try:
                    msg = (
                        "A task you submitted was cancelled because it was pending too long "
                        "without being processed. Please try again or contact support if this "
                        "keeps happening."
                    )
                    send_notification(
                        config, task_info["user_id"], msg,
                        conversation_token=task_info["conversation_token"],
                    )
                except Exception as e:
                    logger.error(f"Failed to notify user about failed task: {e}")

        # 4. Clean up old completed tasks
        deleted_count = db.cleanup_old_tasks(conn, sched.task_retention_days)
        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} old task(s)")

    # 5. Clean up old emails from IMAP (outside db context)
    if config.email.enabled and sched.email_retention_days > 0:
        try:
            from .email_support import cleanup_old_emails
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
                # No location.db yet (user never ingested a ping) → nothing
                # to clean up. The file + parent dir are created on first
                # webhook write; connecting before then raises "unable to
                # open database file".
                if not ctx.db_path.exists():
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
            # No location.db yet (user never ingested a ping) → no visits
            # to reconcile. See the matching guard in run_cleanup_checks.
            if not ctx.db_path.exists():
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
                # Overlap guard: don't stack a new run while a prior run of the
                # same job is still in flight. A `* * * * *` job behind a wedged
                # single background worker would otherwise grow the queue one
                # row/minute (the location-alert incident). last_run_at is left
                # untouched so the job fires the next tick once the in-flight run
                # clears — correct for sparse jobs too (advancing it would push
                # the next fire out by a full interval).
                inflight = db.count_inflight_tasks_for_scheduled_job(conn, job.id)
                if inflight:
                    logger.warning(
                        "Scheduled job '%s' (user: %s) skipped: %d prior run(s) "
                        "still in flight",
                        job.name, job.user_id, inflight,
                    )
                    continue
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
            from .transport.talk import poll_talk_conversations
            # Single-pass mode still lazily uses the persistent loop here:
            # poll_talk_conversations pulls the shared get_talk_client singleton
            # (whose httpx pool is bound to the persistent loop), and the shared
            # process_one_task delivery path already submits via run_coro, so all
            # Talk I/O must stay on one loop. The persistent runtime is a single
            # daemon thread that exits with the one-shot process.
            talk_tasks = run_coro(poll_talk_conversations(config))
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
        from .transport.email import poll_emails

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

    # Single-pass mode lazily started the persistent runtime via run_coro
    # (poller / delivery). Stop it so the shared httpx client's aclose hook
    # runs a clean TLS shutdown instead of the connections being dropped on
    # process exit. No-op if the runtime was never started (Talk disabled).
    reset_async_runtime()

    return processed


def _talk_poll_loop(config: Config) -> None:
    """Background thread: continuously polls Talk conversations."""
    from .transport.talk import poll_talk_conversations

    while not _shutdown_requested:
        try:
            # Runs on the shared persistent loop. The long-poll awaits yield the
            # loop, so delivery coroutines (acks, results) submitted via run_coro
            # interleave normally; the poll's own FIRST_COMPLETED + cancel only
            # touches the tasks it created. submit timeout=None matches the old
            # asyncio.run-forever semantics — httpx-level timeouts bound each poll.
            talk_tasks = run_coro(poll_talk_conversations(config))
            if talk_tasks:
                logger.info("Queued %d Talk task(s)", len(talk_tasks))
        except Exception as e:
            logger.error("Talk poll error: %s", e)
        time.sleep(config.scheduler.talk_poll_interval)


def _dispatch_sleep(
    pool: "WorkerPool", config: Config, should_stop: Callable[[], bool]
) -> None:
    """Sleep out one base poll tick, re-dispatching workers in sub-tick slices.

    Every periodic check in the daemon loop is self-throttled by its own
    interval timer, so the only work that needs to happen on every base tick is
    ``pool.dispatch()`` (pending-task discovery). Sleeping the whole
    ``poll_interval`` in one shot means a freshly-enqueued task waits up to a
    full ``poll_interval`` before a worker claims it. Instead we sleep in
    ``dispatch_interval`` slices and dispatch after each, so cold pickup latency
    is bounded by ``dispatch_interval`` — without re-running any of the heavy
    interval-gated checks (they stay on ``poll_interval`` granularity because the
    slice loop consumes one full base tick before the outer loop iterates).

    ``dispatch_interval`` <= 0 or >= ``poll_interval`` restores the legacy
    single-sleep-per-tick behaviour. ``should_stop`` is polled before and after
    each slice so shutdown is honoured within one ``dispatch_interval``.
    """
    base = config.scheduler.poll_interval
    slice_s = config.scheduler.dispatch_interval
    if slice_s <= 0 or slice_s >= base:
        time.sleep(base)
        return
    deadline = time.monotonic() + base
    while not should_stop():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(slice_s, remaining))
        if should_stop():
            return
        try:
            pool.dispatch()
        except Exception as e:  # noqa: BLE001
            logger.error("Error dispatching workers: %s", e)


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

    # Reclaim tasks abandoned mid-execution by a dead prior instance before any
    # worker runs. Under the flock every running/locked row is an orphan, so
    # this recovers a restart in seconds instead of waiting out
    # worker_stuck_minutes (DB-only; doesn't need the asyncio runtime).
    try:
        recover_orphaned_tasks_on_startup(config)
    except Exception as e:  # noqa: BLE001
        logger.warning("Startup orphan recovery skipped: %s", e)

    # Start the persistent asyncio runtime that hosts all Talk (and other) I/O
    # on one loop with one pooled httpx client. Explicit start here surfaces a
    # loop-creation failure before the daemon goes live rather than lazily on the
    # first run_coro call; every run_coro site (poller, delivery, consumers,
    # notifications) shares it.
    from .async_runtime import get_async_runtime
    get_async_runtime()
    logger.info("STARTUP Started persistent asyncio runtime")

    # Start Talk polling in background thread so it runs independently of task processing
    if config.talk.enabled:
        talk_thread = threading.Thread(
            target=_talk_poll_loop, args=(config,), daemon=True, name="talk-poller",
        )
        talk_thread.start()
        logger.info("STARTUP Started Talk polling thread")

    # Create worker pool for per-user concurrent task processing
    pool = WorkerPool(config)

    # Defense-in-depth: a separate thread alerts if the single-threaded main
    # loop stops ticking (ISSUE-143). The loop bumps watchdog.tick() each
    # iteration below.
    watchdog = LoopWatchdog(config, config.scheduler.loop_stall_alert_seconds)
    watchdog.start()

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
    # Seed the backup clock from the persisted last-run timestamp so it survives
    # restarts: an overdue backup (or one that never ran) fires promptly, a
    # recent one waits out the remaining interval. Without this the clock reset
    # every boot and a host deploying more than once a day never backed up.
    from . import db_backup as _db_backup
    last_db_backup = _db_backup.last_backup_time(config)
    # Re-armable flag so a silently-stopped backup pages once, not every tick.
    backup_stale_alerted = False
    last_status_write = 0.0
    last_trigger_check = 0.0
    # Init to "now" (not 0.0) so the first stats line fires after one full
    # interval — avoids a noisy emit during startup while state is hydrating.
    last_stats_check = time.time()

    while not _shutdown_requested:
        # Mark the loop alive for the stall watchdog before doing any work.
        watchdog.tick()

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

        # Check sleep cycles periodically (same interval as briefings). The
        # extraction runs synchronous per-user LLM calls and can take minutes;
        # suspend the stall watchdog so a healthy nightly run doesn't page.
        if now - last_sleep_cycle_check >= config.scheduler.briefing_check_interval:
            try:
                from .memory.sleep_cycle import check_sleep_cycles
                with watchdog.suspended(), db.get_db(config.db_path) as conn:
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
                with watchdog.suspended(), db.get_db(config.db_path) as conn:
                    channel_tokens = check_channel_sleep_cycles(conn, config)
                    if channel_tokens:
                        logger.info("Ran channel sleep cycle for %d channel(s): %s", len(channel_tokens), ", ".join(channel_tokens))
            except Exception as e:
                logger.error("Error running channel sleep cycles: %s", e)
            last_channel_sleep_cycle_check = now

        # Poll emails periodically
        if config.email.enabled and now - last_email_poll >= config.scheduler.email_poll_interval:
            try:
                from .transport.email import poll_emails
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
                run_cleanup_checks(config)
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
                # A full quick_check + REINDEX sweep over every per-user DB can
                # take a while; suspend the stall watchdog around it.
                with watchdog.suspended():
                    check_db_health(config)
            except Exception as e:  # noqa: BLE001
                logger.error("Error running DB health checks: %s", e)
            last_db_health_check = now

        # Snapshot local DBs to the mount for off-host durability (they left the
        # Nextcloud-synced workspaces when they moved to local disk). Same
        # watchdog-suspend treatment as the health sweep — a full snapshot over
        # every per-user DB can take a while.
        if (
            config.scheduler.db_backup_enabled
            and config.scheduler.db_backup_interval
            and now - last_db_backup >= config.scheduler.db_backup_interval
        ):
            try:
                from .db_backup import backup_databases
                with watchdog.suspended():
                    backup_results = backup_databases(config)
                _alert_backup_problems(config, backup_results)
            except Exception as e:  # noqa: BLE001
                logger.error("Error running DB backup: %s", e)
            last_db_backup = now

        # Staleness alert (issue #6): a persisted last-run older than 2x the
        # interval means backups have silently stopped (the clock-reset defect's
        # failure mode). The clock now only advances on a durable OK run, so this
        # also catches the mount-down case where snapshots can't be written.
        if config.scheduler.db_backup_enabled and config.scheduler.db_backup_interval:
            persisted = _db_backup.last_backup_time(config)
            backup_stale_alerted = _maybe_alert_backup_stale(
                config, now, persisted, backup_stale_alerted
            )

        # Emit the periodic process-health line (threads / fds / rss /
        # running-tasks / active-workers). interval == 0 disables it.
        if (
            config.scheduler.scheduler_stats_interval
            and now - last_stats_check >= config.scheduler.scheduler_stats_interval
        ):
            _emit_scheduler_stats(config, pool)
            last_stats_check = now

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

        # Sleep out the rest of the base tick, re-dispatching in sub-tick slices
        # so a freshly-enqueued task is claimed within dispatch_interval instead
        # of waiting a full poll_interval (the gated checks above stay on
        # poll_interval granularity — see _dispatch_sleep).
        _dispatch_sleep(pool, config, lambda: _shutdown_requested)

    # Stop the stall watchdog before tearing the rest down.
    watchdog.stop()

    # Shutdown workers before releasing lock
    pool.shutdown()

    # Stop the persistent asyncio runtime after the worker pool: cancels any
    # in-flight coroutine (the talk-poller daemon thread may be mid long-poll),
    # then runs cleanup hooks (closes the shared TalkClient httpx pool) and stops
    # the loop. reset_async_runtime also clears the process globals so an
    # in-process restart rebuilds from a clean slate. Best-effort — a hung
    # network coro can't block daemon shutdown (stop has its own timeout).
    try:
        reset_async_runtime()
        logger.info("Stopped persistent asyncio runtime")
    except Exception as e:  # noqa: BLE001
        logger.warning("Error stopping persistent asyncio runtime: %s", e)

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
