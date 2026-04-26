"""!command dispatch system — synchronous commands intercepted before task queue."""

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


from . import db
from . import memory_search as memory_search_mod
from .config import Config
from .nextcloud_client import ocs_get
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

    if config.is_admin(user_id):
        total_running = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
        ).fetchone()[0]
        total_pending = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'pending'"
        ).fetchone()[0]
        lines.append("")
        lines.append(f"**System:** {total_running} running, {total_pending} queued")

    return "\n".join(lines)


@command("memory", "Show memory: `!memory user`, `!memory channel`, `!memory facts`")
async def cmd_memory(config, conn, user_id, conversation_token, args, client):
    mount = config.nextcloud_mount_path
    target = args.strip().lower()

    if target == "user":
        if mount is None:
            return "Nextcloud mount not configured -- cannot read memory files."
        mem_path = mount / "Users" / user_id / config.bot_dir_name / "config" / "USER.md"
        if mem_path.exists():
            content = mem_path.read_text()
            if content.strip():
                return f"**User memory** ({len(content)} chars):\n\n{content}"
        return "**User memory:** (empty)"

    if target == "channel":
        if mount is None:
            return "Nextcloud mount not configured -- cannot read memory files."
        from .storage import validate_conversation_token
        validate_conversation_token(conversation_token)
        mem_path = mount / "Channels" / conversation_token / "CHANNEL.md"
        if mem_path.exists():
            content = mem_path.read_text()
            if content.strip():
                return f"**Channel memory** ({len(content)} chars):\n\n{content}"
        return "**Channel memory:** (empty)"

    if target == "facts":
        try:
            from .knowledge_graph import ensure_table, get_current_facts, get_fact_count, format_facts_for_prompt
            ensure_table(conn)
            counts = get_fact_count(conn, user_id)
            total = counts["current"]
            if total == 0:
                return "**Knowledge graph:** (no facts)"
            facts = get_current_facts(conn, user_id)
            text = format_facts_for_prompt(facts)
            if total <= 20:
                return f"**Knowledge graph** ({total} facts):\n\n{text}"
            # Summarize by subject for large fact sets
            subjects: dict[str, int] = {}
            for f in facts:
                subjects[f.subject] = subjects.get(f.subject, 0) + 1
            summary = ", ".join(f"{s} ({n})" for s, n in sorted(subjects.items(), key=lambda x: -x[1]))
            return (
                f"**Knowledge graph** ({total} facts across {len(subjects)} entities):\n\n"
                f"**Entities:** {summary}\n\n"
                f"Use `istota-skill memory_search facts` or `!memory facts <entity>` to query specific entities."
            )
        except Exception as e:
            return f"Error reading knowledge graph: {e}"

    if target.startswith("facts "):
        entity = target[6:].strip()
        if not entity:
            return "Usage: `!memory facts <entity>`"
        try:
            from .knowledge_graph import ensure_table, get_current_facts, format_facts_for_prompt
            ensure_table(conn)
            facts = get_current_facts(conn, user_id, subject=entity)
            if facts:
                text = format_facts_for_prompt(facts)
                return f"**Facts about {entity}** ({len(facts)}):\n\n{text}"
            return f"**Facts about {entity}:** (none found)"
        except Exception as e:
            return f"Error reading knowledge graph: {e}"

    return "Usage: `!memory user`, `!memory channel`, or `!memory facts`"


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
        if job.model:
            line += f" `model: {job.model}`"
        if job.effort:
            line += f" `effort: {job.effort}`"
        if job.last_run_at:
            line += f" (last: {job.last_run_at[:16]})"
        if job.consecutive_failures > 0:
            line += f" **{job.consecutive_failures} failures**"
        lines.append(line)

    return "\n".join(lines)


