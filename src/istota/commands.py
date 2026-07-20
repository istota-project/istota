"""!command dispatch system — synchronous commands intercepted before task queue."""

import json
import logging
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone


from typing import TYPE_CHECKING

from . import db
from .brain import Brain, make_brain
from .memory import search as memory_search_mod
from .config import Config
from .nextcloud_client import ocs_get

if TYPE_CHECKING:
    from .transport.registry import TransportRegistry

logger = logging.getLogger("istota.commands")


@dataclass
class CommandContext:
    """Everything a ``!command`` handler needs, free of any one surface.

    A handler reads ``config`` / ``conn`` / ``user_id`` / ``conversation_token``
    / ``args`` like before. ``surface`` (``"talk"`` | ``"web"`` | future
    ``"matrix"``) and ``registry`` let the handful of commands that genuinely
    need surface-specific behavior (room-name resolution, the Talk-only search
    enhancement) branch on the surface or resolve a transport — instead of being
    handed a baked-in ``TalkClient``. Most handlers ignore both.
    """

    config: Config
    conn: sqlite3.Connection
    user_id: str
    conversation_token: str
    args: str
    surface: str = "talk"
    registry: "TransportRegistry | None" = None
    # Output slot: a handler that returns plain text but also has a structured
    # payload (e.g. !search's result cards) sets this; ``dispatch`` threads it
    # onto the returned ``CommandResult.data`` for rich stream surfaces. Most
    # handlers leave it None and just return their text.
    result_data: dict | None = None


@dataclass
class CommandResult:
    """Outcome of ``dispatch``.

    ``handled`` is False only when the content was not a ``!command`` at all (the
    caller falls through to task creation). ``text`` is the command's response.
    ``delivered`` is True when ``dispatch`` already pushed ``text`` to the
    surface (push surfaces like Talk); on stream surfaces (web chat) it stays
    False and the caller renders ``text`` inline.
    """

    handled: bool
    text: str | None = None
    delivered: bool = False
    # Optional structured payload for rich stream surfaces (web chat). Push
    # surfaces (Talk) ignore it and render `text`; the web caller forwards it as
    # `command_data` so the client can render a dedicated component. Additive and
    # backward-compatible — absent `data` behaves exactly as before.
    data: dict | None = None


# Type for command handlers — a single surface-agnostic context in. A handler
# returns plain `text`, or a `CommandResult` when it also carries a structured
# `data` payload (e.g. !search's clickable result cards).
CommandHandler = Callable[[CommandContext], Awaitable["str | CommandResult"]]

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


# `!model <alias> <prompt>` — one-shot model override for a single task.
# The alias table is owned by the active brain (each brain implementation
# carries its own provider-specific model namespace); this surface is just
# the user-facing parser plus a usage helper. Roles like ``smart`` are
# resolved by the brain through the global operator-override table in
# ``brain._roles``.


@dataclass
class ModelPrefix:
    """Result of parsing a `!model` prefix.

    `unknown_alias` is set when the prefix matched but the alias wasn't in
    `MODEL_ALIASES` (or no alias was supplied) — caller posts a usage message.
    Otherwise `model`/`effort` carry the override (both may be None for the
    explicit "default" alias) and `remainder` is the prompt with the prefix
    stripped.
    """

    model: str | None
    effort: str | None
    remainder: str
    unknown_alias: str | None = None


