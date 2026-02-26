"""!command dispatch system — synchronous commands intercepted before task queue."""

import json
import logging
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import db
from .config import Config
from .talk import TalkClient, clean_message_content, split_message

logger = logging.getLogger("istota.commands")

# Type for command handlers
# Args: (config, conn, user_id, conversation_token, args_str, talk_client)
# Returns: response message string (posted to Talk by dispatcher)
CommandHandler = Callable[
    [Config, sqlite3.Connection, str, str, str, TalkClient],
    Awaitable[str],
]

# Command registry: name -> (handler, help_text)
COMMANDS: dict[str, tuple[CommandHandler, str]] = {}


def command(name: str, help_text: str):
    """Decorator to register a command handler."""

    def decorator(func: CommandHandler):
        COMMANDS[name] = (func, help_text)
        return func

    return decorator


def parse_command(content: str) -> tuple[str, str] | None:
    """Parse a !command message. Returns (command_name, args_str) or None."""
    content = content.strip()
    if not content.startswith("!"):
        return None
    match = re.match(r"^!(\w+)\s*(.*)", content, re.DOTALL)
    if not match:
        return None
    return (match.group(1).lower(), match.group(2).strip())


async def dispatch(
    config: Config,
    conn: sqlite3.Connection,
    user_id: str,
    conversation_token: str,
    content: str,
) -> bool:
    """
    Try to dispatch content as a !command.
    Returns True if handled (command executed or error posted), False if not a command.
    """
    parsed = parse_command(content)
    if parsed is None:
        return False

    cmd_name, args_str = parsed
    client = TalkClient(config)

    if cmd_name not in COMMANDS:
        await client.send_message(
            conversation_token,
            f"Unknown command `!{cmd_name}`. Type `!help` for available commands.",
        )
        return True

    handler, _ = COMMANDS[cmd_name]
    try:
        response = await handler(config, conn, user_id, conversation_token, args_str, client)
        if response:
            for part in split_message(response):
                await client.send_message(conversation_token, part)
    except Exception as e:
        logger.error("Command !%s failed: %s", cmd_name, e, exc_info=True)
        await client.send_message(
            conversation_token,
            f"Command `!{cmd_name}` failed: {e}",
        )

    return True


# =============================================================================
# Command implementations
# =============================================================================


@command("help", "List available commands")
async def cmd_help(config, conn, user_id, conversation_token, args, client):
    lines = ["**Available commands:**", ""]
    for name, (_, help_text) in sorted(COMMANDS.items()):
        lines.append(f"- `!{name}` -- {help_text}")
    return "\n".join(lines)