@command("skills", "List available skills and their triggers")
async def cmd_skills(config, conn, user_id, conversation_token, args, client):
    from .skills._loader import get_skill_availability, load_skill_index

    skills_dir = config.skills_dir
    bundled_dir = getattr(config, "bundled_skills_dir", None)
    index = load_skill_index(skills_dir, bundled_dir=bundled_dir)

    is_admin = config.is_admin(user_id)

    # Collect disabled skills (instance-wide + per-user)
    disabled = set(config.disabled_skills)
    user_config = config.get_user(user_id)
    if user_config:
        disabled |= set(user_config.disabled_skills)

    # Check for detail view: !skills <name>
    skill_arg = args.strip() if args else ""
    if skill_arg and skill_arg in index:
        return _format_skill_detail(index[skill_arg], skill_arg, disabled, is_admin)

    available = []
    unavailable = []
    disabled_skills = []

    for name in sorted(index):
        meta = index[name]
        if meta.admin_only and not is_admin:
            continue

        if name in disabled:
            disabled_skills.append((name, meta))
            continue

        status, missing_dep = get_skill_availability(meta)
        if status == "unavailable":
            unavailable.append((name, meta, missing_dep))
        else:
            available.append((name, meta))

    lines = [f"**Skills** ({len(index)} total)", ""]
    for name, meta in available:
        tags = []
        if meta.always_include:
            tags.append("always")
        if meta.admin_only:
            tags.append("admin")
        if meta.keywords:
            tags.append(f"keywords: {', '.join(meta.keywords[:5])}")
        if meta.resource_types:
            tags.append(f"resources: {', '.join(meta.resource_types)}")
        if meta.source_types:
            tags.append(f"sources: {', '.join(meta.source_types)}")
        tag_str = f" ({'; '.join(tags)})" if tags else ""
        lines.append(f"- **{name}**: {meta.description}{tag_str}")

    if unavailable:
        lines.append("")
        lines.append("**Unavailable** (install to enable):")
        for name, meta, missing_dep in unavailable:
            lines.append(f"- {name} — missing `{missing_dep}` (`uv sync --extra {name}`)")

    if disabled_skills:
        lines.append("")
        lines.append("**Disabled**:")
        for name, meta in disabled_skills:
            lines.append(f"- {name} — {meta.description}")

    return "\n".join(lines)


def _format_skill_detail(meta, name, disabled, is_admin):
    """Format detailed view for a single skill."""
    from .skills._loader import get_skill_availability

    lines = [f"**{name}**: {meta.description}", ""]

    status, missing_dep = get_skill_availability(meta)
    if name in disabled:
        lines.append("Status: disabled by config")
    elif status == "unavailable":
        lines.append(f"Status: unavailable (missing `{missing_dep}`)")
        lines.append(f"Install: `uv sync --extra {name}`")
    else:
        lines.append("Status: available")

    if meta.always_include:
        lines.append("Selection: always included")
    else:
        triggers = []
        if meta.keywords:
            triggers.append(f"keywords: {', '.join(meta.keywords)}")
        if meta.resource_types:
            triggers.append(f"resource types: {', '.join(meta.resource_types)}")
        if meta.source_types:
            triggers.append(f"source types: {', '.join(meta.source_types)}")
        if meta.file_types:
            triggers.append(f"file types: {', '.join(meta.file_types)}")
        if triggers:
            lines.append(f"Triggers: {'; '.join(triggers)}")

    if meta.admin_only:
        lines.append("Access: admin only")

    if meta.dependencies:
        lines.append(f"Dependencies: {', '.join(meta.dependencies)}")

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
    from .storage import validate_conversation_token
    validate_conversation_token(conversation_token)
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


