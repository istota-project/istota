"""Heartbeat monitoring system for periodic health checks."""

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx

from . import db
from .storage import get_user_heartbeat_path

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger("istota.heartbeat")

# Pattern to extract TOML blocks from markdown
_TOML_BLOCK_RE = re.compile(r"```toml\s*\n(.*?)```", re.DOTALL)


@dataclass
class HeartbeatSettings:
    """Global heartbeat settings for a user."""
    conversation_token: str = ""
    quiet_hours: list[str] = field(default_factory=list)
    default_cooldown_minutes: int = 60


@dataclass
class HeartbeatCheck:
    """Definition of a single heartbeat check."""
    name: str
    type: str  # file-watch, shell-command, url-health, calendar-conflicts, task-deadline, self-check
    config: dict  # type-specific fields
    channel: str = "talk"
    cooldown_minutes: int | None = None
    interval_minutes: int | None = None  # Per-check frequency (None = every cycle)


@dataclass
class CheckResult:
    """Result of running a heartbeat check."""
    healthy: bool
    message: str
    details: dict | None = None


def _get_mount_path(config: "Config", path: str) -> Path:
    """Get the local mount path for a Nextcloud path."""
    return config.nextcloud_mount_path / path.lstrip("/")


def load_heartbeat_config(
    config: "Config",
    user_id: str,
) -> tuple[HeartbeatSettings, list[HeartbeatCheck]] | None:
    """
    Load heartbeat configuration from a user's HEARTBEAT.md file.

    Returns (settings, checks) tuple, or None if no config found.
    """
    if not config.use_mount:
        logger.debug("Heartbeat requires mount; skipping user %s", user_id)
        return None

    heartbeat_path = _get_mount_path(config, get_user_heartbeat_path(user_id, config.bot_dir_name))
    if not heartbeat_path.exists():
        return None

    content = heartbeat_path.read_text()
    if not content.strip():
        return None

    # Extract TOML block from markdown
    match = _TOML_BLOCK_RE.search(content)
    if not match:
        logger.debug("No TOML block found in %s", heartbeat_path)
        return None

    toml_content = match.group(1)
    if not toml_content.strip():
        return None

    # Check if all lines are comments
    lines = [line.strip() for line in toml_content.strip().split("\n")]
    non_comment_lines = [line for line in lines if line and not line.startswith("#")]
    if not non_comment_lines:
        return None

    try:
        import tomllib
        data = tomllib.loads(toml_content)
    except Exception as e:
        logger.warning("Failed to parse heartbeat config for %s: %s", user_id, e)
        return None

    # Parse settings
    settings_data = data.get("settings", {})
    settings = HeartbeatSettings(
        conversation_token=settings_data.get("conversation_token", ""),
        quiet_hours=settings_data.get("quiet_hours", []),
        default_cooldown_minutes=settings_data.get("default_cooldown_minutes", 60),
    )

    # Parse checks
    checks = []
    for check_data in data.get("checks", []):
        name = check_data.get("name", "")
        check_type = check_data.get("type", "")
        if not name or not check_type:
            continue

        # Extract type-specific config (all fields except top-level check fields)
        _top_level_fields = ("name", "type", "channel", "cooldown_minutes", "interval_minutes")
        check_config = {
            k: v for k, v in check_data.items()
            if k not in _top_level_fields
        }

        checks.append(HeartbeatCheck(
            name=name,
            type=check_type,
            config=check_config,
            channel=check_data.get("channel", "talk"),
            cooldown_minutes=check_data.get("cooldown_minutes"),
            interval_minutes=check_data.get("interval_minutes"),
        ))

    if not checks:
        return None

    logger.debug("Loaded %d heartbeat check(s) for user %s", len(checks), user_id)
    return settings, checks


def is_quiet_hours(user_tz_str: str, quiet_hours: list[str]) -> bool:
    """
    Check if current time is within quiet hours.

    Handles both same-day ranges (09:00-17:00) and cross-midnight ranges (22:00-07:00).
    """
    if not quiet_hours:
        return False

    try:
        user_tz = ZoneInfo(user_tz_str)
    except Exception:
        user_tz = ZoneInfo("UTC")

    now = datetime.now(user_tz)
    current_minutes = now.hour * 60 + now.minute

    for time_range in quiet_hours:
        if "-" not in time_range:
            continue

        try:
            start_str, end_str = time_range.split("-", 1)
            start_h, start_m = map(int, start_str.strip().split(":"))
            end_h, end_m = map(int, end_str.strip().split(":"))
            start_minutes = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m

            if start_minutes <= end_minutes:
                # Same-day range (e.g., 09:00-17:00)
                if start_minutes <= current_minutes < end_minutes:
                    return True
            else:
                # Cross-midnight range (e.g., 22:00-07:00)
                if current_minutes >= start_minutes or current_minutes < end_minutes:
                    return True
        except (ValueError, AttributeError):
            continue

    return False