def parse_model_prefix(content: str, brain: Brain) -> ModelPrefix | None:
    """Parse a `!model <alias> <prompt>` prefix using ``brain`` for alias lookup.

    Returns None when `content` is not a `!model` prefix at all (so the
    caller's normal command-dispatch path runs unchanged). The active
    brain owns the alias namespace, so this parser is pure syntax and
    delegates resolution.
    """
    stripped = content.strip()
    match = re.match(r"^!model\b\s*(\S+)?\s*(.*)", stripped, re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    alias = (match.group(1) or "").lower()
    remainder = match.group(2).strip()
    resolved = brain.resolve_alias(alias) if alias else None
    if resolved is None:
        return ModelPrefix(model=None, effort=None, remainder=remainder, unknown_alias=alias)
    model, effort = resolved
    return ModelPrefix(model=model, effort=effort, remainder=remainder)


def model_prefix_usage(brain: Brain) -> str:
    """User-facing help string listing the active brain's `!model` aliases."""
    aliases = [alias for alias, _model, _effort in brain.list_aliases()]
    return f"Usage: `!model <alias> <prompt>`. Aliases: {', '.join(f'`{a}`' for a in aliases)}."


@dataclass
class ModelPrefixOutcome:
    """Surface-agnostic result of pre-processing a message for a `!model` prefix.

    ``matched`` is True when the message started with ``!model``. ``usage`` is
    set when the prefix was malformed (unknown alias, or no prompt and no
    attachments) — the caller shows it and stops. Otherwise ``model`` / ``effort``
    carry the override and ``content`` is the prompt with the prefix stripped.
    Both Talk inbound and the web send handler call this so the rule
    ("`!model opus` alone is valid only with an attachment") lives in one place.
    """

    matched: bool
    content: str
    model: str | None = None
    effort: str | None = None
    usage: str | None = None


def resolve_model_prefix(
    content: str, brain: Brain, *, has_attachments: bool = False,
) -> ModelPrefixOutcome:
    """Apply the shared `!model <alias> <prompt>` rule across surfaces."""
    prefix = parse_model_prefix(content, brain)
    if prefix is None:
        return ModelPrefixOutcome(matched=False, content=content)
    if prefix.unknown_alias is not None:
        return ModelPrefixOutcome(matched=True, content=content, usage=model_prefix_usage(brain))
    # "!model opus" with no prompt is only meaningful when there's an attachment
    # to act on; otherwise there's nothing to do — show usage.
    if not prefix.remainder.strip() and not has_attachments:
        return ModelPrefixOutcome(matched=True, content=content, usage=model_prefix_usage(brain))
    return ModelPrefixOutcome(
        matched=True, content=prefix.remainder, model=prefix.model, effort=prefix.effort,
    )


async def resolve_room_name(ctx: CommandContext, token: str) -> str:
    """Resolve a channel token to a human-readable name, surface-agnostically.

    Talk goes through the registered transport's ``resolve_channel_name`` (an OCS
    read); web chat reads the room's stored name; any other surface falls back to
    the opaque token.
    """
    if not token:
        return token
    if ctx.registry is not None:
        transport = ctx.registry.get(ctx.surface)
        resolver = getattr(transport, "resolve_channel_name", None)
        if resolver is not None:
            try:
                return await resolver(token)
            except Exception:
                return token
    if ctx.surface == "web":
        try:
            room = db.get_web_chat_room_by_token(ctx.conn, token)
            if room is not None and room.name:
                return room.name
        except Exception:
            pass
    return token


async def _deliver_result(
    config: Config,
    registry: "TransportRegistry | None",
    surface: str,
    conversation_token: str,
    text: str,
    data: dict | None = None,
) -> CommandResult:
    """Push ``text`` to the surface (push transports) or return it for the caller
    to render (stream surfaces / no registered transport).

    ``data`` (a structured payload) rides along on the returned result for
    stream surfaces to render; push transports deliver only ``text``."""
    transport = registry.get(surface) if registry is not None else None
    is_push = (
        transport is not None
        and getattr(transport.capabilities, "surface_class", "push") == "push"
    )
    if is_push and text:
        try:
            await transport.deliver(conversation_token, text)
        except Exception as e:
            logger.error("Command delivery to %s failed: %s", surface, e, exc_info=True)
        return CommandResult(handled=True, text=text, delivered=True, data=data)
    return CommandResult(handled=True, text=text, delivered=False, data=data)


async def dispatch(
    config: Config,
    user_id: str,
    conversation_token: str,
    content: str,
    *,
    surface: str = "talk",
    conn: sqlite3.Connection | None = None,
    registry: "TransportRegistry | None" = None,
) -> CommandResult:
    """Dispatch ``content`` as a ``!command``, surface-agnostically.

    On a push surface (Talk) the result is delivered through the transport and
    ``CommandResult.delivered`` is True; on a stream surface (web chat) it is
    returned in ``CommandResult.text`` for the caller to render inline. Returns
    ``CommandResult(handled=False)`` when ``content`` is not a command.

    ``conn`` is reused when supplied (Talk inbound runs inside its poll
    transaction); otherwise a connection is opened for the handler. ``registry``
    is built on demand when omitted (``make_registry`` does no I/O).
    """
    parsed = parse_command(content)
    if parsed is None:
        return CommandResult(handled=False)

    cmd_name, args_str = parsed
    if registry is None:
        from .transport import make_registry
        registry = make_registry(config)

    if cmd_name not in COMMANDS:
        text = f"Unknown command `!{cmd_name}`. Type `!help` for available commands."
        return await _deliver_result(config, registry, surface, conversation_token, text)

    handler, _ = COMMANDS[cmd_name]

    async def _run(active_conn: sqlite3.Connection) -> tuple[str, dict | None]:
        ctx = CommandContext(
            config=config,
            conn=active_conn,
            user_id=user_id,
            conversation_token=conversation_token,
            args=args_str,
            surface=surface,
            registry=registry,
        )
        result = await handler(ctx)
        # A handler returns plain text (and may set ``ctx.result_data`` for a
        # structured payload), or a CommandResult carrying its own text + data.
        if isinstance(result, CommandResult):
            return (result.text or "", result.data if result.data is not None else ctx.result_data)
        return (result, ctx.result_data)

    data: dict | None = None
    try:
        if conn is not None:
            text, data = await _run(conn)
        else:
            with db.get_db(config.db_path) as own_conn:
                text, data = await _run(own_conn)
    except Exception as e:
        logger.error("Command !%s failed: %s", cmd_name, e, exc_info=True)
        text = f"Command `!{cmd_name}` failed: {e}"

    return await _deliver_result(config, registry, surface, conversation_token, text or "", data)


# =============================================================================
# Command implementations
# =============================================================================


@command("help", "List available commands")
async def cmd_help(ctx: CommandContext):
    config = ctx.config
    lines = ["**Available commands:**", ""]
    for name, (_, help_text) in sorted(COMMANDS.items()):
        lines.append(f"- `!{name}` -- {help_text}")
    lines.append("")
    lines.append("**Per-task model override:**")
    lines.append("")
    aliases = [alias for alias, _m, _e in make_brain(config.brain).list_aliases()]
    lines.append(f"- `!model <alias> <prompt>` — one-shot. Aliases: {', '.join(f'`{a}`' for a in aliases)}.")
    return "\n".join(lines)


@command("stop", "Cancel your currently running task")
async def cmd_stop(ctx: CommandContext):
    conn, user_id = ctx.conn, ctx.user_id
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


@command("models", "List available model aliases (and what they resolve to)")
async def cmd_models(ctx: CommandContext):
    config = ctx.config
    lines = ["**Model aliases**", "", "Use `!model <alias> <prompt>` to override the model for a single task.", ""]
    for alias, model, effort in make_brain(config.brain).list_aliases():
        if model is None:
            target = "(no override — use default)"
        elif effort:
            target = f"`{model}` + effort `{effort}`"
        else:
            target = f"`{model}`"
        lines.append(f"- `{alias}` → {target}")
    return "\n".join(lines)


# Effort levels the CLI brains accept. `!room effort <level>` validates against
# this set; the brain silently drops effort for models that don't support it.
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _room_effort_usage() -> str:
    levels = ", ".join(f"`{lvl}`" for lvl in _EFFORT_LEVELS)
    return f"Usage: `!room effort <level>` (or `default` to clear). Levels: {levels}."


def _describe_room_default(model: str | None, effort: str | None) -> str:
    if not model and not effort:
        return "This room uses the instance default model."
    parts = []
    if model:
        parts.append(f"model `{model}`")
    if effort:
        parts.append(f"effort `{effort}`")
    return "Room default: " + " + ".join(parts) + "."


@command(
    "room",
    "Show or set this room's standing model/effort default: "
    "`!room`, `!room model <alias>`, `!room effort <level>` "
    "(applies to every message here, on Talk and web; `default` clears)",
)
async def cmd_room(ctx: CommandContext):
    config, conn, args = ctx.config, ctx.conn, ctx.args
    # Resolve the canonical room token — the default lives on the shared rooms
    # registry, keyed by canonical token, so a per-surface ref must be mapped.
    token = db.resolve_room_token(conn, ctx.surface, ctx.conversation_token) \
        or ctx.conversation_token
    room = db.get_room(conn, token)
    if room is None:
        return "This room isn't registered yet — send a message first, then set its default."

    parts = args.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if not sub:
        return _describe_room_default(room.model, room.effort)

    if sub == "model":
        alias = rest.lower()
        aliases = [a for a, _m, _e in make_brain(config.brain).list_aliases()]
        if not alias:
            return (
                "Usage: `!room model <alias>` (or `default` to clear). "
                f"Aliases: {', '.join(f'`{a}`' for a in aliases)}."
            )
        resolved = make_brain(config.brain).resolve_alias(alias)
        if resolved is None:
            return (
                f"Unknown model alias `{alias}`. "
                f"Aliases: {', '.join(f'`{a}`' for a in aliases)}."
            )
        model, effort = resolved
        if model is None:
            # `!room model default` — a full reset of the model dimension,
            # effort included.
            db.set_room_model_effort(conn, token, None, None)
            return "Room model reset — this room now uses the instance default."
        if effort is not None:
            # An effort-bearing alias (e.g. `opus-high`) is an explicit
            # both-pick, so it sets effort too.
            db.set_room_model_effort(conn, token, model, effort)
        else:
            # A plain model alias leaves any separately-set `!room effort` intact
            # (the two knobs are orthogonal).
            db.set_room_model(conn, token, model)
            effort = db.get_room(conn, token).effort
        return _describe_room_default(model, effort)

    if sub == "effort":
        level = rest.lower()
        if not level:
            return _room_effort_usage()
        if level == "default":
            db.set_room_effort(conn, token, None)
            return "Room effort reset."
        if level not in _EFFORT_LEVELS:
            return _room_effort_usage()
        db.set_room_effort(conn, token, level)
        return f"Room effort set to `{level}`."

    return (
        "Usage: `!room` (show), `!room model <alias>`, `!room effort <level>`. "
        "Use `default` to clear."
    )


@command("status", "Show your running/pending tasks and system status")
async def cmd_status(ctx: CommandContext):
    config, conn, user_id = ctx.config, ctx.conn, ctx.user_id
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
async def cmd_memory(ctx: CommandContext):
    config, conn = ctx.config, ctx.conn
    user_id, conversation_token, args = ctx.user_id, ctx.conversation_token, ctx.args
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
            from .memory.knowledge_graph import ensure_table, get_current_facts, get_fact_count, format_facts_for_prompt
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
            from .memory.knowledge_graph import ensure_table, get_current_facts, format_facts_for_prompt
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
async def cmd_cron(ctx: CommandContext):
    config, conn, user_id, args = ctx.config, ctx.conn, ctx.user_id, ctx.args
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
async def cmd_skills(ctx: CommandContext):
    config, user_id, args = ctx.config, ctx.user_id, ctx.args
    from .skills._loader import (
        effective_disabled_skills,
        get_skill_availability,
        load_skill_index,
    )

    skills_dir = config.skills_dir
    bundled_dir = getattr(config, "bundled_skills_dir", None)
    index = load_skill_index(skills_dir, bundled_dir=bundled_dir)

    is_admin = config.is_admin(user_id)

    # Disabled = instance-wide + per-user + the capability gate (a skill whose
    # requires_capability isn't available, e.g. browse/devbox with the service
    # undeployed). Shared with the executor + skills CLI so all three agree.
    disabled = effective_disabled_skills(config, user_id, index)

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
async def cmd_check(ctx: CommandContext):
    config, conn = ctx.config, ctx.conn
    user_id, conversation_token = ctx.user_id, ctx.conversation_token
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
            lines.append("- Claude + Bash: **FAIL** (expected 'healthcheck-ok')")
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


# Upper bound on turns pulled for an export. A conversation rarely approaches
# this; it just keeps a runaway room from materializing unbounded history.
_EXPORT_LIMIT = 10000


def _format_db_timestamp(created_at: str, tz=None) -> str:
    """Format a DB ISO ``created_at`` string to a readable local time."""
    if not created_at:
        return ""
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if tz:
            dt = dt.astimezone(tz)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return created_at[:16]


def _format_history_markdown(
    messages: list["db.ConversationMessage"], bot_name: str, tz=None,
) -> str:
    """Render completed conversation turns (user prompt + bot result) as
    markdown. Surface-agnostic — works for any conversation_token."""
    lines: list[str] = []
    for m in messages:
        ts = _format_db_timestamp(m.created_at, tz)
        if m.prompt and m.prompt.strip():
            lines.append("")
            lines.append(f"**{m.user_id or 'User'}** — {ts}")
            lines.append(m.prompt.strip())
        if m.result and m.result.strip():
            lines.append("")
            lines.append(f"**{bot_name}** — {ts}")
            lines.append(m.result.strip())
        lines.append("")
        lines.append("---")
    return "\n".join(lines)


def _format_history_text(
    messages: list["db.ConversationMessage"], bot_name: str, tz=None,
) -> str:
    """Render completed conversation turns as plaintext."""
    lines: list[str] = []
    for m in messages:
        ts = _format_db_timestamp(m.created_at, tz)
        if m.prompt and m.prompt.strip():
            lines.append(f"{m.user_id or 'User'} — {ts}")
            lines.append(m.prompt.strip())
            lines.append("")
        if m.result and m.result.strip():
            lines.append(f"{bot_name} — {ts}")
            lines.append(m.result.strip())
            lines.append("")
    return "\n".join(lines).rstrip()


@command("export", "Export conversation history to a file: `!export [markdown|text]`")
async def cmd_export(ctx: CommandContext):
    config, conn = ctx.config, ctx.conn
    user_id, conversation_token, args = ctx.user_id, ctx.conversation_token, ctx.args
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

    # Resolve user timezone — live DB value so it tracks travel (ISSUE-099).
    from zoneinfo import ZoneInfo

    tz_str = config.resolve_user_timezone(user_id)
    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        tz = None

    bot_name = config.bot_name

    # Conversation history comes from the tasks DB (each completed task is a
    # user prompt + bot result turn), so export works on any surface — Talk,
    # web chat, future ones — without reaching into a surface's message store.
    messages = db.get_conversation_history(conn, conversation_token, limit=_EXPORT_LIMIT)
    format_md = fmt == "markdown"
    render = _format_history_markdown if format_md else _format_history_text

    # Check for existing export
    existing_meta = None
    if export_path.exists():
        try:
            first_line = export_path.read_text().split("\n", 1)[0]
            existing_meta = _parse_export_metadata(first_line)
        except Exception:
            pass

    if existing_meta and existing_meta["token"] == conversation_token:
        # Incremental export — only turns newer than the last exported task id.
        since_id = existing_meta["last_id"]
        new_messages = [m for m in messages if m.id > since_id]
        if not new_messages:
            return "No new messages since last export."

        last_id = new_messages[-1].id
        new_content = render(new_messages, bot_name, tz=tz)

        existing_content = export_path.read_text()
        # Replace first line (metadata) with the updated one, then append.
        rest = existing_content.split("\n", 1)[1] if "\n" in existing_content else ""
        new_meta = _build_export_metadata(conversation_token, last_id, fmt)
        export_path.write_text(new_meta + "\n" + rest.rstrip("\n") + "\n" + new_content + "\n")

        rel_path = f"/{export_path.relative_to(mount)}"
        return f"Appended {len(new_messages)} new messages to `{rel_path}`"

    # Full export
    if not messages:
        return "No messages to export."

    last_id = messages[-1].id
    title = await resolve_room_name(ctx, conversation_token)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    if tz:
        now_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M")

    meta_line = _build_export_metadata(conversation_token, last_id, fmt)

    if format_md:
        header_parts = [meta_line, "", f"# {title}", "", f"**Exported:** {now_str}", "", "---"]
    else:
        header_parts = [meta_line, "", title, f"Exported: {now_str}", "=" * 40]

    body = render(messages, bot_name, tz=tz)
    content = "\n".join(header_parts) + "\n" + body + "\n"
    export_path.write_text(content)

    rel_path = f"/{export_path.relative_to(mount)}"
    return f"Exported {len(messages)} messages to `{rel_path}`"


@command("more", "Show execution trace for a task: `!more #31875` or `!more 31875`")
async def cmd_more(ctx: CommandContext):
    config, conn, user_id, args = ctx.config, ctx.conn, ctx.user_id, ctx.args
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


# Personal / channel memory source types — not room-bound conversation turns.
_MEMORY_SOURCE_TYPES = ("memory_file", "user_memory", "channel_memory", "channel_memory_durable")
# The full default search set: room conversation turns + every memory kind.
_DEFAULT_SEARCH_SOURCE_TYPES = ["conversation", *_MEMORY_SOURCE_TYPES]
# Channel-scoped memory (belongs to the current room we fetched channel:{token} for).
_CHANNEL_MEMORY_SOURCE_TYPES = ("channel_memory", "channel_memory_durable")


def _search_memory(
    config: Config,
    conn: sqlite3.Connection,
    user_id: str,
    query: str,
    *,
    limit: int = 20,
    source_types: list[str] | None = None,
    since: str | None = None,
    conversation_token: str | None = None,
    match_mode: str = "and",
    allow_or_fallback: bool = False,
    prefix: bool = False,
) -> list[dict]:
    """Search the memory index and classify each hit onto a scope axis.

    Mirrors the two correct search callers (executor recall + the memory_search
    skill CLI): when ``conversation_token`` is set, the ``channel:{token}``
    namespace is searched too, and the default source set covers conversation
    turns plus every memory kind.

    Each result is classified independent of task-row existence:
      - ``conversation`` rows are room-bound — room token from the task row, or
        the durable ``messages`` store when the task row has aged out; else
        room-unknown (``conversation_token=None``, shown only under ``--all``).
      - ``channel_memory`` / ``channel_memory_durable`` rows belong to the
        current channel (we only fetched ``channel:{current}``): tagged with the
        current room token and ``is_memory=True``.
      - ``memory_file`` / ``user_memory`` rows are the user's personal memory —
        not room-bound, ``is_memory=True``, room token ``None``.

    Results are deduped by ``task_id`` (conversation) and
    ``(source_type, source_id)`` (memory), keeping the higher-ranked hit.
    """
    if source_types is None:
        source_types = list(_DEFAULT_SEARCH_SOURCE_TYPES)

    include_user_ids = [f"channel:{conversation_token}"] if conversation_token else None

    try:
        results = memory_search_mod.search(
            conn, user_id, query, limit=limit,
            source_types=source_types,
            since=since,
            include_user_ids=include_user_ids,
            match_mode=match_mode,
            allow_or_fallback=allow_or_fallback,
            prefix=prefix,
        )
    except Exception as e:
        logger.debug("Memory search failed: %s", e)
        return []

    out: list[dict] = []
    seen_conv: set[int] = set()
    seen_mem: set[tuple[str, str]] = set()

    for r in results:
        is_memory = r.source_type in _MEMORY_SOURCE_TYPES
        entry: dict = {
            "summary": _summarize_chunk(r.content),
            "source_type": r.source_type,
            "source_id": r.source_id,
            "is_memory": is_memory,
            "task_id": None,
            "talk_message_id": None,
            "conversation_token": None,
            "date": "",
            "room": "",
        }

        if r.source_type == "conversation":
            task_id_str = r.metadata.get("task_id") or r.source_id
            try:
                task_id = int(task_id_str)
            except (ValueError, TypeError):
                task_id = None

            room_token: str | None = None
            if task_id is not None:
                if task_id in seen_conv:
                    continue  # keep the higher-ranked chunk for this turn
                seen_conv.add(task_id)
                task = db.get_task(conn, task_id)
                if task:
                    room_token = task.conversation_token
                    entry["talk_message_id"] = task.talk_message_id
                    created = task.created_at or ""
                    entry["date"] = created[:10] if len(created) >= 10 else created
                else:
                    # Task row aged out of retention — recover room scope from
                    # the durable messages store so the hit isn't dropped.
                    try:
                        room_token = db.get_message_room_for_task(conn, task_id)
                    except Exception:
                        room_token = None
            entry["task_id"] = task_id
            entry["conversation_token"] = room_token
            entry["room"] = room_token or ""

        elif r.source_type in _CHANNEL_MEMORY_SOURCE_TYPES:
            key = (r.source_type, str(r.source_id))
            if key in seen_mem:
                continue
            seen_mem.add(key)
            entry["conversation_token"] = conversation_token
            entry["room"] = conversation_token or ""

        else:  # memory_file / user_memory — personal, not room-bound
            key = (r.source_type, str(r.source_id))
            if key in seen_mem:
                continue
            seen_mem.add(key)

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
    ctx: CommandContext,
    tokens: set[str],
) -> dict[str, str]:
    """Resolve conversation tokens to display names, surface-agnostically.
    Returns token→name map."""
    names: dict[str, str] = {}
    for token in tokens:
        names[token] = await resolve_room_name(ctx, token)
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