@command("more", "Show execution trace for a task: `!more #31875` or `!more 31875`")
async def cmd_more(config, conn, user_id, conversation_token, args, client):
    # Parse task ID from args (strip # prefix if present)
    task_id_str = args.strip().lstrip("#")
    if not task_id_str.isdigit():
        return "Usage: `!more #<task_id>` — show the execution trace for a completed task."

    task_id = int(task_id_str)
    task = db.get_task(conn, task_id)
    if not task:
        return f"Task #{task_id} not found."

    # Only allow viewing your own tasks (unless admin)
    if task.user_id != user_id and not config.is_admin(user_id):
        return f"Task #{task_id} belongs to another user."

    if not task.execution_trace:
        if task.status in ("pending", "locked", "running"):
            return f"Task #{task_id} is still {task.status} — trace available after completion."
        return f"Task #{task_id} has no execution trace (pre-trace task or non-streaming execution)."

    try:
        trace = json.loads(task.execution_trace)
    except (json.JSONDecodeError, TypeError):
        return f"Task #{task_id} has a corrupted execution trace."

    # Format the trace
    prompt_preview = task.prompt[:80] + "..." if len(task.prompt) > 80 else task.prompt
    lines = [
        f"**Task #{task_id}** ({task.status}) — {prompt_preview}",
        "",
    ]

    for entry in trace:
        if entry.get("type") == "tool":
            lines.append(f"🔧 {entry['text']}")
        elif entry.get("type") == "text":
            # Indent assistant text to distinguish from tool calls
            text = entry["text"].strip()
            if text:
                lines.append(f"> {text}")

    # Add result summary
    if task.result:
        result_preview = task.result[:200] + "..." if len(task.result) > 200 else task.result
        lines.append("")
        lines.append(f"**Result:** {result_preview}")
    elif task.error:
        error_preview = task.error[:200] + "..." if len(task.error) > 200 else task.error
        lines.append("")
        lines.append(f"**Error:** {error_preview}")

    return "\n".join(lines)


# =============================================================================
# !search command
# =============================================================================


def _summarize_chunk(content: str) -> str:
    """Extract a 1-2 sentence summary from a memory search chunk."""
    lines = []
    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        for prefix in ("User: ", "Bot: ", "user: ", "bot: "):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):]
                break
        lines.append(stripped)

    text = " ".join(lines)
    if len(text) <= 200:
        return text
    for i in range(200, 80, -1):
        if text[i] in ".!?":
            return text[: i + 1]
    return text[:200] + "..."


def _search_memory(
    config: Config,
    conn: sqlite3.Connection,
    user_id: str,
    query: str,
    limit: int = 20,
    source_types: list[str] | None = None,
    since: str | None = None,
) -> list[dict]:
    """Search the memory index and resolve task/room metadata."""
    if source_types is None:
        source_types = ["conversation", "memory_file"]
    try:
        results = memory_search_mod.search(
            conn, user_id, query, limit=limit,
            source_types=source_types,
            since=since,
        )
    except Exception as e:
        logger.debug("Memory search failed: %s", e)
        return []

    out = []
    for r in results:
        entry: dict = {
            "summary": _summarize_chunk(r.content),
            "source_type": r.source_type,
        }

        if r.source_type == "conversation":
            task_id_str = r.metadata.get("task_id") or r.source_id
            try:
                task_id = int(task_id_str)
            except (ValueError, TypeError):
                task_id = None

            if task_id:
                task = db.get_task(conn, task_id)
                if task:
                    entry["task_id"] = task_id
                    entry["conversation_token"] = task.conversation_token
                    entry["talk_message_id"] = task.talk_message_id
                    created = task.created_at or ""
                    entry["date"] = created[:10] if len(created) >= 10 else created
                    entry["room"] = task.conversation_token or ""
                else:
                    entry["task_id"] = task_id
                    entry["conversation_token"] = None
                    entry["date"] = ""
                    entry["room"] = ""
            else:
                entry["task_id"] = None
                entry["conversation_token"] = None
                entry["date"] = ""
                entry["room"] = ""
        else:
            entry["task_id"] = None
            entry["conversation_token"] = None
            entry["date"] = ""
            entry["room"] = ""

        out.append(entry)

    return out