# ============================================================================
# Check handlers
# ============================================================================


def _check_file_watch(check: HeartbeatCheck, config: "Config") -> CheckResult:
    """
    Check file age or existence.

    Config fields:
        path: Nextcloud path to file
        max_age_hours: Maximum age in hours (optional)
    """
    file_path = check.config.get("path", "")
    max_age_hours = check.config.get("max_age_hours")

    if not file_path:
        return CheckResult(healthy=False, message="No path configured")

    if not config.use_mount:
        return CheckResult(healthy=False, message="File watch requires mount")

    local_path = _get_mount_path(config, file_path)
    if not local_path.exists():
        return CheckResult(
            healthy=False,
            message=f"File not found: {file_path}",
            details={"path": file_path},
        )

    if max_age_hours is not None:
        try:
            mtime = local_path.stat().st_mtime
            age_hours = (datetime.now().timestamp() - mtime) / 3600
            if age_hours > max_age_hours:
                return CheckResult(
                    healthy=False,
                    message=f"File too old: {file_path} ({age_hours:.1f}h > {max_age_hours}h)",
                    details={"path": file_path, "age_hours": age_hours, "max_age_hours": max_age_hours},
                )
        except OSError as e:
            return CheckResult(healthy=False, message=f"Error checking file: {e}")

    return CheckResult(healthy=True, message=f"File OK: {file_path}")


def _check_shell_command(check: HeartbeatCheck, config: "Config") -> CheckResult:
    """
    Run a shell command and evaluate the condition.

    Config fields:
        command: Shell command to run
        condition: Simple comparison (< N, > N, == N, contains:X, not-contains:X)
        message: Alert message template with {value} placeholder
        timeout: Command timeout in seconds (default: 30)
    """
    command = check.config.get("command", "")
    condition = check.config.get("condition", "")
    message_template = check.config.get("message", "Check failed: {value}")
    timeout = check.config.get("timeout", 30)

    if not command:
        return CheckResult(healthy=False, message="No command configured")

    try:
        from .executor import build_stripped_env
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=build_stripped_env(),
        )
        value = result.stdout.strip()
    except subprocess.TimeoutExpired:
        return CheckResult(healthy=False, message=f"Command timed out after {timeout}s")
    except Exception as e:
        return CheckResult(healthy=False, message=f"Command error: {e}")

    if not condition:
        # No condition: healthy if exit code is 0
        healthy = result.returncode == 0
        return CheckResult(
            healthy=healthy,
            message="Command succeeded" if healthy else f"Command failed (exit {result.returncode})",
            details={"value": value, "returncode": result.returncode},
        )

    # Evaluate condition
    healthy = False
    try:
        if condition.startswith("<"):
            threshold = float(condition[1:].strip())
            healthy = float(value) < threshold
        elif condition.startswith(">"):
            threshold = float(condition[1:].strip())
            healthy = float(value) > threshold
        elif condition.startswith("=="):
            expected = condition[2:].strip()
            healthy = value == expected
        elif condition.startswith("contains:"):
            substring = condition[9:]
            healthy = substring in value
        elif condition.startswith("not-contains:"):
            substring = condition[13:]
            healthy = substring not in value
        else:
            return CheckResult(
                healthy=False,
                message=f"Unknown condition format: {condition}",
            )
    except (ValueError, TypeError) as e:
        return CheckResult(
            healthy=False,
            message=f"Condition evaluation error: {e}",
            details={"value": value, "condition": condition},
        )

    if healthy:
        return CheckResult(healthy=True, message="Check passed", details={"value": value})

    return CheckResult(
        healthy=False,
        message=message_template.format(value=value),
        details={"value": value, "condition": condition},
    )


def _check_url_health(check: HeartbeatCheck, config: "Config") -> CheckResult:
    """
    HTTP health check.

    Config fields:
        url: URL to check
        expected_status: Expected HTTP status code (default: 200)
        timeout: Request timeout in seconds (default: 10)
    """
    url = check.config.get("url", "")
    expected_status = check.config.get("expected_status", 200)
    timeout = check.config.get("timeout", 10)

    if not url:
        return CheckResult(healthy=False, message="No URL configured")

    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        if response.status_code == expected_status:
            return CheckResult(
                healthy=True,
                message=f"URL healthy: {url}",
                details={"status_code": response.status_code},
            )
        else:
            return CheckResult(
                healthy=False,
                message=f"URL returned {response.status_code}, expected {expected_status}",
                details={"url": url, "status_code": response.status_code, "expected": expected_status},
            )
    except httpx.TimeoutException:
        return CheckResult(
            healthy=False,
            message=f"URL timeout after {timeout}s: {url}",
            details={"url": url, "timeout": timeout},
        )
    except Exception as e:
        return CheckResult(
            healthy=False,
            message=f"URL check failed: {e}",
            details={"url": url, "error": str(e)},
        )


