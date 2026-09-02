"""Heartbeat monitoring system for periodic health checks."""

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx

from . import db
from .shell_exec import SIGPIPE_EXIT, SIGPIPE_NOTE, shell_argv
from .toml_fence import find_toml_block

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger("istota.heartbeat")

# Where the fence starts and ends is `toml_fence`'s to say (ISSUE-386). This
# module used to carry its own copy of an expression that anchored neither
# marker, so a backtick run anywhere after the fence opened — in a comment,
# in a string value — ended the block early and every check below that point
# silently stopped existing.


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
    # Empty = no per-check override; the user's `alert` routing decides (default
    # talk). A non-empty value is an explicit per-check surface that wins.
    channel: str = ""
    cooldown_minutes: int | None = None
    interval_minutes: int | None = None  # Per-check frequency (None = every cycle)


@dataclass
class CheckResult:
    """Result of running a heartbeat check."""
    healthy: bool
    message: str
    details: dict | None = None
    #: What this failure *is*, for a check that can name its own failing parts.
    #:
    #: `should_alert`'s cooldown rate-limits a standing failure; it never ends
    #: one. That is right for a check whose failure is a fact about the world
    #: worth repeating — a `url-health` site that is still down an hour later —
    #: and wrong for one whose failure set includes conditions documented as
    #: the normal state of a deployment, which is what `self-check` became when
    #: it started rendering the doctor registry. A check that sets this is
    #: saying "an unchanged answer is not news": it pages when the signature
    #: changes and stays quiet while it does not.
    #:
    #: `None` — the default, and what all five other check types leave it — is
    #: exactly today's behaviour, which is why this is safe to add here rather
    #: than in a `self-check`-only branch of `should_alert`.
    alert_signature: str | None = None


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

    # Hardened like every other host-side read of `{bot_dir}/config/`
    # (ISSUE-339). This one runs on the scheduler's heartbeat tick, so a FIFO
    # planted at HEARTBEAT.md wedged that loop rather than a single task.
    from .storage import read_user_config_file  # noqa: PLC0415 - import cycle

    content = read_user_config_file(config, user_id, "HEARTBEAT.md")
    if content is None or not content.strip():
        return None

    # Extract TOML block from markdown
    span = find_toml_block(content)
    if span is None:
        logger.debug("No TOML block found in HEARTBEAT.md for %s", user_id)
        return None

    toml_content = content[span[0]:span[1]]
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
            channel=check_data.get("channel", ""),
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


def _build_heartbeat_skill_env(
    config: "Config", user_id: str,
) -> dict[str, str]:
    """Resolve skill-manifest env vars + setup_env hooks for a heartbeat
    shell-command. Mirrors what ``scheduler._execute_command_task`` does
    for command-type scheduled jobs (ISSUE-097) — without it, a heartbeat
    invoking ``istota-skill location current`` / ``istota-skill health …``
    would emit a JSON error envelope that the returncode-0 path silently
    treats as healthy.

    CalDAV discovery is intentionally skipped (``discovered_calendars=[]``)
    so heartbeats don't issue a PROPFIND on every tick. Heartbeats are not
    the right place to invoke calendar-skill CLIs anyway; the gate
    correctly drops CALDAV_* from the env.
    """
    from .skills._env import EnvContext, build_skill_env, dispatch_setup_env_hooks
    from .skills._loader import load_skill_index

    skill_index = load_skill_index(config.skills_dir, config.bundled_skills_dir)
    try:
        with db.get_db(config.db_path) as conn:
            user_resources = db.get_user_resources(conn, user_id)
    except Exception:
        user_resources = []

    user_temp_dir = config.temp_dir / user_id
    user_temp_dir.mkdir(parents=True, exist_ok=True)

    fake_task = db.Task(
        id=0, status="running", source_type="heartbeat",
        user_id=user_id, prompt="", conversation_token="",
    )
    ctx = EnvContext(
        config=config,
        task=fake_task,
        user_resources=user_resources,
        user_config=config.get_user(user_id),
        user_temp_dir=user_temp_dir,
        is_admin=config.is_admin(user_id),
        discovered_calendars=[],
    )

    env: dict[str, str] = {}
    env.update(build_skill_env(list(skill_index), skill_index, ctx))
    env.update(dispatch_setup_env_hooks(list(skill_index), skill_index, ctx))
    return env