async def _search_talk_api(
    config: Config,
    query: str,
    limit: int = 20,
) -> list[dict]:
    """Search Nextcloud Talk messages via the unified search API."""
    data = ocs_get(
        config,
        "/search/providers/talk-message/search",
        params={"term": query, "limit": str(limit)},
        timeout=10.0,
    )
    if not data:
        return []

    entries = data.get("entries", [])
    if not entries:
        return []

    base_url = config.nextcloud.url.rstrip("/")
    out = []
    for entry in entries:
        attrs = entry.get("attributes", {})
        token = attrs.get("conversation", "")
        message_id = attrs.get("messageId", "")
        title = entry.get("title", "")
        subline = entry.get("subline", "")

        talk_link = f"{base_url}/call/{token}#message_{message_id}"

        # subline has the message content; title is "username in roomname"
        out.append({
            "date": "",
            "room": token,
            "summary": subline or title,
            "talk_link": talk_link,
            "conversation_token": token,
        })

    return out


@dataclass
class SearchArgs:
    scope: str | None  # None=current room, "all", or a conversation token
    query: str
    since: str | None = None  # ISO date, e.g. "2026-03-25"
    memories_only: bool = False


def _parse_search_args(args_str: str) -> SearchArgs:
    """Parse search arguments into SearchArgs.

    Flags (order-independent, combinable):
        --all               Search all rooms
        --room <token>      Search specific room
        --since YYYY-MM-DD  Only results on or after date
        --week              Shorthand for --since 7 days ago
        --memories          Only search memory files (not conversations)
    """
    parts = args_str.strip().split()
    if not parts:
        return SearchArgs(scope=None, query="")

    scope: str | None = None
    since: str | None = None
    memories_only = False
    query_parts: list[str] = []

    i = 0
    while i < len(parts):
        token = parts[i]
        if token == "--all":
            scope = "all"
        elif token == "--room" and i + 1 < len(parts):
            i += 1
            scope = parts[i].lstrip("#")
        elif token == "--since" and i + 1 < len(parts):
            i += 1
            since = parts[i]
        elif token == "--week":
            since = (date.today() - timedelta(days=7)).isoformat()
        elif token == "--memories":
            memories_only = True
        else:
            query_parts.append(token)
        i += 1

    query = " ".join(query_parts)

    # If no actual query was found (only flags with no value), treat the whole
    # input as query text so backward compat is preserved for edge cases
    if not query and not any(p.startswith("--") for p in parts if p in ("--all", "--week", "--memories")) and since is None:
        query = args_str.strip()

    return SearchArgs(scope=scope, query=query, since=since, memories_only=memories_only)


async def _resolve_room_names(
    client: TalkClient,
    tokens: set[str],
) -> dict[str, str]:
    """Resolve conversation tokens to display names. Returns token→name map."""
    names: dict[str, str] = {}
    for token in tokens:
        try:
            info = await client.get_conversation_info(token)
            names[token] = info.get("displayName", token)
        except Exception:
            names[token] = token
    return names


def _build_message_link(config: Config, token: str, message_id: int) -> str:
    """Build a Nextcloud Talk deep link to a specific message."""
    base_url = config.nextcloud.url.rstrip("/")
    return f"{base_url}/call/{token}#message_{message_id}"


def _format_search_results(results: list[dict], query: str) -> str:
    """Format search results for Talk output."""
    count = len(results)
    noun = "result" if count == 1 else "results"
    lines = [f"**{count} {noun}** for \"{query}\"", ""]

    for i, r in enumerate(results, 1):
        date = r.get("date", "")
        room_name = r.get("room_name", r.get("room", ""))
        summary = r.get("summary", "")

        location_parts = []
        if date:
            location_parts.append(f"**{date}**")
        if room_name:
            location_parts.append(f"in {room_name}")
        location = " ".join(location_parts)

        if location:
            lines.append(f"{i}. {location} — {summary}")
        else:
            lines.append(f"{i}. {summary}")

        if r.get("talk_link"):
            lines.append(f"   → {r['talk_link']}")
        elif r.get("task_id"):
            lines.append(f"   → task #{r['task_id']}")

        lines.append("")

    return "\n".join(lines).rstrip()