@command("stop", "Cancel your currently running task")
async def cmd_stop(config, conn, user_id, conversation_token, args, client):
    cursor = conn.execute(
        """
        SELECT id, prompt FROM tasks
        WHERE user_id = ? AND status IN ('running', 'locked', 'pending_confirmation')
        ORDER BY created_at DESC LIMIT 1
        """,
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        return "No active task to cancel."

    task_id, prompt = row["id"], row["prompt"]

    # Set cancellation flag
    conn.execute(
        "UPDATE tasks SET cancel_requested = 1 WHERE id = ?",
        (task_id,),
    )
    conn.commit()

    # Also try to kill subprocess if PID is stored
    pid_row = conn.execute(
        "SELECT worker_pid FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if pid_row and pid_row["worker_pid"]:
        try:
            import os
            import signal

            os.kill(pid_row["worker_pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass

    preview = prompt[:80] + "..." if len(prompt) > 80 else prompt
    return f"Cancelling task #{task_id}: {preview}"


@command("status", "Show your running/pending tasks and system status")
async def cmd_status(config, conn, user_id, conversation_token, args, client):
    rows = conn.execute(
        """
        SELECT id, status, prompt, created_at, source_type FROM tasks
        WHERE user_id = ? AND status IN ('pending', 'locked', 'running', 'pending_confirmation')
        ORDER BY created_at ASC
        """,
        (user_id,),
    ).fetchall()

    _interactive_types = {"talk", "email", "cli"}
    interactive = [r for r in rows if r["source_type"] in _interactive_types]
    background = [r for r in rows if r["source_type"] not in _interactive_types]

    status_emoji = {
        "pending": "...",
        "locked": "[locked]",
        "running": "[running]",
        "pending_confirmation": "[confirm?]",
    }

    def _format_row(row):
        preview = row["prompt"][:60] + "..." if len(row["prompt"]) > 60 else row["prompt"]
        emoji = status_emoji.get(row["status"], "-")
        return f"- {emoji} #{row['id']} {preview}"

    lines = []
    if not rows:
        lines.append("No active or pending tasks.")
    else:
        if interactive:
            lines.append(f"**Your tasks ({len(interactive)}):**")
            lines.append("")
            for row in interactive:
                lines.append(_format_row(row))
        if background:
            if interactive:
                lines.append("")
            lines.append(f"**Background ({len(background)}):**")
            lines.append("")
            for row in background:
                tag = f"[{row['source_type']}] " if row["source_type"] != "scheduled" else "[scheduled] "
                preview = row["prompt"][:50] + "..." if len(row["prompt"]) > 50 else row["prompt"]
                emoji = status_emoji.get(row["status"], "-")
                lines.append(f"- {emoji} #{row['id']} {tag}{preview}")
        if not interactive and not background:
            lines.append("No active or pending tasks.")

    total_running = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
    ).fetchone()[0]
    total_pending = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'pending'"
    ).fetchone()[0]

    lines.append("")
    lines.append(f"**System:** {total_running} running, {total_pending} queued")

    return "\n".join(lines)


@command("memory", "Show your user or channel memory: `!memory user` or `!memory channel`")
async def cmd_memory(config, conn, user_id, conversation_token, args, client):
    mount = config.nextcloud_mount_path
    if mount is None:
        return "Nextcloud mount not configured -- cannot read memory files."

    target = args.strip().lower()

    if target == "user":
        mem_path = mount / "Users" / user_id / config.bot_dir_name / "config" / "USER.md"
        if mem_path.exists():
            content = mem_path.read_text()
            if content.strip():
                return f"**User memory** ({len(content)} chars):\n\n{content}"
        return "**User memory:** (empty)"

    if target == "channel":
        mem_path = mount / "Channels" / conversation_token / "CHANNEL.md"
        if mem_path.exists():
            content = mem_path.read_text()
            if content.strip():
                return f"**Channel memory** ({len(content)} chars):\n\n{content}"
        return "**Channel memory:** (empty)"

    return "Usage: `!memory user` or `!memory channel`"


@command("cron", "List/enable/disable scheduled jobs: `!cron`, `!cron enable <name>`, `!cron disable <name>`")
async def cmd_cron(config, conn, user_id, conversation_token, args, client):
    from .cron_loader import update_job_enabled_in_cron_md

    parts = args.strip().split(maxsplit=1)
    subcmd = parts[0].lower() if parts else ""
    job_name = parts[1].strip() if len(parts) > 1 else ""

    if subcmd == "enable" and job_name:
        job = db.get_scheduled_job_by_name(conn, user_id, job_name)
        if not job:
            return f"No scheduled job named '{job_name}' found."
        # Write to CRON.md (source of truth); DB updated on next sync
        if update_job_enabled_in_cron_md(config, user_id, job_name, True):
            db.enable_scheduled_job(conn, job.id)
            return f"Enabled scheduled job '{job_name}' (failure count reset)."
        # Fallback: no CRON.md file, update DB directly
        db.enable_scheduled_job(conn, job.id)
        return f"Enabled scheduled job '{job_name}' (failure count reset). Note: no CRON.md file found — change is DB-only and may not persist."

    if subcmd == "disable" and job_name:
        job = db.get_scheduled_job_by_name(conn, user_id, job_name)
        if not job:
            return f"No scheduled job named '{job_name}' found."
        # Write to CRON.md (source of truth); DB updated on next sync
        if update_job_enabled_in_cron_md(config, user_id, job_name, False):
            db.disable_scheduled_job(conn, job.id)
            return f"Disabled scheduled job '{job_name}'."
        # Fallback: no CRON.md file, update DB directly
        db.disable_scheduled_job(conn, job.id)
        return f"Disabled scheduled job '{job_name}'. Note: no CRON.md file found — change is DB-only and may not persist."

    # Default: list all jobs
    jobs = db.get_user_scheduled_jobs(conn, user_id)
    if not jobs:
        return "No scheduled jobs configured."

    lines = [f"**Scheduled jobs ({len(jobs)}):**", ""]
    for job in jobs:
        status = "enabled" if job.enabled else "DISABLED"
        kind = " (cmd)" if job.command else ""
        line = f"- **{job.name}**{kind} `{job.cron_expression}` [{status}]"
        if job.last_run_at:
            line += f" (last: {job.last_run_at[:16]})"
        if job.consecutive_failures > 0:
            line += f" **{job.consecutive_failures} failures**"
        lines.append(line)

    return "\n".join(lines)


@command("check", "Run Claude Code health check")
async def cmd_check(config, conn, user_id, conversation_token, args, client):
    from .executor import build_bwrap_cmd, build_clean_env

    lines = ["**Health Check**", ""]

    # 1. Claude binary
    claude_path = shutil.which("claude")
    if claude_path:
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True, text=True, timeout=2,
            )
            version = result.stdout.strip() or result.stderr.strip()
            lines.append(f"- Claude binary: PASS ({version})")
        except Exception as e:
            lines.append(f"- Claude binary: PASS (found at {claude_path}, version check failed: {e})")
    else:
        lines.append("- Claude binary: **FAIL** (not found in PATH)")

    # 2. Sandbox (bwrap)
    if config.security.sandbox_enabled:
        bwrap_path = shutil.which("bwrap")
        if bwrap_path:
            try:
                result = subprocess.run(
                    ["bwrap", "--version"],
                    capture_output=True, text=True, timeout=2,
                )
                version = result.stdout.strip() or result.stderr.strip()
                lines.append(f"- Sandbox (bwrap): PASS ({version})")
            except Exception as e:
                lines.append(f"- Sandbox (bwrap): **FAIL** (found but version check failed: {e})")
        else:
            lines.append("- Sandbox (bwrap): **FAIL** (not found in PATH)")
    else:
        lines.append("- Sandbox: skipped (not enabled)")

    # 3. DB health
    try:
        row = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
        lines.append(f"- Database: PASS ({row[0]} total tasks)")
    except Exception as e:
        lines.append(f"- Database: **FAIL** ({e})")

    # 4. Recent task stats (last hour)
    try:
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
        stat_line = f"- Recent tasks (1h): {completed} completed, {failed} failed"
        if failed > 0 and failed >= completed:
            stat_line += " **[warning: high failure rate]**"
        lines.append(stat_line)
    except Exception as e:
        lines.append(f"- Recent tasks: **FAIL** ({e})")

    # 5. Claude execution check (actual invocation)
    lines.append("")
    lines.append("**Execution test:**")
    try:
        cmd = [
            "claude", "-p", "Run: echo healthcheck-ok",
            "--allowedTools", "Bash",
            "--output-format", "text",
        ]

        env = build_clean_env(config)
        # Inherit API key from current environment if not already in env
        if not env.get("ANTHROPIC_API_KEY"):
            import os
            val = os.environ.get("ANTHROPIC_API_KEY")
            if val:
                env["ANTHROPIC_API_KEY"] = val

        # Wrap in sandbox if enabled
        if config.security.sandbox_enabled:
            fake_task = db.Task(
                id=0, status="running", source_type="cli",
                user_id=user_id, prompt="healthcheck",
                conversation_token=conversation_token,
            )
            user_resources = db.get_user_resources(conn, user_id)
            user_temp = config.temp_dir / user_id
            user_temp.mkdir(parents=True, exist_ok=True)
            is_admin = config.is_admin(user_id)
            cmd = build_bwrap_cmd(cmd, config, fake_task, is_admin, user_resources, user_temp)

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, env=env,
        )
        output = result.stdout.strip()
        if "healthcheck-ok" in output:
            lines.append("- Claude + Bash: PASS")
        else:
            stderr_preview = (result.stderr.strip()[:200]) if result.stderr else ""
            stdout_preview = output[:200] if output else "(empty)"
            lines.append(f"- Claude + Bash: **FAIL** (expected 'healthcheck-ok')")
            if stderr_preview:
                lines.append(f"  stderr: {stderr_preview}")
            else:
                lines.append(f"  stdout: {stdout_preview}")
    except subprocess.TimeoutExpired:
        lines.append("- Claude + Bash: **FAIL** (timed out after 30s)")
    except Exception as e:
        lines.append(f"- Claude + Bash: **FAIL** ({e})")

    return "\n".join(lines)