def _check_calendar_conflicts(check: HeartbeatCheck, config: "Config", user_id: str) -> CheckResult:
    """
    Find overlapping calendar events.

    Config fields:
        lookahead_hours: Hours to look ahead (default: 24)
    """
    lookahead_hours = check.config.get("lookahead_hours", 24)

    if not config.caldav_url:
        return CheckResult(healthy=False, message="CalDAV not configured")

    try:
        from .skills.calendar import list_calendars, list_events

        # Get user's calendars
        calendars = list_calendars(
            caldav_url=config.caldav_url,
            username=config.caldav_username,
            password=config.caldav_password,
            user_id=user_id,
        )

        if not calendars:
            return CheckResult(healthy=True, message="No calendars found")

        # Collect all events
        now = datetime.now()
        end_time = datetime.now().replace(
            hour=23, minute=59, second=59
        )
        # Extend to lookahead hours
        from datetime import timedelta
        end_time = now + timedelta(hours=lookahead_hours)

        all_events = []
        for cal in calendars:
            try:
                events = list_events(
                    caldav_url=config.caldav_url,
                    username=config.caldav_username,
                    password=config.caldav_password,
                    calendar_path=cal["path"],
                    start_date=now.strftime("%Y-%m-%d"),
                    end_date=end_time.strftime("%Y-%m-%d"),
                )
                for event in events:
                    if event.get("start") and event.get("end"):
                        all_events.append(event)
            except Exception as e:
                logger.debug("Error listing events from %s: %s", cal.get("name"), e)

        if not all_events:
            return CheckResult(healthy=True, message="No upcoming events")

        # Check for overlaps
        conflicts = []
        for i, event1 in enumerate(all_events):
            for event2 in all_events[i + 1:]:
                # Parse times (simplified - assumes ISO format)
                try:
                    start1 = datetime.fromisoformat(event1["start"].replace("Z", "+00:00"))
                    end1 = datetime.fromisoformat(event1["end"].replace("Z", "+00:00"))
                    start2 = datetime.fromisoformat(event2["start"].replace("Z", "+00:00"))
                    end2 = datetime.fromisoformat(event2["end"].replace("Z", "+00:00"))

                    # Check overlap
                    if start1 < end2 and start2 < end1:
                        conflicts.append({
                            "event1": event1.get("summary", "Untitled"),
                            "event2": event2.get("summary", "Untitled"),
                            "time": event1["start"],
                        })
                except (ValueError, TypeError):
                    continue

        if conflicts:
            conflict_desc = ", ".join(
                f"'{c['event1']}' and '{c['event2']}'" for c in conflicts[:3]
            )
            return CheckResult(
                healthy=False,
                message=f"Calendar conflicts found: {conflict_desc}",
                details={"conflicts": conflicts},
            )

        return CheckResult(healthy=True, message="No calendar conflicts")

    except ImportError:
        return CheckResult(healthy=False, message="Calendar skill not available")
    except Exception as e:
        return CheckResult(healthy=False, message=f"Calendar check error: {e}")