def _build_search_data(
    config: Config, query: str, results: list[dict], text: str,
) -> dict:
    """Build the structured `search_results` payload for rich stream surfaces.

    Maps each enriched result dict onto the surface-neutral card shape the web
    client renders. A conversation card carries the `task_id` the client jumps
    to; a `talk_link` is included only when a `talk_message_id` is present (the
    Talk deep-link ceiling). `text` is the plain-text fallback (the transcript's
    durable record, and what a non-structured client shows)."""
    out: list[dict] = []
    for r in results:
        room_token = r.get("conversation_token")
        talk_message_id = r.get("talk_message_id")
        talk_link = r.get("talk_link")
        if not talk_link and room_token and talk_message_id:
            talk_link = _build_message_link(config, room_token, talk_message_id)
        room_name = r.get("room_name") or None
        out.append({
            "source_type": r.get("source_type"),
            "summary": r.get("summary", ""),
            "date": r.get("date", ""),
            "room_token": room_token,
            "room_name": room_name,
            "task_id": r.get("task_id"),
            "talk_message_id": talk_message_id,
            "talk_link": talk_link,
        })
    return {"kind": "search_results", "query": query, "results": out, "text": text}


@command("search", "Search conversation history: `!search <query>`, `!search --all <query>`, `!search --since DATE <query>`, `!search --memories <query>`")
async def cmd_search(ctx: CommandContext):
    config, conn = ctx.config, ctx.conn
    user_id, conversation_token, args = ctx.user_id, ctx.conversation_token, ctx.args
    parsed = _parse_search_args(args)
    if not parsed.query:
        return (
            "Usage: `!search <query>`, `!search --all <query>`, "
            "`!search --room <token> <query>`\n"
            "Filters: `--since YYYY-MM-DD`, `--week`, `--memories`"
        )

    source_types = list(_MEMORY_SOURCE_TYPES) if parsed.memories_only else None

    # The Nextcloud Talk full-text search is a Talk-only enhancement layered on
    # top of the memory index; skip it on other surfaces and for memories-only.
    if parsed.memories_only or ctx.surface != "talk":
        talk_results: list[dict] = []
    else:
        talk_results = await _search_talk_api(config, parsed.query)

    def _assemble(mem_results: list[dict]) -> list[dict]:
        """Merge memory + Talk hits, apply the --since / room-scope filters, cap.
        Memory results take priority for task-id dedup."""
        seen_task_ids: set[int] = set()
        merged: list[dict] = []
        for r in mem_results:
            tid = r.get("task_id")
            if tid:
                seen_task_ids.add(tid)
            merged.append(r)
        for r in talk_results:
            tid = r.get("task_id")
            if tid and tid in seen_task_ids:
                continue
            merged.append(r)

        if parsed.since and talk_results:
            merged = [r for r in merged if not r.get("date") or r["date"] >= parsed.since]

        # Apply room scoping. Only the conversation/Talk axis is room-bound;
        # memory rows (personal + current-channel memory) are never discarded in
        # the current-room view, and are excluded from a specific-room search
        # (they belong to the current room / the user, not the named room).
        if parsed.scope is None:
            merged = [
                r for r in merged
                if r.get("is_memory") or r.get("conversation_token") == conversation_token
            ]
        elif parsed.scope != "all":
            merged = [
                r for r in merged
                if not r.get("is_memory") and r.get("conversation_token") == parsed.scope
            ]
        return merged[:8]

    # The memory index is the surface-agnostic backbone. Pass the current room so
    # the channel namespace + channel-memory source types are searched, with
    # prefix matching for forgiveness. Run strict AND first (precision); if the
    # *scoped* result is empty, retry once in OR mode. The forgiveness gate has
    # to key on the scoped emptiness — a strict AND match in another room (found
    # via the user namespace) would otherwise suppress the OR retry even though
    # it never survives the scope filter, silently defeating forgiveness.
    def _run(match_mode: str) -> list[dict]:
        return _search_memory(
            config, conn, user_id, parsed.query,
            source_types=source_types, since=parsed.since,
            conversation_token=conversation_token,
            match_mode=match_mode, prefix=True,
        )

    all_results = _assemble(_run("and"))
    if not all_results:
        all_results = _assemble(_run("or"))

    if not all_results:
        # The plain-text message is the durable record; the empty structured
        # card lets a rich stream client render "no results" in place.
        text = f"No results for \"{parsed.query}\"."
        ctx.result_data = _build_search_data(config, parsed.query, [], text)
        return text

    # Resolve room display names for all unique tokens
    tokens = {r["conversation_token"] for r in all_results if r.get("conversation_token")}
    room_names = await _resolve_room_names(ctx, tokens)

    # Enrich results with room names and (Talk-only) message links
    for r in all_results:
        token = r.get("conversation_token", "")
        r["room_name"] = room_names.get(token, token)

        # Deep links are a Talk concept — only build them on the Talk surface.
        msg_id = r.get("talk_message_id")
        if ctx.surface == "talk" and token and msg_id:
            r["talk_link"] = _build_message_link(config, token, msg_id)

    text = _format_search_results(all_results, parsed.query)
    ctx.result_data = _build_search_data(config, parsed.query, all_results, text)
    return text


@command("trust", "Trust an email sender: `!trust sender@example.com`")
async def cmd_trust(ctx: CommandContext):
    config, conn, user_id, args = ctx.config, ctx.conn, ctx.user_id, ctx.args
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
async def cmd_untrust(ctx: CommandContext):
    conn, user_id, args = ctx.conn, ctx.user_id, ctx.args
    email = args.strip().lower()
    if not email or "@" not in email:
        return "Usage: `!untrust sender@example.com`"

    removed = db.remove_trusted_sender(conn, user_id, email)
    if removed:
        return f"Removed `{email}` from trusted senders."
    return f"`{email}` is not in your trusted senders list. Note: senders in config files must be removed from the config."