def _read_claude_oauth_token() -> str | None:
    """Read the OAuth access token from Claude's credentials file (~/.claude/.credentials.json)."""
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if not creds_path.exists():
        return None
    try:
        with open(creds_path) as f:
            creds = json.load(f)
        oauth = creds.get("claudeAiOauth", {})
        return oauth.get("accessToken")
    except Exception:
        logger.exception("Failed to read Claude credentials from %s", creds_path)
        return None


def _format_utilization(label: str, bucket: dict, tz=None) -> str:
    """Format a usage bucket (five_hour, seven_day, etc.) into a readable line."""
    from datetime import datetime

    pct = bucket.get("utilization", 0)  # API returns 0-100 percentage
    resets_at = bucket.get("resets_at")

    bar_len = 20
    filled = min(round(pct / 100 * bar_len), bar_len)
    bar = "#" * filled + "-" * (bar_len - filled)

    line = f"- {label}: [{bar}] {pct:.0f}%"
    if resets_at:
        try:
            dt = datetime.fromisoformat(resets_at)
            if tz:
                dt = dt.astimezone(tz)
            line += f" (resets {dt.strftime('%b %-d %-H:%M')})"
        except Exception:
            line += f" (resets {resets_at})"
    return line


@command("usage", "Show Claude API usage limits")
async def cmd_usage(config, conn, user_id, conversation_token, args, client):
    """Query Anthropic OAuth usage endpoint for current utilization."""
    token = _read_claude_oauth_token()
    if not token:
        creds_path = Path.home() / ".claude" / ".credentials.json"
        return f"**Error:** Could not read OAuth token from {creds_path}"

    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
    }

    # Resolve user timezone
    from zoneinfo import ZoneInfo

    user_config = config.get_user(user_id)
    tz_str = user_config.timezone if user_config else "UTC"
    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        tz = None

    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            response = await http.get(
                "https://api.anthropic.com/api/oauth/usage",
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        lines = ["**Claude Code Usage**", ""]

        # Rate-limit buckets (have utilization + resets_at)
        for key, value in data.items():
            if key == "extra_usage" or not isinstance(value, dict):
                continue
            if "utilization" not in value:
                continue
            label = key.replace("_", " ").replace("seven day", "7-day").replace("five hour", "5-hour")
            lines.append(_format_utilization(label, value, tz=tz))

        # Extra usage (pay-as-you-go overflow)
        extra = data.get("extra_usage")
        if isinstance(extra, dict) and extra.get("is_enabled"):
            used = extra.get("used_credits", 0) / 100
            limit = extra.get("monthly_limit", 0) / 100
            pct = extra.get("utilization", 0)
            lines.append("")
            lines.append(f"**Extra usage:** ${used:.2f} / ${limit:.2f} ({pct:.0f}%)")

        if len(lines) == 2:
            lines.append("No usage data returned.")

        return "\n".join(lines)

    except httpx.HTTPStatusError as e:
        logger.error("Usage API request failed: %s", e)
        return f"**Error:** API request failed ({e.response.status_code})"
    except Exception as e:
        logger.error("Failed to fetch usage: %s", e, exc_info=True)
        return f"**Error:** {e}"


# =============================================================================
# !export command
# =============================================================================

_EXPORT_META_RE = re.compile(
    r"^(?:<!--|#)\s*export:token=([^,]+),last_id=(\d+),updated=([^\s>]+)"
)


def _parse_export_metadata(first_line: str) -> dict | None:
    """Parse metadata from the first line of an export file."""
    m = _EXPORT_META_RE.match(first_line.strip())
    if not m:
        return None
    return {
        "token": m.group(1),
        "last_id": int(m.group(2)),
        "updated": m.group(3),
    }


def _build_export_metadata(token: str, last_id: int, fmt: str) -> str:
    """Build the metadata header line."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if fmt == "markdown":
        return f"<!-- export:token={token},last_id={last_id},updated={ts} -->"
    return f"# export:token={token},last_id={last_id},updated={ts}"


def _format_timestamp(epoch: int, tz=None) -> str:
    """Format a Unix epoch timestamp to a readable string."""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    if tz:
        dt = dt.astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M")


def _format_messages_markdown(messages: list[dict], tz=None) -> str:
    """Format messages as markdown with coalescing."""
    lines: list[str] = []
    prev_actor: str | None = None

    for msg in messages:
        actor = msg.get("actorDisplayName") or msg.get("actorId", "Unknown")
        content = clean_message_content(msg)
        timestamp = msg.get("timestamp", 0)

        if actor == prev_actor:
            # Coalesce: just append content under same header
            lines.append("")
            lines.append(content)
        else:
            # New actor group
            if prev_actor is not None:
                lines.append("")
                lines.append("---")
            lines.append("")
            lines.append(f"**{actor}** — {_format_timestamp(timestamp, tz)}")
            lines.append(content)
            prev_actor = actor

    # Final separator
    if lines:
        lines.append("")
        lines.append("---")

    return "\n".join(lines)


def _format_messages_text(messages: list[dict], tz=None) -> str:
    """Format messages as plaintext with coalescing."""
    lines: list[str] = []
    prev_actor: str | None = None

    for msg in messages:
        actor = msg.get("actorDisplayName") or msg.get("actorId", "Unknown")
        content = clean_message_content(msg)
        timestamp = msg.get("timestamp", 0)

        if actor == prev_actor:
            lines.append("")
            lines.append(content)
        else:
            if prev_actor is not None:
                lines.append("")
            lines.append(f"{actor} — {_format_timestamp(timestamp, tz)}")
            lines.append(content)
            prev_actor = actor

    return "\n".join(lines)


def _filter_user_messages(messages: list[dict]) -> list[dict]:
    """Filter to only user/bot comment messages (skip system messages)."""
    return [
        m for m in messages
        if m.get("actorType") == "users"
        and m.get("messageType") == "comment"
    ]


@command("export", "Export conversation history to a file: `!export [markdown|text]`")
async def cmd_export(config, conn, user_id, conversation_token, args, client):
    mount = config.nextcloud_mount_path
    if mount is None:
        return "Nextcloud mount not configured — cannot write export file."

    # Parse format
    fmt_arg = args.strip().lower()
    if fmt_arg in ("text", "txt", "plaintext"):
        fmt = "text"
        ext = ".txt"
    else:
        fmt = "markdown"
        ext = ".md"

    # Build export path
    export_dir = mount / "Users" / user_id / config.bot_dir_name / "exports" / "conversations"
    export_dir.mkdir(parents=True, exist_ok=True)
    export_path = export_dir / f"{conversation_token}{ext}"

    # Resolve user timezone
    from zoneinfo import ZoneInfo

    user_config = config.get_user(user_id)
    tz_str = user_config.timezone if user_config else "UTC"
    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        tz = None

    # Check for existing export
    existing_meta = None
    if export_path.exists():
        try:
            first_line = export_path.read_text().split("\n", 1)[0]
            existing_meta = _parse_export_metadata(first_line)
        except Exception:
            pass

    if existing_meta and existing_meta["token"] == conversation_token:
        # Incremental export
        since_id = existing_meta["last_id"]
        new_messages = await client.fetch_messages_since(conversation_token, since_id)
        user_messages = _filter_user_messages(new_messages)

        if not user_messages:
            return "No new messages since last export."

        last_id = user_messages[-1]["id"]

        # Format new messages
        if fmt == "markdown":
            new_content = _format_messages_markdown(user_messages, tz=tz)
        else:
            new_content = _format_messages_text(user_messages, tz=tz)

        # Read existing content, replace metadata line, append new messages
        existing_content = export_path.read_text()
        # Replace first line (metadata) with updated one
        rest = existing_content.split("\n", 1)[1] if "\n" in existing_content else ""
        new_meta = _build_export_metadata(conversation_token, last_id, fmt)
        export_path.write_text(new_meta + "\n" + rest.rstrip("\n") + "\n" + new_content + "\n")

        rel_path = f"/{export_path.relative_to(mount)}"
        return f"Appended {len(user_messages)} new messages to `{rel_path}`"

    else:
        # Full export
        all_messages = await client.fetch_full_history(conversation_token)
        user_messages = _filter_user_messages(all_messages)

        if not user_messages:
            return "No messages to export."

        last_id = user_messages[-1]["id"]

        # Get conversation info for frontmatter
        try:
            room_info = await client.get_conversation_info(conversation_token)
            title = room_info.get("displayName", conversation_token)
        except Exception:
            title = conversation_token

        try:
            participants = await client.get_participants(conversation_token)
            participant_names = sorted(
                p.get("displayName") or p.get("actorId", "")
                for p in participants
                if p.get("actorType") == "users"
            )
        except Exception:
            participant_names = []

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        if tz:
            now_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M")

        meta_line = _build_export_metadata(conversation_token, last_id, fmt)

        if fmt == "markdown":
            header_parts = [
                meta_line,
                "",
                f"# {title}",
                "",
                f"**Exported:** {now_str}",
            ]
            if participant_names:
                header_parts.append(f"**Participants:** {', '.join(participant_names)}")
            header_parts.append("")
            header_parts.append("---")
            body = _format_messages_markdown(user_messages, tz=tz)
        else:
            header_parts = [
                meta_line,
                "",
                title,
                f"Exported: {now_str}",
            ]
            if participant_names:
                header_parts.append(f"Participants: {', '.join(participant_names)}")
            header_parts.append("=" * 40)
            body = _format_messages_text(user_messages, tz=tz)

        content = "\n".join(header_parts) + "\n" + body + "\n"
        export_path.write_text(content)

        rel_path = f"/{export_path.relative_to(mount)}"
        return f"Exported {len(user_messages)} messages to `{rel_path}`"