def _check_task_deadline(check: HeartbeatCheck, config: "Config", user_id: str) -> CheckResult:
    """
    Check for overdue tasks from TASKS.md.

    Config fields:
        source: "file" (only supported option currently)
        warn_hours_before: Hours before deadline to warn (default: 24)
    """
    warn_hours_before = check.config.get("warn_hours_before", 24)

    if not config.use_mount:
        return CheckResult(healthy=False, message="Task deadline check requires mount")

    from .storage import get_user_tasks_file_path

    tasks_path = _get_mount_path(config, get_user_tasks_file_path(user_id, config.bot_dir_name))
    if not tasks_path.exists():
        return CheckResult(healthy=True, message="No TASKS.md file")

    try:
        content = tasks_path.read_text()
    except OSError as e:
        return CheckResult(healthy=False, message=f"Error reading TASKS.md: {e}")

    # Parse tasks with deadlines
    # Look for patterns like: - [ ] Task @due(2024-01-15) or - [ ] Task (due: 2024-01-15)
    deadline_pattern = re.compile(
        r"^- \[ \].*?(?:@due\(|due:\s*)(\d{4}-\d{2}-\d{2})",
        re.MULTILINE | re.IGNORECASE,
    )

    now = datetime.now()
    overdue = []
    upcoming = []

    for match in deadline_pattern.finditer(content):
        try:
            deadline = datetime.strptime(match.group(1), "%Y-%m-%d")
            # Extract task text (first line)
            line_start = content.rfind("\n", 0, match.start()) + 1
            line_end = content.find("\n", match.start())
            if line_end == -1:
                line_end = len(content)
            task_text = content[line_start:line_end].strip()
            # Clean up the task text
            task_text = re.sub(r"^- \[ \]\s*", "", task_text)[:60]

            hours_until = (deadline - now).total_seconds() / 3600
            if hours_until < 0:
                overdue.append({"task": task_text, "deadline": match.group(1)})
            elif hours_until <= warn_hours_before:
                upcoming.append({"task": task_text, "deadline": match.group(1), "hours": hours_until})
        except ValueError:
            continue

    if overdue:
        desc = ", ".join(t["task"][:30] for t in overdue[:3])
        return CheckResult(
            healthy=False,
            message=f"Overdue tasks: {desc}",
            details={"overdue": overdue, "upcoming": upcoming},
        )

    if upcoming:
        desc = ", ".join(f"{t['task'][:30]} (in {t['hours']:.0f}h)" for t in upcoming[:3])
        return CheckResult(
            healthy=False,
            message=f"Tasks due soon: {desc}",
            details={"overdue": overdue, "upcoming": upcoming},
        )

    return CheckResult(healthy=True, message="No overdue or upcoming deadlines")