@command("search", "Search conversation history: `!search <query>`, `!search --all <query>`, `!search --since DATE <query>`, `!search --memories <query>`")
async def cmd_search(config, conn, user_id, conversation_token, args, client):
    parsed = _parse_search_args(args)
    if not parsed.query:
        return (
            "Usage: `!search <query>`, `!search --all <query>`, "
            "`!search --room <token> <query>`\n"
            "Filters: `--since YYYY-MM-DD`, `--week`, `--memories`"
        )

    source_types = ["memory_file"] if parsed.memories_only else None
    mem_results = _search_memory(
        config, conn, user_id, parsed.query,
        source_types=source_types, since=parsed.since,
    )

    # Skip Talk API when filtering to memories only
    if parsed.memories_only:
        talk_results: list[dict] = []
    else:
        talk_results = await _search_talk_api(config, parsed.query)

    # Merge and deduplicate (memory results take priority)
    seen_task_ids: set[int] = set()
    all_results: list[dict] = []

    for r in mem_results:
        tid = r.get("task_id")
        if tid:
            seen_task_ids.add(tid)
        all_results.append(r)

    for r in talk_results:
        tid = r.get("task_id")
        if tid and tid in seen_task_ids:
            continue
        all_results.append(r)

    # Filter Talk results by date when --since is set
    if parsed.since and talk_results:
        all_results = [
            r for r in all_results
            if not r.get("date") or r["date"] >= parsed.since
        ]

    # Apply room scoping
    if parsed.scope is None:
        all_results = [
            r for r in all_results
            if r.get("conversation_token") == conversation_token
        ]
    elif parsed.scope != "all":
        all_results = [
            r for r in all_results
            if r.get("conversation_token") == parsed.scope
        ]

    all_results = all_results[:8]

    if not all_results:
        return f"No results for \"{parsed.query}\"."

    # Resolve room display names for all unique tokens
    tokens = {r["conversation_token"] for r in all_results if r.get("conversation_token")}
    room_names = await _resolve_room_names(client, tokens)

    # Enrich results with room names and message links
    for r in all_results:
        token = r.get("conversation_token", "")
        r["room_name"] = room_names.get(token, token)

        # Build message deep link from task's talk_message_id
        msg_id = r.get("talk_message_id")
        if token and msg_id:
            r["talk_link"] = _build_message_link(config, token, msg_id)

    return _format_search_results(all_results, parsed.query)


@command("trust", "Trust an email sender: `!trust sender@example.com`")
async def cmd_trust(config, conn, user_id, conversation_token, args, client):
    email = args.strip().lower()
    if not email:
        # List trusted senders
        db_senders = db.list_trusted_senders(conn, user_id)
        user_config = config.users.get(user_id)
        config_patterns = user_config.trusted_email_senders if user_config else []

        lines = ["**Trusted email senders:**", ""]
        if config_patterns:
            for p in sorted(config_patterns):
                lines.append(f"- `{p}` (config)")
        if db_senders:
            for s in db_senders:
                lines.append(f"- `{s['sender_email']}`")
        if not config_patterns and not db_senders:
            lines.append("No trusted senders configured.")
        return "\n".join(lines)

    if "@" not in email:
        return "Usage: `!trust sender@example.com` or `!trust` to list."

    added = db.add_trusted_sender(conn, user_id, email)
    if added:
        return f"Trusted `{email}` — future emails from this sender will be processed automatically."
    return f"`{email}` is already trusted."


@command("untrust", "Remove a trusted email sender: `!untrust sender@example.com`")
async def cmd_untrust(config, conn, user_id, conversation_token, args, client):
    email = args.strip().lower()
    if not email or "@" not in email:
        return "Usage: `!untrust sender@example.com`"

    removed = db.remove_trusted_sender(conn, user_id, email)
    if removed:
        return f"Removed `{email}` from trusted senders."
    return f"`{email}` is not in your trusted senders list. Note: senders in config files must be removed from the config."