def _check_shell_command(check: HeartbeatCheck, config: "Config", user_id: str | None = None) -> CheckResult:
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
        env = build_stripped_env()
        # Mirror scheduler._execute_command_task: module-skill CLIs (feeds, money)
        # spawned from a heartbeat check need to find the daemon's config and
        # know which user they're acting for. Without these, load_config() returns
        # a default Config() with empty users and the skill exits with a JSON
        # error envelope — but the shell command's exit 0 makes the heartbeat
        # look healthy.
        if config.config_path:
            env["ISTOTA_CONFIG_PATH"] = str(config.config_path)
        if config.db_path:
            env["ISTOTA_DB_PATH"] = str(config.db_path)
        if user_id:
            env["ISTOTA_USER_ID"] = user_id
        env["ISTOTA_EXPERIMENTAL_FEATURES"] = ",".join(config.experimental.features)
        # Skill-manifest env + setup_env hooks. Without these, heartbeat
        # shell-commands that invoke `istota-skill location current` /
        # `istota-skill health …` etc. see LOCATION_DB_PATH / HEALTH_DB_PATH
        # unset and emit a JSON error envelope — which the returncode-0
        # branch below would silently treat as healthy. ISSUE-097 fixed the
        # same shape for the scheduler command-task path.
        if user_id:
            try:
                env.update(_build_heartbeat_skill_env(config, user_id))
            except Exception as e:
                logger.warning(
                    "heartbeat skill-env resolution failed for user=%s: %s",
                    user_id, e,
                )
        # `shell_argv` rather than `shell=True` (`/bin/sh -c`, dash, no
        # `pipefail`): a probe ending in a pipe reported its last stage, so a
        # check whose real command failed read as healthy indefinitely — the
        # exact condition a heartbeat exists to detect.
        result = subprocess.run(
            shell_argv(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        value = result.stdout.strip()
    except subprocess.TimeoutExpired:
        return CheckResult(healthy=False, message=f"Command timed out after {timeout}s")
    except Exception as e:
        return CheckResult(healthy=False, message=f"Command error: {e}")

    if not condition:
        # No condition: healthy if exit code is 0. But: istota-skill CLIs
        # (feeds, money, health, location, …) emit
        # ``{"status":"error","error":"…"}`` on stdout and exit 0 when they
        # catch their own errors — so a heartbeat like
        # ``istota-skill location current`` whose setup_env hook didn't fire
        # would look healthy without this check. Same defense-in-depth that
        # ``scheduler._execute_command_task`` applies for command-tasks.
        healthy = result.returncode == 0
        if healthy and value.startswith("{"):
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                parsed = None
            if isinstance(parsed, dict) and parsed.get("status") == "error":
                err_msg = parsed.get("error") or "skill reported status=error"
                return CheckResult(
                    healthy=False,
                    message=f"Command returned error envelope: {err_msg}",
                    details={"value": value, "returncode": result.returncode},
                )
        if healthy:
            message = "Command succeeded"
        else:
            message = f"Command failed (exit {result.returncode})"
            if result.returncode == SIGPIPE_EXIT:
                # This message is what reaches the operator in an alert, and a
                # SIGPIPE'd producer writes nothing to stderr to go with it. A
                # bare "exit 141" on a probe that was correct is the kind of
                # thing someone debugs from scratch at 3am.
                message = f"{message}. {SIGPIPE_NOTE}"
        return CheckResult(
            healthy=healthy,
            message=message,
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


    # Same hardening, same directory (ISSUE-339). A refusal is reported as a
    # failed check rather than as "no file": a planted inode at TASKS.md is a
    # condition an operator should see, and reporting healthy would hide it.
    from .storage import read_user_config_file  # noqa: PLC0415 - import cycle

    content = read_user_config_file(config, user_id, "TASKS.md")
    if content is None:
        return CheckResult(healthy=False, message="Error reading TASKS.md")
    if not content:
        return CheckResult(healthy=True, message="No TASKS.md file")

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


#: Registry checks the heartbeat does not run, each with the reason it is out.
#: Follows `scheduler.SWEEP_SKIPPED_CHECKS`' precedent as a module constant with
#: its reasons written beside the entries rather than in a commit message.
#:
#: What every entry has in common is a cost that multiplies by something the
#: heartbeat multiplies by. `self-check` is per user *and* per check definition,
#: on a cadence each user chooses in their own `HEARTBEAT.md`, and it is not
#: admin-gated — `run_check` gates only `shell-command`. So a check costing one
#: spawn per configured user, or reaching past a `--version`, is a check this
#: path must not run.
#:
#: **What this list is not for, now that the thread question is settled.** The
#: spec that unified this handler onto the registry argued the cost was already
#: paid, because `scheduler.check_doctor` runs the same registry hourly with
#: `probe=True`. That was true of the *work* and false of the *thread*, and the
#: gap was real: `check_heartbeats` ran synchronously on the dispatch loop,
#: holding one write transaction for the whole sweep, so this handler blocked
#: dispatch for as long as the registry took, once per user. Both halves are
#: fixed where they belonged rather than here — the sweep is spawned through
#: `_spawn_background_check` and commits per check — so `web.basemap`
#: (network), `runtime.mount_liveness` (a FUSE stat) and `config.skill_overlays`
#: (a mount walk) are deliberately **not** skipped: they are slow, and slow is
#: no longer the thing that hurts. What still belongs here is a check whose cost
#: is paid per *user* when the answer is deployment-wide, or one that owns its
#: own cadence elsewhere.
_SELF_CHECK_SKIPPED = (
    # A `PRAGMA quick_check` over the whole framework database. `check_db_health`
    # owns that on a daily cadence, and `SWEEP_SKIPPED_CHECKS` excludes it from
    # the hourly sweep for the same reason — more strongly here, since a
    # heartbeat can be configured to run more often than hourly.
    "runtime.framework_db",
    # An HTTPS GET behind its own TTL and its own deployment-wide disk cache.
    # Its cadence is not this one's to override.
    "runtime.subscription_usage",
    # One exec-transport socket connection per configured user.
    "developer.container",
    # Spawns `iptables`.
    "security.devbox_netfilter",
)


def _check_self(check: HeartbeatCheck, config: "Config", user_id: str) -> CheckResult:
    """Run the doctor registry and report its verdict.

    Config fields:
        execution_test: whether to run the live model invocation (default: True)

    This used to be a hand-rolled copy of `commands.cmd_check` — the same five
    probes in the same order, drifting from doctor's registry and from the other
    copy. All five are registry checks now, so this handler selects and renders
    and asserts nothing of its own.

    Four things about the call are decisions rather than defaults:

    ``execution_test`` selects ``live``, not ``deep``. It gates exactly one
    thing today — the live model invocation — and ``live`` is exactly one thing.
    Mapping it to ``deep`` would have widened ``execution_test = false`` from
    "spawn nothing" to "run the whole registry", and ``true`` to "also build a
    namespace". The key keeps its name, its default and its meaning.

    ``deep`` is therefore never passed. A namespace spawn on a per-user,
    per-check cadence multiplies by both; ``security.sandbox_effective`` answers
    the availability question from the warm memo at no cost, which is what this
    path actually needs.

    ``probe`` stays True. ``probe=False`` would map neatly onto the old
    ``shutil.which`` calls, but it would make ``live=True`` self-contradictory.
    :data:`_SELF_CHECK_SKIPPED` names the ones whose cost multiplies badly
    instead. The sweep that calls this runs on a background thread and commits
    per check, so a slow check here costs its own wall time and nothing else;
    that was not true when this handler was written, and the constant's first
    paragraph records what changed.

    And it redacts before anything leaves. `scheduler` and `web_app` both do;
    this path delivers to a user and had no reason to be the exception.

    **An admin is told which checks failed; everyone else gets the count.** The
    same disclosure boundary `commands.cmd_check` draws, drawn here for the same
    reason and by the same mechanism, because this handler reaches the same
    audience: ``run_check`` admin-gates only ``shell-command``, and
    ``check_heartbeats`` runs each user's own ``HEARTBEAT.md`` and delivers the
    message to that user. A registry ``detail`` is not a per-user fact —
    ``config.skill_overlays`` labels overlays ``{user_id}/{filename}`` across
    every user's tree, ``developer.repos_layout`` names the namespaces filed on
    disk, and ``runtime.model_execution`` names the admin it probed as. An
    allowlist of safe check names is rejected here for the reason the spec
    rejects it there: a second list to keep in step with a registry that grows,
    where a check whose detail widens later leaks with nothing going red.

    Naming the failures is what makes an admin's alert actionable, and a count
    alone would be a regression on what this reported before — so the narrowing
    is only for the audience that could not act on the names anyway.

    ``user_id`` is now read only for that gate. The probe itself resolves the
    deployment's own user (``doctor._probe_user``), which is a deliberate
    narrowing recorded there; it is not a per-user knob to restore.
    """
    from . import doctor

    try:
        results = doctor.redact(
            doctor.run_checks(
                config,
                live=bool(check.config.get("execution_test", True)),
                skip=_SELF_CHECK_SKIPPED,
            ),
            config,
        )
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not page a user
        # Both scheduler callers wrap this same pair and return no results
        # (`run_startup_checks`, `check_doctor`), because `run_checks` is not
        # exception-proof end to end: a check returning a non-iterable escapes
        # the per-check `try`, and `redact` is outside it entirely. `run_check`'s
        # blanket handler would catch this, but it renders as an unhealthy
        # result, so a defect in doctor would alert every user with a
        # `self-check` rather than being logged for an operator.
        logger.error("heartbeat self-check could not run doctor: %s", exc, exc_info=True)
        return CheckResult(
            healthy=True,
            message="the health registry could not be run; see the daemon log",
            details={"failures": [], "summary": "registry unavailable"},
        )

    healthy, summary = doctor.verdict(results)
    failures = doctor.failing(results)
    if config.is_admin(user_id):
        message = "; ".join(f"{r.name}: {r.detail}" for r in failures) or summary
    else:
        message = summary
    return CheckResult(
        healthy=healthy,
        message=message,
        # Not delivered to the user — nothing in `heartbeat` or `scheduler`
        # reads it — so the names are safe here whatever the gate above said.
        details={"failures": [r.name for r in failures], "summary": summary},
        # Sorted, and the names alone: `run_checks` walks the registry in
        # declaration order, so a check inserted between two failing ones would
        # otherwise change the signature and page every user about a failure set
        # that had not changed. The `detail` is deliberately excluded for the
        # same reason — several carry a count or a path that moves on its own.
        # Empty stays None rather than "": a healthy run has nothing to
        # suppress, and `should_alert` returns False for it long before this.
        alert_signature="|".join(sorted(r.name for r in failures)) or None,
    )


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

    # _check_shell_command pulls build_stripped_env(); non-admins must
    # not be able to run arbitrary shell.
    if check.type == "shell-command" and not config.is_admin(user_id):
        return CheckResult(
            healthy=False,
            message="shell-command checks are admin-only",
        )

    try:
        # Some handlers need user_id (calendar, task-deadline, self-check,
        # shell-command — the latter so subprocesses spawned from the check
        # can resolve the user's context the same way scheduler tasks do).
        if check.type in ("calendar-conflicts", "task-deadline", "self-check", "shell-command"):
            return handler(check, config, user_id)
        else:
            return handler(check, config)
    except Exception as e:
        logger.exception("Error running check %s for user %s", check.name, user_id)
        return CheckResult(
            healthy=False,
            message=f"Check error: {e}",
        )


#: How long an unchanged failure set stays suppressed before it is reported
#: again. Deliberately far longer than any `default_cooldown_minutes` and still
#: finite: see the gate in `should_alert` for the two cases that need the floor.
MAX_SIGNATURE_SUPPRESSION_HOURS = 24


def _within_suppression_window(last_alert_at: str | None) -> bool:
    """Whether the recorded alert is recent enough to still suppress a repeat.

    An unparseable or absent timestamp reads as *outside* the window, so the
    alert goes out. Failing towards a duplicate page rather than towards
    silence is the only safe direction for a gate whose whole job is to
    withhold notifications.
    """
    if not last_alert_at:
        return False
    try:
        last = datetime.fromisoformat(last_alert_at)
    except (ValueError, TypeError):
        return False
    elapsed = (datetime.now(ZoneInfo("UTC")).replace(tzinfo=None) - last).total_seconds()
    return elapsed < MAX_SIGNATURE_SUPPRESSION_HOURS * 3600


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
    - The failure set is unchanged since the last delivered alert and that
      alert is within `MAX_SIGNATURE_SUPPRESSION_HOURS` (`alert_signature`)
    - Within cooldown period
    - Within quiet hours
    """
    if result.healthy:
        return False

    # A failure that has not changed since the last alert is not news. Only a
    # check that named what it is failing on gets to make that claim; every
    # other type leaves `alert_signature` None and keeps the cooldown-only
    # behaviour below.
    #
    # Bounded by `MAX_SIGNATURE_SUPPRESSION_HOURS`, which is what makes this a
    # rate limit rather than an off switch. The signature is the failing
    # *names*, so a condition that is getting worse without changing which
    # checks fail — a disk at 20% and the same disk at 0.5% — produces the same
    # string; and `_dispatch` reports success when any one destination
    # succeeded, so a user routed to two surfaces whose second leg failed would
    # otherwise never see a retry. Neither is worth a page every cooldown, and
    # both are worth one a day. The window is cleared outright on recovery, so
    # this only ever governs a failure that is still standing.
    if result.alert_signature is not None:
        state = db.get_heartbeat_state(conn, user_id, check.name)
        if state and state.last_alert_signature == result.alert_signature:
            if _within_suppression_window(state.last_alert_at):
                logger.debug(
                    "Skipping alert for %s/%s: unchanged failure set (%s)",
                    user_id, check.name, result.alert_signature,
                )
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


def effective_alert_surface(config: "Config", user_id: str, check: HeartbeatCheck) -> str:
    """The surface a heartbeat alert delivers to: the check's explicit ``channel``
    if it set one, else the user's ``alert`` routing default (then bare talk).

    A check with no ``channel`` defers to the per-user routing table, so
    ``routing={"alert": "ntfy"}`` reroutes every otherwise-default check.
    """
    if check.channel:
        return check.channel
    from .notifications import surface_for_purpose
    return surface_for_purpose(config, user_id, "alert")


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
        surface=effective_alert_surface(config, user_id, check),
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
        # Live DB timezone (reusing conn) so quiet-hours track travel without
        # a daemon restart (ISSUE-099).
        user_tz = config.resolve_user_timezone(user_id, conn=conn)

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
            # Commit each write as it is made, rather than once at the end of
            # the sweep. `get_db` is in Python's legacy implicit-transaction
            # mode, so this write opens the daemon's single SQLite write
            # transaction and — with no commit before the loop's next
            # iteration — held it across every remaining check, `_check_self`'s
            # whole doctor registry among them: process spawns, a socket per
            # configured service and an optional live model call. That was
            # tolerable only while the sweep ran on the dispatch loop and
            # nothing else could be writing; off the loop it becomes lock
            # contention against the loop and the workers, who then wait out
            # `get_db`'s busy timeout. It also has to be released before
            # `send_heartbeat_alert` below, which opens its own connection to
            # this database on the web and Talk legs — a second connection
            # under a held write lock is the busy-timeout deadlock
            # `notification_store` documents, and it would land on the delivery
            # path of the alert this sweep exists to send.
            #
            # Nothing here needs sweep-wide atomicity: each row is one check's
            # own state. The gain is also durability — the sweep runs on a
            # daemon thread now, which a shutdown kills without joining, and an
            # uncommitted `last_alert_at` for an alert already delivered means
            # a duplicate page on the next start.
            conn.commit()

            if check_result.healthy:
                db.update_heartbeat_state(
                    conn, user_id, check.name,
                    last_healthy_at=True,
                    reset_errors=True,
                    # Recovery forgets what the last alert was about, so the
                    # same failure recurring next month is news again rather
                    # than being suppressed for ever by a stale signature.
                    clear_alert_signature=True,
                )
                conn.commit()
            else:
                # Check if we should alert
                if should_alert(conn, user_id, check, check_result, settings, user_tz):
                    # Distinguish "user hasn't configured this channel" from
                    # "delivery failed". Bumping consecutive_errors for an
                    # unconfigured channel turns a config gap into a fake
                    # alert-pipeline outage; we log instead and move on.
                    from .notifications import is_channel_configured

                    alert_surface = effective_alert_surface(config, user_id, check)
                    if not is_channel_configured(
                        config, user_id, alert_surface,
                        conversation_token=settings.conversation_token,
                    ):
                        logger.warning(
                            "heartbeat %r for user %s: channel %r not configured "
                            "— alert skipped (configure via /istota/settings)",
                            check.name, user_id, alert_surface,
                        )
                        continue

                    sent = send_heartbeat_alert(config, user_id, check, check_result, settings)
                    if sent:
                        db.update_heartbeat_state(
                            conn, user_id, check.name,
                            last_alert_at=True,
                            # Only a *delivered* alert records its signature.
                            # Recording one for a send that reached nobody would
                            # suppress every later page for the same condition —
                            # `notification_store.last_delivered_at` draws the
                            # same line for the same reason.
                            last_alert_signature=check_result.alert_signature,
                        )
                    else:
                        db.update_heartbeat_state(
                            conn, user_id, check.name,
                            last_error_at=True,
                            increment_errors=True,
                        )
                    conn.commit()

    return checked_users