def _check_self(check: HeartbeatCheck, config: "Config", user_id: str) -> CheckResult:
    """
    Run system health diagnostics (mirrors !check command).

    Config fields:
        execution_test: Whether to run Claude CLI invocation test (default: True)
    """
    from .executor import build_bwrap_cmd, build_clean_env

    failures = []

    # 1. Claude binary
    if not shutil.which("claude"):
        failures.append("Claude binary not found in PATH")

    # 2. Sandbox (bwrap) — only if sandbox enabled
    if config.security.sandbox_enabled and not shutil.which("bwrap"):
        failures.append("bwrap not found in PATH (sandbox enabled)")

    # 3. DB health
    try:
        with db.get_db(config.db_path) as conn:
            conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
    except Exception as e:
        failures.append(f"Database error: {e}")

    # 4. Recent task failure rate (last hour)
    try:
        with db.get_db(config.db_path) as conn:
            stats = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
                FROM tasks
                WHERE created_at > datetime('now', '-1 hour')
                """,
            ).fetchone()
            completed = stats[0] or 0
            failed = stats[1] or 0
            if failed > 0 and failed >= completed:
                failures.append(
                    f"High failure rate: {failed} failed vs {completed} completed in last hour"
                )
    except Exception as e:
        failures.append(f"Task stats error: {e}")

    # 5. Claude execution test (optional, default enabled)
    if check.config.get("execution_test", True):
        try:
            cmd = [
                "claude", "-p", "Run: echo healthcheck-ok",
                "--allowedTools", "Bash",
                "--output-format", "text",
            ]

            env = build_clean_env(config)
            if not env.get("ANTHROPIC_API_KEY"):
                val = os.environ.get("ANTHROPIC_API_KEY")
                if val:
                    env["ANTHROPIC_API_KEY"] = val

            if config.security.sandbox_enabled:
                fake_task = db.Task(
                    id=0, status="running", source_type="cli",
                    user_id=user_id, prompt="healthcheck",
                    conversation_token="",
                )
                try:
                    with db.get_db(config.db_path) as conn:
                        user_resources = db.get_user_resources(conn, user_id)
                except Exception:
                    user_resources = []
                user_temp = config.temp_dir / user_id
                user_temp.mkdir(parents=True, exist_ok=True)
                is_admin = config.is_admin(user_id)
                cmd = build_bwrap_cmd(cmd, config, fake_task, is_admin, user_resources, user_temp)

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, env=env,
            )
            if "healthcheck-ok" not in result.stdout:
                failures.append("Claude execution test: 'healthcheck-ok' not in output")
        except subprocess.TimeoutExpired:
            failures.append("Claude execution test timed out (30s)")
        except Exception as e:
            failures.append(f"Claude execution test error: {e}")

    if failures:
        return CheckResult(
            healthy=False,
            message="; ".join(failures),
            details={"failures": failures},
        )

    return CheckResult(healthy=True, message="All self-checks passed")


# Handler dispatch table
_CHECK_HANDLERS = {
    "file-watch": _check_file_watch,
    "shell-command": _check_shell_command,
    "url-health": _check_url_health,
    "calendar-conflicts": _check_calendar_conflicts,
    "task-deadline": _check_task_deadline,
    "self-check": _check_self,
}


def run_check(
    check: HeartbeatCheck,
    config: "Config",
    user_id: str,
) -> CheckResult:
    """Run a single heartbeat check."""
    handler = _CHECK_HANDLERS.get(check.type)
    if not handler:
        return CheckResult(
            healthy=False,
            message=f"Unknown check type: {check.type}",
        )

    try:
        # Some handlers need user_id (calendar, task-deadline, self-check)
        if check.type in ("calendar-conflicts", "task-deadline", "self-check"):
            return handler(check, config, user_id)
        else:
            return handler(check, config)
    except Exception as e:
        logger.exception("Error running check %s for user %s", check.name, user_id)
        return CheckResult(
            healthy=False,
            message=f"Check error: {e}",
        )


def should_alert(
    conn,
    user_id: str,
    check: HeartbeatCheck,
    result: CheckResult,
    settings: HeartbeatSettings,
    user_tz: str,
) -> bool:
    """
    Determine if an alert should be sent for this check result.

    Returns False if:
    - Check is healthy
    - Within cooldown period
    - Within quiet hours
    """
    if result.healthy:
        return False

    # Check quiet hours
    if is_quiet_hours(user_tz, settings.quiet_hours):
        logger.debug("Skipping alert for %s/%s: quiet hours", user_id, check.name)
        return False

    # Check cooldown
    state = db.get_heartbeat_state(conn, user_id, check.name)
    if state and state.last_alert_at:
        try:
            last_alert = datetime.fromisoformat(state.last_alert_at)
            cooldown_minutes = check.cooldown_minutes or settings.default_cooldown_minutes
            cooldown_seconds = cooldown_minutes * 60
            elapsed = (datetime.now(ZoneInfo("UTC")).replace(tzinfo=None) - last_alert).total_seconds()
            if elapsed < cooldown_seconds:
                logger.debug(
                    "Skipping alert for %s/%s: cooldown (%d/%d seconds)",
                    user_id, check.name, int(elapsed), cooldown_seconds,
                )
                return False
        except (ValueError, TypeError):
            pass

    return True


def send_heartbeat_alert(
    config: "Config",
    user_id: str,
    check: HeartbeatCheck,
    result: CheckResult,
    settings: HeartbeatSettings,
) -> bool:
    """
    Send an alert for a failed heartbeat check.

    Returns True if alert was sent successfully.
    """
    from .notifications import send_notification

    message = f"**Heartbeat Alert: {check.name}**\n\n{result.message}"

    return send_notification(
        config, user_id, message,
        surface=check.channel,
        conversation_token=settings.conversation_token,
        title=f"Heartbeat Alert: {check.name}",
    )


def check_heartbeats(conn, config: "Config") -> list[str]:
    """
    Check all heartbeats for all users.

    Returns list of user IDs that were checked.
    """
    checked_users = []

    for user_id, user_config in config.users.items():
        result = load_heartbeat_config(config, user_id)
        if not result:
            continue

        settings, checks = result
        checked_users.append(user_id)
        user_tz = user_config.timezone

        for check in checks:
            # Skip if per-check interval hasn't elapsed
            if check.interval_minutes is not None:
                state = db.get_heartbeat_state(conn, user_id, check.name)
                if state and state.last_check_at:
                    try:
                        last_check = datetime.fromisoformat(state.last_check_at)
                        elapsed = (datetime.now(ZoneInfo("UTC")).replace(tzinfo=None) - last_check).total_seconds()
                        if elapsed < check.interval_minutes * 60:
                            continue
                    except (ValueError, TypeError):
                        pass

            # Run the check
            check_result = run_check(check, config, user_id)

            # Update state
            db.update_heartbeat_state(
                conn, user_id, check.name,
                last_check_at=True,
            )

            if check_result.healthy:
                db.update_heartbeat_state(
                    conn, user_id, check.name,
                    last_healthy_at=True,
                    reset_errors=True,
                )
            else:
                # Check if we should alert
                if should_alert(conn, user_id, check, check_result, settings, user_tz):
                    sent = send_heartbeat_alert(config, user_id, check, check_result, settings)
                    if sent:
                        db.update_heartbeat_state(
                            conn, user_id, check.name,
                            last_alert_at=True,
                        )
                    else:
                        db.update_heartbeat_state(
                            conn, user_id, check.name,
                            last_error_at=True,
                            increment_errors=True,
                        )

    return checked_users
