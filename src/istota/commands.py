"""!command dispatch system — synchronous commands intercepted before task queue."""

import dataclasses
import json
import logging
import re
import signal
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo


from typing import TYPE_CHECKING

from . import db
from .brain import (
    Brain,
    EFFORT_LEVELS,
    KNOWN_BRAIN_KINDS,
    effective_fallback_kind,
    make_brain,
    model_namespace_for_kind,
    resolve_brain_kind,
    room_selectable_kinds,
)
from .memory import search as memory_search_mod
from .process_group import kill_process_group
from .config import Config
# The static room-model table: a stdlib-only leaf importing nothing, so it
# costs this module — which is imported on the Talk polling path — nothing.
from .surfaces import is_room_member
# The cost-render rule, imported rather than copied. It already has two
# implementations — this one and `web/src/lib/usageFormat.ts` — pinned against
# each other by a parity test; a third, inside a surface, is exactly what those
# tests exist to prevent. `usage_render` is a stdlib-only leaf, so it costs this
# module (imported on the Talk polling path) nothing to take at import time.
from .usage_render import COST_PLACEHOLDER, fmt_int, render_cost

if TYPE_CHECKING:
    from .subscription_usage import Spend, UsageSnapshot, UsageWindow
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
    # The name the user actually typed, before alias resolution. Almost every
    # handler ignores it; `!confirm` reads it because `!yes` and `!no` are two
    # answers to one question and the alias table maps both onto one handler.
    invoked_as: str = ""
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

# Hidden command aliases: alias -> canonical command name. Resolved in
# `dispatch` but omitted from `!help` / autocomplete (which read `COMMANDS`).
# Read by name outside this module: `GET /chat/commands` serializes it as
# `command_aliases` so the web client can tell that `!inject` is a command
# without it reaching the popover (ISSUE-350). Renaming this needs that
# endpoint and `tests/test_web_chat_commands.py` changed with it.
_COMMAND_ALIASES: dict[str, str] = {
    "inject": "steer",
    # Answering a held task. `!confirm` is the documented spelling; these are
    # the words people actually type, and `!no` needs to reach the same handler
    # as `!yes` so a decline is not a different command to learn. `cmd_confirm`
    # reads `ctx.invoked_as` to tell them apart.
    "yes": "confirm",
    "y": "confirm",
    "approve": "confirm",
    "no": "confirm",
    "n": "confirm",
    "decline": "confirm",
    "reject": "confirm",
    # For the reader who wants only the plan windows and guesses a name for
    # them. It runs the same handler and does not filter the output — the token
    # totals directly above the windows are half of the answer to "how much
    # headroom is left", and splitting them would mean always typing both.
    "limits": "usage",
}

# Brain kinds that can actually be steered mid-flight today (`!steer`). A brain
# may *declare* `supports_steering` before its live wiring lands (tmux), so the
# command layer gates on this explicit v1 allowlist as well: flipping tmux on
# later is a one-line change once its paste path exists.
_STEERABLE_KINDS: frozenset[str] = frozenset({"native"})


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


# The `!model <alias> <prompt>` grammar, in one place. `is_model_prefix` is the
# half a caller can ask *before* building a brain, which matters on the Talk
# poll's per-message path: the brain is now the room's own, so constructing one
# per message would build (and never close) a provider client for every message
# in a native-pinned room, not only for the `!model` ones.
_MODEL_PREFIX_RE = re.compile(r"^!model\b\s*(\S+)?\s*(.*)", re.DOTALL | re.IGNORECASE)


def is_model_prefix(content: str) -> bool:
    """Whether ``content`` is a ``!model`` prefix at all, without needing a brain.

    Exactly the test ``parse_model_prefix`` makes before it touches the brain,
    so the two cannot disagree about what counts as a prefix.
    """
    return bool(_MODEL_PREFIX_RE.match((content or "").strip()))


@dataclass
class ModelPrefix:
    """Result of parsing a `!model` prefix.

    `unknown_alias` is set when the prefix matched but the alias didn't resolve
    (or no alias was supplied) — caller posts a usage message. An optional
    ``:effort`` modifier (`opus:high`, `smart:low`) is handled by the brain's
    ``resolve_alias``. Otherwise `model`/`effort` carry the override (both may be
    None for the explicit "default" alias) and `remainder` is the prompt with
    the prefix stripped.
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
    match = _MODEL_PREFIX_RE.match(stripped)
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


def brain_for_room(config: Config, conn, room_token: str, source_type: str):
    """The ``BrainConfig`` a new task in this room would run under.

    Reads ``rooms.brain`` for ``room_token`` — the *canonical* token, so a
    caller holding a surface-native ref resolves it first — and hands it to
    ``resolve_brain_kind`` as the override. The answer is the same one the
    executor will compute for the next task in this room, which is what makes it
    the right brain to resolve a model alias against: every writer of
    ``rooms.model`` and every surface that *offers* a model name goes through
    this, so a room can never be handed an id its own brain cannot resolve.

    Composition is ``make_brain(brain_for_room(...))`` at each call site — a
    consumer resolving an alias needs an instance, and this returns config.

    The per-user native API-key overlay the executor applies is deliberately
    **not** applied: this path resolves alias names and never reaches a
    provider, so a secrets lookup on the Talk poll's inner loop would buy
    nothing.

    Never raises. An unreadable room falls through to the source-type layer,
    which is the answer this returned before the column existed.

    ``resolve_brain_kind`` is **inside** the guard, not after it. `rooms.brain`
    is a `TEXT` column with no `CHECK`, SQLite is dynamically typed, and that
    function opens with `(override or "").strip()` — so a non-string value on
    the row raises `AttributeError` from a call that looks total. The one caller
    that most needs this never to raise is the Talk poll's per-message loop,
    which runs inside the batch transaction with nothing between it and the
    top: an exception there drops every remaining conversation's messages and
    rolls back the cursors, so the batch is re-read and re-aborts every cycle.
    `executor._native_web_fetch_enabled` guards the identical call for the same
    reason.
    """
    try:
        override = None
        if room_token:
            room = db.get_room(conn, room_token)
            if room is not None:
                override = getattr(room, "brain", None)
        return resolve_brain_kind(source_type, config.brain, override=override)
    except Exception:  # noqa: BLE001 — a room read must not raise into a poll loop
        logger.debug("brain_for_room: falling back to the source-type answer", exc_info=True)
        return resolve_brain_kind(source_type, config.brain)


def _model_namespace(brain_config) -> str | None:
    """The model namespace a resolved ``BrainConfig`` reads aliases in.

    ``None`` when the kind is not one that builds, which callers read as "not
    known" rather than as "the same namespace" — the safe direction for D5's
    clearing rule is to drop a pin whose portability could not be established.

    A **lookup** since ISSUE-417: `model_namespace` is a class attribute, and
    constructing a brain to read one is not free — `TmuxClaudeBrain.__init__`
    shells out to the installed `claude` and warns on a version mismatch, so
    running `!brain` on a tmux deployment emitted operator-facing noise about a
    question that never needed an instance. The `try` stays because this is
    reached from the Talk poll loop, but `model_namespace_for_kind` does not
    raise; it is a belt for a caller passing something odd.
    """
    try:
        return model_namespace_for_kind(getattr(brain_config, "kind", None))
    except Exception:  # noqa: BLE001 — an unbuildable kind is an unknown namespace
        logger.debug("model namespace lookup failed", exc_info=True)
        return None


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
    # Resolve hidden aliases (e.g. `!inject` -> `steer`) to the canonical name.
    # The typed name is kept — `!yes` and `!no` share one handler and it needs
    # to know which was said.
    invoked_as = cmd_name
    cmd_name = _COMMAND_ALIASES.get(cmd_name, cmd_name)
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
            invoked_as=invoked_as,
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
    lines = ["**Available commands:**", ""]
    for name, (_, help_text) in sorted(COMMANDS.items()):
        lines.append(f"- `!{name}` -- {help_text}")
    lines.append("")
    lines.append("**Per-task model override:**")
    lines.append("")
    # The room's own brain: `!help` offers model names, which D5 Rule 2 binds
    # exactly as it binds the writers. Advertising `opus` in a room whose brain
    # resolves no such alias is the same defect as writing it, one step earlier.
    aliases = [alias for alias, _m, _e in make_brain(_ctx_brain(ctx)).list_aliases()]
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
        # The whole group, not the pid alone: the CLI's children are where the
        # work is, and a bare kill leaves them running after the user has
        # visibly stopped the task (ISSUE-257). kill_process_group falls back
        # to the single process when the pid leads no group of its own, so a
        # recorded pid that is somebody else's child can never resolve to their
        # group. Both of today's writers record leaders, tmux panes included, so
        # a tmux `!stop` now takes the pane's whole command tree.
        kill_process_group(pid_row["worker_pid"], signal.SIGTERM)

    preview = prompt[:80] + "..." if len(prompt) > 80 else prompt
    return f"Cancelling task #{task_id}: {preview}"


# `!confirm` verbs, keyed by the alias the user typed. A bare `!confirm` is an
# approval; the decline spellings are listed so `!no` reaches the same handler.
_DECLINE_ALIASES = frozenset({"no", "n", "decline", "reject"})
# Trailing words a user may append instead of using an alias: `!confirm 41 no`.
_VERB_WORDS = {
    "yes": "approve", "y": "approve", "ok": "approve", "approve": "approve",
    "confirm": "approve", "trust": "trust",
    "no": "decline", "n": "decline", "cancel": "decline", "decline": "decline",
    "reject": "decline",
}


@command(
    "confirm",
    "Answer a held task: `!confirm`, `!confirm <task-id>`, `!confirm <id> no`, "
    "`!confirm <id> trust`. `!yes` / `!no` are shorthands",
)
async def cmd_confirm(ctx: CommandContext):
    """Approve or decline a task parked in `pending_confirmation`, from any surface.

    This is the surface-agnostic half of the confirmation loop (ISSUE-241).
    Talk had one — a plain "yes" intercepted by `handle_confirmation_reply` —
    and no other surface had any, so a gated inbound email was unanswerable
    from web chat: typing "yes" there just started a new task.

    A bare invocation acts only when exactly **one** question is open. With
    several it lists them and does nothing: the held task is untrusted inbound
    mail, and approving the wrong one is precisely the misfire the gate exists
    to prevent.
    """
    from . import confirmations

    conn, user_id = ctx.conn, ctx.user_id
    words = ctx.args.split()

    target_id: int | None = None
    # `isdecimal`, not `isdigit`: the latter is True for '²', which `int()` then
    # refuses, turning a typo into a traceback instead of the usage message.
    if words and words[0].lstrip("#").isdecimal():
        target_id = int(words.pop(0).lstrip("#"))

    # Contradictions are refused, not resolved. `verb = mapped` last-word-wins
    # made `!no 41 trust` approve *and* trust — resolving an ambiguous input
    # towards approval, on a gate whose whole job is holding untrusted mail.
    spoken = "decline" if ctx.invoked_as in _DECLINE_ALIASES else None
    for word in words:
        mapped = _VERB_WORDS.get(word.lower())
        if mapped is None:
            return (
                f"Don't know what `{word}` means here. Try `!confirm <task-id>`, "
                "`!confirm <task-id> no`, or `!confirm <task-id> trust`."
            )
        if spoken is not None and mapped != spoken:
            return (
                f"`!{ctx.invoked_as} … {word}` says two different things. "
                "Use `!confirm <task-id>` to approve, `!confirm <task-id> no` to "
                "discard, or `!confirm <task-id> trust` to approve and trust the "
                "sender."
            )
        spoken = mapped
    verb = spoken or "approve"

    pending = confirmations.pending_for_user(conn, user_id)
    if not pending:
        return "Nothing is waiting for your confirmation."

    if target_id is not None:
        task = next((t for t in pending if t.id == target_id), None)
        if task is None:
            # Deliberately one message for "no such task", "not yours" and
            # "already answered": the command must not become an oracle for
            # which task ids exist, and the three are the same to the user.
            return (
                f"Task #{target_id} isn't waiting for your confirmation. "
                f"Open right now:\n{confirmations.format_listing(conn, pending)}"
            )
    elif len(pending) == 1:
        task = pending[0]
    else:
        # Nothing decided, so nothing recorded — same rule as the bare-answer
        # path on both surfaces.
        return confirmations.ambiguity_listing(conn, pending)

    # The wording stays addressed — `#id` and a label — rather than adopting the
    # bare "Confirmed." a natural-language answer gets. This command exists to
    # answer a *named* question, most often one of several, so saying which one
    # was answered is the whole point of having typed it.
    label = confirmations.describe(conn, task)
    if verb == "decline":
        confirmations.decline(conn, task, by=ctx.surface)
        return _record_confirm_exchange(
            ctx, f"Declined #{task.id} — {label}. Nothing was run.",
        )

    trusted = confirmations.approve(
        conn, task, trust_sender=(verb == "trust"), config=ctx.config,
        by=ctx.surface,
    )
    if trusted:
        return _record_confirm_exchange(
            ctx,
            f"Confirmed #{task.id} — {label}. Future mail from this sender is "
            "processed without asking.",
        )
    # Deliberately keyed on what happened, not on what was asked: `trust` on a
    # task with no recorded sender trusts nobody, and claiming otherwise would
    # leave the user believing a control is in place that isn't.
    return _record_confirm_exchange(
        ctx, f"Confirmed #{task.id} — {label}. Running it now.",
    )


# Surfaces whose `role='user'` rows the web transcript actually renders
# (`TRANSCRIPT_SURFACE_FILTER`). Recording an answer from anywhere else stores
# an invisible question above a visible ack, which is worse than storing
# nothing — a CLI or REPL `!confirm` therefore leaves no transcript row.
#
# Imported rather than copied: this was a hand copy of `db`'s tuple, and the
# two had to be read side by side to be seen as one thing. It is **not**
# `surfaces.is_room_member`, and the difference is exactly email: the question
# is "may this surface deposit a `role='user'` row at all" (member plus guest),
# not "does this surface own rooms". The set happening to equal the room-role
# set plus the one guest is a coincidence, and its domain is `source_type`
# values rather than surface names — see the declaration in `db.py`.
_TRANSCRIPT_SURFACES = db.TRANSCRIPT_SURFACES


def _record_confirm_exchange(ctx: CommandContext, reply: str) -> "CommandResult":
    """Commit the answer and leave it in the room transcript.

    A confirmation is an authorization decision, so the exchange is durable
    however it was given — typed as `!confirm` here, or as a bare "yes" through
    `confirmations.parse_answer` on either surface. Without this the two ways
    of answering the same question leave different records, and the answer is
    gone on the next reload. Usage errors ("nothing is waiting", an unknown id)
    and the ambiguity listing deliberately do not record: neither decides
    anything.

    The message ids ride back on `result_data` under the same
    `confirmation_answered` kind the bare-answer path uses, because the web
    client stamps them onto the rows it has already drawn. Without that, the
    room stream's echo of the two stored rows appends a second copy of each —
    they carry no task id, so `msg_id` is the only dedup key available.
    """
    from . import confirmations

    # **Deliberately not `surfaces.is_room_member`**, unlike the `!steer` and
    # `!retry` writes in this same file. The question here is "may this surface
    # deposit a `role='user'` row in a room at all", which admits email — a
    # `guest` that joins a room's transcript without owning one. Reading the
    # ownership predicate would return False for email and stop an email
    # `!confirm` recording the exchange this function's docstring calls a
    # durable authorization record.
    if ctx.surface not in _TRANSCRIPT_SURFACES:
        ctx.conn.commit()
        return CommandResult(handled=True, text=reply)

    room_token = (
        db.resolve_room_token(ctx.conn, ctx.surface, ctx.conversation_token)
        or ctx.conversation_token
    )
    user_msg_id, system_msg_id = confirmations.record_exchange(
        ctx.conn, room_token,
        answer_text=f"!{ctx.invoked_as} {ctx.args}".strip(),
        ack=reply, origin_surface=ctx.surface, answered_by=ctx.user_id,
    )
    ctx.conn.commit()
    return CommandResult(
        handled=True,
        text=reply,
        data={
            "kind": "confirmation_answered",
            "user_msg_id": user_msg_id,
            "system_msg_id": system_msg_id,
        },
    )


# Max steers that may sit `pending` (undrained) on one task at once. A steer is
# cheaper than a task and rate-limiting it defeats rapid course-correction, so
# we cap *depth* rather than throttle per minute (see the !steer spec).
_MAX_PENDING_STEERS = 10


def _steer_refusal(kind: str) -> str:
    """The graceful refusal for a task whose brain can't be steered in v1."""
    if kind == "claude_code":
        return (
            "This task is running under the headless Claude Code brain, which "
            "can't be steered mid-flight. Use `!stop` to cancel, or wait for it "
            "to finish and reply normally."
        )
    if kind == "tmux_claude":
        return (
            "This task is running under the tmux brain, which isn't steerable "
            "yet. Use `!stop` to cancel, or wait for it to finish and reply "
            "normally."
        )
    return (
        f"This task's brain (`{kind}`) can't be steered mid-flight. Use `!stop` "
        "to cancel, or wait for it to finish and reply normally."
    )


@command(
    "steer",
    "Steer your running task: `!steer <text>` — delivered to the model mid-run "
    "(doesn't restart it; `!stop` cancels)",
)
async def cmd_steer(ctx: CommandContext):
    config, conn, user_id, args = ctx.config, ctx.conn, ctx.user_id, ctx.args

    text = args.strip()
    if not text:
        return "Usage: `!steer <text>` — send a note to your running task without restarting it."

    # Steering is room-scoped: you steer the task you're watching. Resolve the
    # canonical room token (a per-surface ref maps to it) so it matches the
    # task's stored conversation_token.
    room_token = db.resolve_room_token(conn, ctx.surface, ctx.conversation_token) \
        or ctx.conversation_token

    # The user's most recent running/locked task in *this* room. pending_confirmation
    # is excluded — answer those with a normal reply, not a steer.
    row = conn.execute(
        """
        SELECT id, source_type, brain FROM tasks
        WHERE user_id = ? AND conversation_token = ? AND status IN ('running', 'locked')
        ORDER BY created_at DESC LIMIT 1
        """,
        (user_id, room_token),
    ).fetchone()
    if not row:
        return "No running task in this room to steer."
    task_id, source_type = row["id"], row["source_type"]

    # Steerability. Resolve the brain this task actually runs under — its own
    # pinned kind first (`tasks.brain`, filled from the room when the task was
    # created), then `source_type_overrides` — and gate on the v1 allowlist, since
    # a brain may declare `supports_steering` before its live wiring exists
    # (tmux). Reading the task's column rather than the deployment default is
    # what makes the refusal name the brain this room actually runs.
    #
    # Not a one-way improvement, and the residue is recorded rather than implied.
    # Three states leave the row disagreeing with what is actually running: the
    # availability breaker skipped the primary, a reroute replaced it
    # mid-attempt, or the operator *widened* `room_selectable` after the task
    # started, so this resolution admits a pin the executor refused at dispatch.
    # The first two are reachable without this column — nothing today records the
    # brain that actually ran on a *live* task row — and the third is the price
    # of resolving a stored value at read time rather than freezing the answer.
    resolved_kind = resolve_brain_kind(
        source_type, config.brain, override=row["brain"],
    ).kind
    if resolved_kind not in _STEERABLE_KINDS:
        return _steer_refusal(resolved_kind)

    # Depth cap (not a rate limit).
    pending = db.count_pending_steers(conn, task_id)
    if pending >= _MAX_PENDING_STEERS:
        return (
            f"Too many pending steers ({pending}) on task #{task_id} — let it "
            "catch up before sending more."
        )

    # Write the steer (its own committed transaction, like `!stop`'s flag flip).
    db.add_task_steer(conn, task_id, text, user_id, ctx.surface)

    # Record the steer durably in the room transcript so it shows in time order
    # (between the prompt and the eventual result) after a reload. task_id is
    # left NULL: the unique (room, role, task_id) index reserves the per-task user
    # slot for the original prompt, and LLM-context reconstruction joins on
    # task_id — so a NULL-task_id row is display-only, never re-paired as a
    # phantom turn. Room surfaces only; best-effort.
    #
    # The ownership question, unlike `_record_confirm_exchange`'s gate a few
    # hundred lines up: this write needs a room the surface owns to write into,
    # so email — a `guest` — is out, and was before the predicate had a name.
    if is_room_member(ctx.surface):
        try:
            if db.get_room(conn, room_token) is not None:
                msg_id = db.add_message(
                    conn, room_token, role="user", body=text,
                    origin_surface=ctx.surface, task_id=None,
                    # A steer is a `task_id IS NULL` row like a confirmation
                    # answer, so nothing downstream can recover who typed it —
                    # and in a shared room the answer is not always the reader.
                    author_user_id=user_id,
                )
                conn.commit()
                # The id rides back for the same reason `!confirm`'s does: the
                # web client has already drawn its own row for what was typed,
                # and this stored row echoes over the room stream carrying no
                # task id — so `msg_id` is the only dedup key `appendStreamedRow`
                # has, and without the stamp the steer appears twice. The body
                # goes with it because the two differ: the client drew the whole
                # `!steer <note>` line, while what is stored (and what a reload
                # shows) is the note alone.
                ctx.result_data = {
                    "kind": "steer_recorded",
                    "user_msg_id": msg_id,
                    "body": text,
                }
        except Exception:
            logger.debug("steer transcript write failed", exc_info=True)

    # Surface the steer on the running task's live event log so a reconnecting
    # SSE client replays it and the live view shows it landed before the model
    # reacts. Best-effort — the real feedback is the model shifting in the stream.
    try:
        preview = text if len(text) <= 120 else text[:117] + "…"
        db.append_task_event(
            conn, task_id, "progress_text", {"text": f"↪️ Steering: {preview}"},
        )
    except Exception:
        logger.debug("steer event frame failed", exc_info=True)

    return f"Steering task #{task_id} — your note will reach the model at its next step."


def _room_token(ctx: CommandContext) -> str:
    """This context's canonical room token.

    The rooms registry is keyed by canonical token, so a per-surface ref has to
    be mapped before anything reads a room's standing defaults.
    """
    return (
        db.resolve_room_token(ctx.conn, ctx.surface, ctx.conversation_token)
        or ctx.conversation_token
    )


def _ctx_brain(ctx: CommandContext):
    """The brain a new task from this context would run under.

    ``ctx.surface`` stands in for the task's ``source_type``: for the two room
    surfaces the two strings are the same value (``record_inbound`` is handed
    ``source_type="talk"`` / ``"web"`` from the same surface), and a surface with
    no room has no room brain to find either way.
    """
    return brain_for_room(ctx.config, ctx.conn, _room_token(ctx), ctx.surface)


@command("models", "List available model aliases (and what they resolve to)")
async def cmd_models(ctx: CommandContext):
    lines = ["**Model aliases**", "", "Use `!model <alias> <prompt>` to override the model for a single task.", ""]
    # The room's own brain, not the deployment default: offering a room names it
    # cannot run is the same defect as writing one, one step earlier.
    for alias, model, effort in make_brain(_ctx_brain(ctx)).list_aliases():
        if model is None:
            target = "(no override — use default)"
        elif effort:
            target = f"`{model}` + effort `{effort}`"
        else:
            target = f"`{model}`"
        lines.append(f"- `{alias}` → {target}")
    lines.append("")
    lines.append(
        "Append `:low|:medium|:high|:xhigh|:max` to any name to set effort, "
        "e.g. `!model opus:high`."
    )
    return "\n".join(lines)


# Effort levels the CLI brains accept — the set is the single source of truth in
# ``brain._aliases.EFFORT_LEVELS`` (imported at top); this ordered tuple is just
# the display order for usage messages. `!room effort <level>` validates against
# it; the brain silently drops effort for models that don't support it.
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
assert set(_EFFORT_LEVELS) == set(EFFORT_LEVELS), (
    "commands._EFFORT_LEVELS drifted from brain._aliases.EFFORT_LEVELS"
)


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


def _outgoing_namespace(config: Config, source_type: str, outgoing: str) -> str | None:
    """The namespace `rooms.model` was resolved in, inferred from the room's kind.

    **The fallback, not the answer.** Since ISSUE-420 the namespace is recorded
    beside the model when it is written, and `_clear_pin_across_namespaces`
    prefers that fact; this runs only for a row written before that column
    existed, where the kind is the only evidence there is.

    Deliberately **not** routed through `resolve_brain_kind`. That function
    refuses an override the operator has since dropped from the allowlist, so
    routing the outgoing kind through it answers with the namespace of the
    *deployment default*.

    The worked example this docstring used to give for that — a room pinned to
    `native`, dropped from the allowlist, then moved to `claude_code` — is no
    longer a case this function decides, and it was the wrong way round. Where
    the pin was refused, `brain_for_room` resolved the alias against the lane,
    so the stored id is `anthropic` and keeping it is correct; inferring
    `openai_compat` from the kind is what would have thrown it away. Recording
    the namespace is what settles it, because the two writes — one made while
    the kind was admitted, one after — leave identical rows. What this function
    still answers correctly is the case it was really built for: a pin written
    while the kind *was* admitted, which is genuinely in that kind's namespace
    and which `resolve_brain_kind`'s refusal would misreport.

    Only an *unknown* kind yields None, which the caller reads as "not
    established" and therefore clears. Since ISSUE-417 this is a lookup, so a
    known kind that this host cannot currently *build* — a misconfigured
    `[brain.native]`, a missing `claude` — still answers with its namespace and
    a same-namespace pin is kept where it used to be cleared. That narrowing is
    deliberate: a namespace is a property of the kind rather than of this host's
    ability to construct it, and clearing a pin that is still valid costs the
    user their setting for a reason that has nothing to do with portability.
    """
    kind = (outgoing or "").strip()
    if not kind:
        return _model_namespace(resolve_brain_kind(source_type, config.brain))
    if kind not in KNOWN_BRAIN_KINDS:
        return None
    return _model_namespace(dataclasses.replace(config.brain, kind=kind))


def _room_pin_admitted(config: Config, pinned: str) -> bool:
    """Whether ``resolve_brain_kind`` would admit this room's stored kind.

    Exactly its two refusal branches, and `room_selectable_kinds` already
    intersects against the buildable kinds, so this is the whole predicate. It
    is asked separately from "what is it running" because the two answers
    coincide by accident in one case that matters: a room pinned to the kind
    that is *already* the instance default, whose pin the operator has since
    dropped from the allowlist, runs that kind either way — but it is not
    pinned, so it still has failover.
    """
    return bool(pinned) and pinned in room_selectable_kinds(config.brain)


def _describe_room_brain(config: Config, room, surface: str) -> str:
    """One line for `!room`'s show branch: which brain this room runs.

    Deliberately short — `!brain` is where the whole resolution chain is
    explained. What this has to get right is the refused case: a room whose
    stored kind the operator has since dropped from `[brain] room_selectable`
    keeps the column but runs something else, and a line that only echoed the
    column would say the opposite of what happens.
    """
    pinned = (getattr(room, "brain", None) or "").strip()
    running = resolve_brain_kind(surface, config.brain, override=pinned or None).kind
    if not pinned:
        return f"Brain: `{running}` (inherited — set one with `!brain <kind>`)."
    if not _room_pin_admitted(config, pinned):
        return (
            f"Brain: this room is set to `{pinned}`, which the operator no "
            f"longer allows; it is running `{running}`."
        )
    return f"Brain: `{pinned}` (pinned to this room, so it does not fall back)."


def _describe_failover(config: Config, surface: str, pinned: str) -> str:
    """The failover line, for both `!brain` forms.

    A pinned room has none, by design (D12): the room named *this* brain, so a
    task that cannot run on it fails with the primary's own reason rather than
    answering from a different model. Worth saying plainly at the moment of
    choosing and at every subsequent read, because it is a real cost — a
    deployment that leans on `[brain] fallback` to absorb a usage limit loses
    that absorption in a pinned room.

    Note what a pinned room does *not* lose: the availability breaker still
    opens on a bad result and the operator is still alerted. What the pin
    removes is the reroute, so a task through an outage makes one doomed attempt
    rather than being skipped straight to a substitute that does not exist.
    """
    if pinned:
        return (
            f"Failover: none — this room is pinned to `{pinned}`, so a turn it "
            "cannot run fails with the real reason instead of answering from "
            "another brain. `!brain default` restores the inherited setting."
        )
    target = effective_fallback_kind(resolve_brain_kind(surface, config.brain))
    if target:
        return f"Failover: `{target}`, if the brain above is unavailable."
    return "Failover: none configured for this deployment."


# `!brain <kind>` is the room's own choice of brain, and it is the one command
# here that changes an *enforcement* posture rather than a preference: the CLI
# brains run the whole agent inside bubblewrap, while `native` runs the agent
# loop in the daemon process with its file confinement enforced in Python. So
# writing it is admin-gated and the kinds on offer are an operator allowlist,
# empty by default. Reading is never gated — every member of a shared room is
# entitled to know what their turns run under.
@command(
    "brain",
    "Show or set this room's brain: `!brain`, `!brain <kind>`, `!brain default` "
    "(admin-only to set; the operator chooses which kinds are offered)",
)
async def cmd_brain(ctx: CommandContext):
    config, conn, args = ctx.config, ctx.conn, ctx.args
    token = _room_token(ctx)
    room = db.get_room(conn, token)
    if room is None:
        return (
            "This room isn't registered yet — send a message first, then set "
            "its brain."
        )

    wanted = args.strip().lower()
    pinned = (room.brain or "").strip()

    if not wanted:
        running = resolve_brain_kind(
            ctx.surface, config.brain, override=pinned or None,
        ).kind
        by_lane = (config.brain.source_type_overrides or {}).get(ctx.surface)
        lines = [
            f"**Brain** — running `{running}` in this room", "",
            f"- This room: {f'`{pinned}`' if pinned else 'not set (inherits below)'}",
            f"- Rule for `{ctx.surface}` turns: "
            + (f"`{by_lane}`" if by_lane else "none"),
            f"- Instance default: `{config.brain.kind}`",
        ]
        admitted = _room_pin_admitted(config, pinned)
        if pinned and not admitted:
            lines.append(
                f"- This room's `{pinned}` is not in the operator's list of "
                "selectable brains, so it is being ignored."
            )
        lines += ["", _describe_failover(config, ctx.surface, pinned if admitted else "")]
        return "\n".join(lines)

    if not config.is_admin(ctx.user_id):
        return "Only an admin can change this room's brain. `!brain` shows what it runs."

    # `default` is checked ahead of the allowlist deliberately. Clearing is a
    # narrowing, so it needs no entry — and emptying `[brain] room_selectable`
    # is the documented way to switch the feature off, which is exactly how a
    # room ends up holding a pin nothing will honour. Gating the clear behind
    # the same check leaves that pin in place for good, with `!room` reporting
    # it as ignored on every read and no command able to remove it.
    if wanted == "default":
        if not pinned:
            return "This room already inherits its brain. Nothing to clear."
        cleared = _clear_pin_across_namespaces(
            config, conn, token, room,
            source_type=ctx.surface, outgoing=pinned, incoming=None,
        )
        db.set_room_brain(conn, token, None)
        running = resolve_brain_kind(ctx.surface, config.brain).kind
        return "\n".join([
            f"Room brain cleared — this room now inherits `{running}`.",
            *cleared,
            _describe_failover(config, ctx.surface, ""),
        ])

    selectable = room_selectable_kinds(config.brain)
    if not selectable:
        return (
            "Room brain selection is switched off on this deployment — the "
            "operator has not listed any kinds under `[brain] room_selectable`."
        )

    offered = ", ".join(f"`{k}`" for k in sorted(selectable))
    if wanted not in selectable:
        return (
            f"`{wanted}` isn't available for this room. "
            f"Selectable brains: {offered}. Use `default` to clear."
        )

    cleared = _clear_pin_across_namespaces(
        config, conn, token, room,
        source_type=ctx.surface, outgoing=pinned, incoming=wanted,
    )
    db.set_room_brain(conn, token, wanted)
    return "\n".join([
        f"Room brain set to `{wanted}` — every turn here now runs it.",
        *cleared,
        _describe_failover(config, ctx.surface, wanted),
    ])


def _clear_pin_across_namespaces(
    config: Config, conn, token: str, room, *,
    source_type: str, outgoing: str, incoming: str | None,
) -> list[str]:
    """D5 Rule 1: drop the room's model pin when the brain changes namespace.

    `rooms.model` holds a canonical id resolved in one brain's namespace, and
    the room does not store the alias that produced it — so an id that crossed
    from `anthropic` to `openai_compat` is not re-resolvable, only unrunnable.
    A move *within* a namespace (`claude_code` ⇄ `tmux_claude`) keeps the pin,
    which is correct: the same id runs under both.

    Returns the lines to add to the reply, empty when nothing was cleared, so
    the caller says what it did rather than doing it silently. Writes nothing
    when there is no pin to lose.

    Clears when either namespace could not be determined. That is the safe
    direction here and the opposite of this module's usual "behave as before":
    leaving a pin whose portability is unknown is the failure being prevented,
    while a cleared pin costs one command to set again.
    """
    if not room.model:
        # With no model pin there is nothing namespaced to lose: an effort level
        # on its own is a semantic rung every brain reads, so it survives the
        # change. This is the *only* case in which effort survives — below, it
        # goes with the model, because the two were set as a pair.
        return []
    # The recorded fact first, and the inference only for a row that predates
    # the column — the same preference `executor._pin_origin_namespace` applies,
    # and it has to be applied here too or the two readers of one row disagree
    # (ISSUE-420). `_outgoing_namespace` reads the *kind* the room named, which
    # is the origin only where that kind was admitted when the model was
    # written; where it was refused, `brain_for_room` resolved the alias in the
    # lane's namespace and `model_namespace` says so. Getting this wrong clears
    # a pin that is about to move within one namespace, and tells the user their
    # id belonged to the outgoing brain when it never did.
    before = (
        (getattr(room, "model_namespace", None) or "").strip()
        or _outgoing_namespace(config, source_type, outgoing)
    )
    after = _model_namespace(
        resolve_brain_kind(source_type, config.brain, override=incoming or None)
    )
    if before is not None and after is not None and before == after:
        return []
    logger.info(
        "room %s: clearing model pin %r across a brain namespace change "
        "(%s -> %s)", token, room.model, before, after,
    )
    db.set_room_model_effort(conn, token, None, None)
    dropped = f"model default (`{room.model}`)"
    if room.effort:
        # Named too, because `set_room_model_effort` moves the pair and a reply
        # that mentioned only the model would under-report what it just did.
        dropped += f" and effort (`{room.effort}`)"
    return [
        f"Its {dropped} was cleared — that id belongs to the previous brain's "
        "namespace. Set a new one with `!room model <alias>`."
    ]


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
    token = _room_token(ctx)
    room = db.get_room(conn, token)
    if room is None:
        return "This room isn't registered yet — send a message first, then set its default."

    parts = args.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if not sub:
        # Read-only on the brain: `!brain` is the single writer, and this line
        # exists so somebody reading a room's settings sees which brain the
        # model figures below were resolved against.
        return _describe_room_default(room.model, room.effort) + "\n" + \
            _describe_room_brain(config, room, ctx.surface)

    if sub == "model":
        alias = rest.lower()
        # Resolved through the *room's* brain. A room pinned to a brain in
        # another model namespace must not have an id it cannot run written into
        # its standing default — that is not one bad turn, it is every turn in
        # the room until somebody notices.
        room_brain = make_brain(brain_for_room(config, conn, token, ctx.surface))
        # The namespace to record beside whatever this write stores. Taken from
        # the brain the alias is *actually* resolved against, which is not the
        # same as `room.brain`: `brain_for_room` refuses a kind the operator has
        # dropped from `[brain] room_selectable` and falls through to the lane,
        # so after such a change the id below belongs to the lane's namespace
        # while the column still names the refused kind (ISSUE-420).
        room_namespace = getattr(room_brain, "model_namespace", None)
        aliases = [a for a, _m, _e in room_brain.list_aliases()]
        if not alias:
            return (
                "Usage: `!room model <alias>` (or `default` to clear). "
                f"Aliases: {', '.join(f'`{a}`' for a in aliases)}."
            )
        if alias == "default":
            # The reserved `default` keyword always clears the room's model +
            # effort, independent of any operator override that may have given
            # the `default` alias a concrete resolution.
            db.set_room_model_effort(conn, token, None, None)
            return "Room model reset — this room now uses the instance default."
        resolved = room_brain.resolve_alias(alias)
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
            db.set_room_model_effort(
                conn, token, model, effort, namespace=room_namespace,
            )
        else:
            # A plain model alias leaves any separately-set `!room effort` intact
            # (the two knobs are orthogonal).
            db.set_room_model(conn, token, model, namespace=room_namespace)
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

    # `[confirm?]` used to be a dead end — the row said a task was waiting on
    # the user and nothing here said how to answer it (ISSUE-241).
    if any(r["status"] == "pending_confirmation" for r in rows):
        lines.append("")
        lines.append("Answer a `[confirm?]` with `!confirm <task-id>` or `!confirm <task-id> no`.")

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


# =============================================================================
# !usage command
# =============================================================================

# The 20-character ASCII bar the removed `!usage` drew, kept as it was: it reads
# well in a chat client, needs no markup, and is the one thing the admin card
# cannot do as compactly.
_USAGE_BAR_WIDTH = 20

# A reading younger than this gets no age footer; past it, the reply says how
# old the number is. Note what this does *not* mean: the module's default cache
# TTL is 1800s and a warm cache is shared with the dashboard and the doctor
# sweep, so a reading the module considers perfectly current is routinely one to
# thirty minutes old and the footer is therefore common rather than exceptional.
# That is the intent — the alternative reading of "only when something is wrong"
# would leave an admin unable to tell a live percentage from a half-hour-old
# one, which is the pair that matters when deciding whether to start a long run.
# What the threshold buys is only that a reply rendered off a fetch this second
# does not carry a pointless "0s old".
_USAGE_FRESH_SECONDS = 60

# The `user_id` filter for a non-admin whose id is empty. `task_usage.user_id`
# is `TEXT NOT NULL` and every writer stamps a real id, so this matches nothing
# — which is the right answer for a caller we cannot attribute, and the opposite
# of what passing the empty string through would do.
_NO_SUCH_USER = "\x00"


@command("usage", "Show token usage, and plan limits on a subscription")
async def cmd_usage(ctx: CommandContext):
    """Token and cost totals, plus the Claude Code plan windows for an admin.

    Deliberately the same report as `istota usage` (`cli.cmd_usage`) and the
    dashboard's Token usage card, with one section they do not have — which is
    why the handler shares a name with the CLI's rather than apologising for it.

    **Three sections, gated additively**, exactly as `cmd_status` above appends
    its `**System:**` block: everyone sees their own token totals, an admin sees
    the fleet's, the by-brain split and the plan. No `_ADMIN_ONLY` set, no
    `dispatch` gate, no `!help` filter and no catalogue filter — the command is
    useful to everyone and only its account-wide parts are withheld. A non-admin
    is told nothing about what is missing, the way `!status` simply omits its
    system line.

    **Section 3 is gated on whether a reading is available, not on
    `config.brain.kind`.** `source_type_overrides` and `[brain] fallback` between
    them make the configured brain a poor proxy for "does this deployment burn
    the subscription" — a `native` deployment with a `claude_code` fallback is
    the case that most needs the number, and a `claude_code` one with an override
    sending scheduled work elsewhere still burns it. A credential the endpoint
    answers for is direct evidence instead. With no reading the section is
    omitted silently; a chat reply is not where an unreachable diagnostic
    endpoint gets reported, and `runtime.subscription_usage` says so properly.

    This handler renders and nothing else. The credential, the request, the parse
    and the deployment-wide cache all live in `subscription_usage`, so `!usage`,
    the admin card and the doctor check share one reading and one parser — the
    restored command is not the one that was deleted, which carried its own HTTP
    call, its own token reader and its own formatter.
    """
    import asyncio

    config, user_id = ctx.config, ctx.user_id
    is_admin = config.is_admin(user_id)

    # Commit the caller's transaction before blocking, the same way `cmd_drafts`
    # does below and for the same reason. The Talk poller wraps its whole batch
    # in one transaction and hands `dispatch` that connection, already mid-write
    # (the message cache upsert, the room membership touch) by the time a
    # `!command` runs. Everything this handler then does is blocking and some of
    # it is a network round trip bounded by `subscription_usage_timeout_seconds`,
    # so leaving the poll's write lock held across it stalls every other writer
    # in the daemon — scheduler, workers, web — on their busy timeout, for a
    # command that only reads. Nothing here writes, so unlike the drafts case
    # there is no durability question, only the lock.
    if ctx.conn is not None:
        try:
            ctx.conn.commit()
        except sqlite3.Error:
            # A connection we could not commit is the caller's problem, not a
            # reason to refuse a read-only report.
            logger.debug("!usage could not commit the caller's transaction", exc_info=True)

    # Both halves block — SQLite queries, and on a cache miss an HTTPS GET — and
    # this coroutine runs on the loop that polls every Talk conversation. Hence
    # `to_thread`. This comment used to name `cmd_check`'s bare
    # `subprocess.run` as the counterexample; that handler now does both of
    # these, so the precedent runs the other way.
    lines = await asyncio.to_thread(_usage_token_sections, config, user_id, is_admin)

    if is_admin:
        # Imported here, not at module scope: `commands` is imported on the Talk
        # polling path and `subscription_usage` pulls in urllib and subprocess.
        from . import subscription_usage

        # One clock for the fetch, the cache freshness and the age footer.
        now = time.time()
        snapshot = await asyncio.to_thread(
            subscription_usage.get_snapshot, config, now_ts=now
        )
        # `ctx.conn` on the loop thread, where this runs — the resolver reads
        # the live profile row and should reuse the connection already open
        # rather than making its own.
        lines += _usage_plan_section(snapshot, config, user_id, now, ctx.conn)

    return "\n".join(lines)


def _usage_token_sections(config: Config, user_id: str, is_admin: bool) -> list[str]:
    """Sections 1 and 2, from `task_usage`. Blocking — call it in a thread.

    Opens its own connection rather than taking the handler's: this runs in a
    worker thread, and a sqlite3 connection refuses to be used from any thread
    but the one that made it, which in `dispatch` is the event loop's.
    `outbound_drafts.release` reaches the database from `!drafts` the same way.
    """
    # `db.iso_utc_days_ago`, not a local one: it sits beside
    # `db.sql_datetime_days_ago` precisely so a caller picks between the two
    # date formats by name. `' '` sorts below `'T'`, so the wrong one against
    # `task_usage.created_at` is silently wrong rather than an error.
    day_since = db.iso_utc_days_ago(1)
    month_since = db.iso_utc_days_ago(30)
    # A non-admin is filtered to their own rows and learns nothing about anyone
    # else's consumption; an admin gets the fleet. `None` is the *only* way to
    # ask for the fleet, so the non-admin branch must never produce a value
    # `db._usage_filters` would treat as absent — it gates on truthiness, and an
    # empty `user_id` would silently drop the WHERE clause and hand a member the
    # whole deployment's totals. No caller passes one today; this is one
    # expression rather than a promise about every future transport.
    scope = None if is_admin else (user_id or _NO_SUCH_USER)

    try:
        with db.get_db(config.db_path) as conn:
            day = db.usage_summary(conn, since=day_since, user_id=scope)
            month = db.usage_summary(conn, since=month_since, user_id=scope)
            brains = (
                db.usage_summary(conn, since=month_since, group_by="brain")
                if is_admin
                else []
            )
    except sqlite3.OperationalError as exc:
        # The table is named, and named with a word boundary. "no such table"
        # alone would catch a *different* missing table and report it as this
        # one — a real fault dressed up as a fresh deployment, with a remedy
        # that fixes nothing. A bare substring test is not enough either: the
        # child table `task_usage_models` contains this one's name, and `\b`
        # does not fire between `e` and `_`, which is exactly why it is here.
        if not re.search(r"no such table: task_usage\b", str(exc)):
            raise
        # A fresh deployment, not a fault. `db.get_db` only connects — it is
        # `init_db` that runs `schema.sql` — so the remedy names something that
        # actually creates the table rather than telling the reader to try again
        # and get the same line forever. `istota usage` says the same words.
        # Guarded because `dispatch` would otherwise put "no such table:
        # task_usage" into a chat room, and because section 3 is still worth
        # rendering underneath.
        return [
            "**Token usage**",
            "",
            "No usage data yet — the `task_usage` table is created when the "
            "database is initialized. Restart the daemon, or run `istota init`.",
        ]

    lines = [f"**Token usage** — {'fleet' if is_admin else user_id}", ""]
    if not day["rows"] and not month["rows"]:
        # Not a row of zeroes, which reads as a measured nothing. `istota usage`
        # declines the same way.
        lines.append("No usage recorded.")
    else:
        lines.append(f"- 24h: {_usage_totals_line(day)}")
        lines.append(f"- 30d: {_usage_totals_line(month)}")

    if brains:
        # The section that answers "which brain is spending", and the reason the
        # command needs no brain in its name.
        lines += ["", "**By brain** (30d)", ""]
        for group in brains:
            key = str(group.get("key") or "unknown")
            lines.append(
                f"- {key}: {_compact_tokens(group['total_tokens'])} tokens, "
                f"{render_cost(group['cost_by_basis'])}"
            )
    return lines


def _usage_totals_line(summary: dict) -> str:
    """One window's row count, tokens, cache hit rate and cost."""
    rows = summary["rows"]
    return (
        f"{fmt_int(rows)} row{'' if rows == 1 else 's'}, "
        f"{_compact_tokens(summary['total_tokens'])} tokens, "
        f"{summary['cache_hit_rate'] * 100:.0f}% cached, "
        f"{render_cost(summary['cost_by_basis'])}"
    )


def _compact_tokens(value) -> str:
    """`31.4M`, `1.2M`, `9,840`.

    Compact only past a million, where the separators stop helping: the CLI's
    table gives a fleet total 15 characters of its own, while a chat line carries
    four figures and has to stay one line. Below that the comma-grouped integer
    is both shorter and exact.
    """
    try:
        total = int(value or 0)
    except (TypeError, ValueError):
        return COST_PLACEHOLDER
    for unit, size in (("B", 1_000_000_000), ("M", 1_000_000)):
        if abs(total) >= size:
            return f"{total / size:.1f}{unit}"
    return f"{total:,}"


def _usage_plan_section(
    snapshot: "UsageSnapshot",
    config: Config,
    user_id: str,
    now_ts: float,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Section 3 — the plan windows — or nothing at all.

    Omitted silently when there is nothing to show. A *stale* reading is not
    nothing: an old real number outranks a blank, and the footer admits its age.

    The heading names the brain, which is the disambiguation a `!cc-` prefix
    would have bought without spending the command name on it (and `!cc-usage` is
    unspellable anyway — `parse_command`'s `\\w` excludes hyphens).
    """
    if not snapshot.has_data:
        return []

    zone, zone_is_fallback = _usage_timezone(config, user_id, conn)
    lines = ["", "**Claude Code subscription**", ""]
    for window in snapshot.windows:
        # Sanitized once and read twice. Formatting `window.percent` directly
        # beside a defended bar would put the defence on one half of the line
        # only — a string percent survives `float()` and then raises on `:.0f`,
        # and a NaN draws an empty bar labelled `nan`.
        percent = _usage_percent(window.percent)
        lines.append(
            f"- {window.label}: [{_usage_bar(percent)}] "
            f"{percent:.0f}%{_usage_reset(window, zone, zone_is_fallback)}"
        )

    spend = snapshot.spend
    if spend is not None and spend.enabled:
        # Money the account has actually committed, reported in minor units with
        # an explicit currency — not a token count priced at list. That is why it
        # does not contradict the rule keeping a dollar figure off the lines
        # above on a subscription deployment.
        lines += [
            "",
            f"**Extra usage:** {_usage_money(spend.used_minor, spend)} / "
            f"{_usage_money(spend.limit_minor, spend)} ({spend.percent:.0f}%)",
        ]

    age = snapshot.age_seconds(now_ts)
    if snapshot.error or age > _USAGE_FRESH_SECONDS:
        # The error itself stays out of the reply: what went wrong belongs in
        # `runtime.subscription_usage`, and all a reader needs here is that the
        # number is not current.
        lines += ["", f"_Reading is {_usage_age(age)} old._"]
    return lines


def _usage_percent(value) -> float:
    """A finite percentage in [0, 100], whatever arrives.

    `subscription_usage._percent` already guarantees this for a live reading;
    the repeat is because the cache is a file on disk and this renders a
    fixed-width field, where a NaN or a string would produce a ragged bar or a
    `nan` label rather than a wrong number. `OverflowError` is caught explicitly:
    `round(inf)` raises it and it is neither a `TypeError` nor a `ValueError`.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN, which compares false against everything, 0 included
        return 0.0
    return min(100.0, max(0.0, number))


def _usage_bar(percent: float) -> str:
    """A 20-character bar, filled by floor rather than by rounding.

    Floor, and a full bar reserved for 100, because the two ends are what a
    quota display is read for: `round` fills all twenty blocks from 97.5% up, so
    a window with headroom left draws as one with none, and empties the bar
    below 2.5% so a window that is being consumed draws as untouched. Floor also
    makes the fill monotonic, where `round`'s banker's rule gives 12.5% two
    blocks and 17.5% four. A non-zero percentage always shows at least one block
    for the same reason: "some" must not render as "none".

    The spec's own sample output agrees on every value it shows — 40% is eight
    blocks, 21% is four, 0% is none.
    """
    if percent >= 100.0:
        return "#" * _USAGE_BAR_WIDTH
    filled = int(percent / 100.0 * _USAGE_BAR_WIDTH)
    if percent > 0.0:
        filled = max(1, filled)
    filled = min(_USAGE_BAR_WIDTH - 1, filled)
    return "#" * filled + "-" * (_USAGE_BAR_WIDTH - filled)


def _usage_reset(window: "UsageWindow", zone: tzinfo, zone_is_fallback: bool) -> str:
    """`" (resets Aug 22 18:07)"` in the reader's own clock, or nothing.

    Absolute, where the admin card's sub-line is relative: a chat reply is read
    once and possibly hours later, while the card re-renders every 60 seconds.
    Both derive from the one `resets_at`.
    """
    raw = window.resets_at
    if not raw:
        return ""
    try:
        text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local = parsed.astimezone(zone)
        # Built field by field rather than with `%-d`, which is a glibc/BSD
        # extension rather than a portable strftime directive.
        stamp = f"{local:%b} {local.day} {local:%H:%M}"
    except (AttributeError, OverflowError, TypeError, ValueError):
        # The whole conversion is inside the guard, not just the parse, and
        # `OverflowError` is in the tuple for one specific reason: a date at the
        # edge of the range parses cleanly and then overflows on the shift into
        # the reader's zone. `9999-12-31T23:00:00Z` is *canonical* — it survives
        # `subscription_usage._normalize_resets_at` unchanged — and raises for a
        # reader east of UTC while rendering fine for one west of it, so which
        # admin typed `!usage` would decide whether the command worked. The
        # module has the same note for the same value, one keystroke from the
        # sentinel expiry this codebase writes into credential files.
        #
        # A reset stamp that cannot be rendered degrades to no stamp, exactly as
        # an unparseable one does. The percentage is still the reading, and
        # `dispatch` would otherwise post the raw exception to the room.
        return ""
    if zone_is_fallback:
        # Name the clock, because it is not the reader's own.
        stamp += " UTC"
    return f" (resets {stamp})"


def _usage_timezone(
    config: Config, user_id: str, conn: sqlite3.Connection | None = None
) -> tuple[tzinfo, bool]:
    """`(zone, say_utc)` for the invoking user.

    `Config.resolve_user_timezone`, not `config.get_user(...).timezone`, and the
    difference is user-visible: the resolver prefers the live `user_profiles`
    row over the in-memory `UserConfig` precisely so a web-UI timezone edit takes
    effect without a scheduler restart (ISSUE-099). Reading the in-memory config
    would render every reset stamp in the zone the daemon booted with, which is
    the staleness that issue exists to have fixed — and `cmd_export` below
    already resolves it the live way, so two commands in this file would
    disagree about what time it is for the same user.

    `conn` is threaded through for the reason the resolver's own signature gives:
    a caller already holding a framework-DB connection should not make it open
    another, which on the FUSE-backed mount is per-call FD churn.

    The resolver never returns empty and never validates — it hands back a
    string, and wrapping it in `ZoneInfo` is the caller's job, invalid names
    included. So the second element is not "did we find a profile" but the
    simpler and more honest "are these times UTC": true when the resolver landed
    on its own `"UTC"` fallback, when the user set UTC deliberately, and when the
    name does not parse. All three render a UTC clock, and the line's job is to
    say which zone it is showing rather than why that zone was picked.
    """
    from zoneinfo import ZoneInfo

    name = config.resolve_user_timezone(user_id, conn=conn)
    if name.strip().upper() != "UTC":
        try:
            return ZoneInfo(name), False
        except Exception:
            # A typo in a profile is not a reason to fail a reply.
            logger.debug("unusable timezone %r for %s in !usage", name, user_id)
    return timezone.utc, True


def _usage_money(minor: int, spend: "Spend") -> str:
    """A pay-as-you-go amount, in major units of its own currency.

    Both the divisor *and* the precision come from the payload's own `exponent`,
    never a hardcoded 100 — that is wrong for every currency that is not
    two-decimal, and it is the bug the removed implementation carried. Taking the
    precision from the same place is what keeps a zero-decimal currency from
    rendering `500.00`, which is two digits the account never had.

    Not routed through `usage_render.fmt_money`, and the distinction is the whole
    reason this line is allowed to carry a currency symbol at all: that rule
    formats a *token cost*, where a sub-cent figure has to stay visible rather
    than round to `$0.00`. This is a credit balance already quantized to its own
    smallest unit, so it is exact at `exponent` places and never sub-unit.
    """
    try:
        exponent = max(0, int(spend.exponent))
    except (AttributeError, TypeError, ValueError):
        exponent = 2
    try:
        major = float(minor) / (10**exponent)
    except (TypeError, ValueError):
        return COST_PLACEHOLDER
    text = f"{major:.{exponent}f}"
    # ISO 4217 codes are uppercase, but nothing upstream normalizes the field,
    # so a `"usd"` off the wire would otherwise render `1.25 usd`.
    currency = str(spend.currency or "USD").upper()
    return f"${text}" if currency == "USD" else f"{text} {currency}"


def _usage_age(seconds: float) -> str:
    """`45s`, `2m`, `1h 04m`, `6d 2h`. Two units at most.

    `doctor._duration` is the same six lines, and is not imported: `doctor` is
    reached from inside every `load_config`, while this module sits on the Talk
    polling path, so the dependency would run the wrong way for a formatter this
    small. The cost rule is shared because it carries a correctness rule; a
    coarse duration does not.
    """
    total = int(max(0.0, seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


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
    from .cron_loader import _MODULE_JOB_PREFIX, update_job_enabled_in_cron_md

    parts = args.strip().split(maxsplit=1)
    subcmd = parts[0].lower() if parts else ""
    job_name = parts[1].strip() if len(parts) > 1 else ""

    # A `_module.*` row is seeded by its module and appears in nobody's
    # CRON.md, so the file write below is not merely a no-op for it — it
    # returns False and drops the user into the fallback branch, whose whole
    # message is that the change is DB-only and may not persist. For a module
    # row the DB is the only authority there is, and the change does persist
    # (ISSUE-392), so that note is the opposite of true. Skip the file.
    is_module_job = job_name.startswith(_MODULE_JOB_PREFIX)

    if subcmd == "enable" and job_name:
        job = db.get_scheduled_job_by_name(conn, user_id, job_name)
        if not job:
            return f"No scheduled job named '{job_name}' found."
        # Write to CRON.md (source of truth); DB updated on next sync
        from .notification_resolvers import cron_job as cron_job_source

        if is_module_job:
            db.enable_scheduled_job(conn, job.id)
            cron_job_source.resolve_for_job(conn, user_id, job.id, by=ctx.surface)
            return (
                f"Enabled module job '{job_name}' (failure count reset; "
                "any scheduler suspension lifted)."
            )
        if update_job_enabled_in_cron_md(config, user_id, job_name, True):
            db.enable_scheduled_job(conn, job.id)
            # The suspension this lifts is the inbox row's close predicate, so
            # the row would go `stale` on the next panel read either way.
            # Closing it here makes it `resolved` by the surface that ended the
            # condition.
            cron_job_source.resolve_for_job(conn, user_id, job.id, by=ctx.surface)
            return (
                f"Enabled scheduled job '{job_name}' (failure count reset; "
                "any scheduler suspension lifted)."
            )
        # Fallback: CRON.md does not say what we asked it to. Three causes,
        # and the message names all three because the user cannot tell them
        # apart from here — no CRON.md at all (the case this branch was
        # written for), no such job in it, or a refused write. That last one
        # is why the wording changed: the mount is rclone-backed, so a failed
        # write is a real case, and until ISSUE-369 the writer swallowed it
        # and reported success here. Update the DB alone and say the change
        # may not persist, which where there is a file it will not: the next
        # sync tick reads the unchanged file and reverts it.
        db.enable_scheduled_job(conn, job.id)
        cron_job_source.resolve_for_job(conn, user_id, job.id, by=ctx.surface)
        return f"Enabled scheduled job '{job_name}' (failure count reset). Note: CRON.md was not updated (no file, no such job in it, or the write was refused) — change is DB-only and may not persist."

    if subcmd == "disable" and job_name:
        job = db.get_scheduled_job_by_name(conn, user_id, job_name)
        if not job:
            return f"No scheduled job named '{job_name}' found."
        # Write to CRON.md (source of truth); DB updated on next sync
        from .notification_resolvers import cron_job as cron_job_source

        # Closed here too, and `disable` is the case the resolver cannot cover:
        # disabling by hand writes the user's column and leaves the scheduler's
        # `auto_disabled_at` where it was, so an inbox row raised by an earlier
        # suspension would keep telling the user to re-enable a job they have
        # just switched off on purpose — forever, since object-backed rows are
        # never age-swept.
        if is_module_job:
            db.disable_scheduled_job(conn, job.id)
            cron_job_source.resolve_for_job(conn, user_id, job.id, by=ctx.surface)
            return f"Disabled module job '{job_name}'."
        if update_job_enabled_in_cron_md(config, user_id, job_name, False):
            db.disable_scheduled_job(conn, job.id)
            cron_job_source.resolve_for_job(conn, user_id, job.id, by=ctx.surface)
            return f"Disabled scheduled job '{job_name}'."
        # Fallback: the same three causes as the enable branch above.
        db.disable_scheduled_job(conn, job.id)
        cron_job_source.resolve_for_job(conn, user_id, job.id, by=ctx.surface)
        return f"Disabled scheduled job '{job_name}'. Note: CRON.md was not updated (no file, no such job in it, or the write was refused) — change is DB-only and may not persist."

    # Default: list all jobs
    jobs = db.get_user_scheduled_jobs(conn, user_id)
    if not jobs:
        return "No scheduled jobs configured."

    lines = [f"**Scheduled jobs ({len(jobs)}):**", ""]
    for job in jobs:
        # Three states, two columns. `DISABLED` wins over `SUSPENDED` when a
        # job is both: the user's own intent is the more informative of the
        # two, and it is the one they can act on from here.
        #
        # `disabled_at` renders alongside it where there is one, symmetrically
        # with the suspension below. Without it the column would be write-only
        # — the legacy rescue arm's `IS NULL` test would be its sole reader —
        # and a `_module.*` row is exactly where an operator needs to see which
        # of the two stopped a job, since only one of them is theirs to undo.
        # An unstamped row predates the column or was switched off some other
        # way, and stays a bare `DISABLED` rather than claiming an author.
        if not job.enabled:
            status = "DISABLED"
            if job.disabled_at:
                status += f" since {job.disabled_at[:16]}"
        elif job.auto_disabled_at:
            status = f"SUSPENDED since {job.auto_disabled_at[:16]}"
        else:
            status = "enabled"
        kind = " (cmd)" if job.command else ""
        line = f"- **{job.name}**{kind} `{job.cron_expression}` [{status}]"
        if job.model:
            line += f" `model: {job.model}`"
        if job.effort:
            line += f" `effort: {job.effort}`"
        if job.brain:
            line += f" `brain: {job.brain}`"
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


@command("check", "Run a deployment health check")
async def cmd_check(ctx: CommandContext):
    """The doctor registry, rendered into a room.

    This used to be a hand-rolled copy of the same five probes
    `heartbeat._check_self` ran, drifting from doctor's registry and from the
    other copy. Both are gone; this selects and renders and asserts nothing of
    its own. Deliberately not an alias for `istota doctor`, which has a
    different audience: this posts into a room, must redact, and answers a
    non-admin differently.

    **A non-admin gets the verdict line, not the registry.** The command is
    registered with no admin gate and must not grow one silently — a non-admin
    asking whether the bot is healthy is a reasonable question with a one-line
    answer. But the whole registry is a different thing from the five lines they
    used to get: `runtime.subscription_usage` reports plan utilization, which
    `cmd_usage` above deliberately withholds from a non-admin;
    `config.skill_overlays` labels overlays by user id across users;
    `developer.container` and `developer.repos_layout` describe other people's
    trees. `redact` covers configured credential values, not cross-user facts.
    An allowlist of non-admin-safe check names was the alternative and is worse:
    a second list to keep in step with a registry that grows, and a check whose
    detail widens later leaks with nothing going red. A count is a count.

    **`live=True`, and `deep` deliberately not passed.** `live` restores the
    execution test this command used to run inline. `deep` would add
    `sandbox.masks` at 30 seconds on top, and neither surface has room:
    the web chat client aborts the very POST that returns this result at
    `SEND_TIMEOUT_MS` (30s) and withholds Retry for a `!`-prefixed body, and the
    Talk path has no timeout but is worse — dispatch runs on the process-global
    asyncio loop inside the poll batch's open write transaction, on the only
    Talk poll thread. That loss is real and compensated:
    `security.sandbox_effective` runs on the shallow path, answers the same
    availability question from the warm memo at no cost, and is the line an
    operator needs. An operator who wants the namespace probe itself runs
    `istota doctor`, which has no surface timeout.

    **The body is fenced.** `render_text` is aligned plain text; posted as
    markdown its two-space indents collapse into one run-on paragraph and its
    eight-space remedy lines become code blocks. A fence costs one line and
    preserves the alignment, where a second renderer in `doctor.py` would be a
    layout to keep in step with `render_text` for one caller.
    """
    import asyncio

    from . import doctor

    # Commit the caller's transaction before blocking, exactly as `cmd_usage`
    # does above and for the same reason. The Talk poller wraps its whole batch
    # in one transaction and hands `dispatch` that connection already mid-write,
    # so holding its write lock across a multi-second probe stalls every other
    # writer in the daemon — scheduler, workers, web — on their busy timeout.
    # Nothing here writes, so there is no durability question, only the lock.
    # This handler is the one `cmd_usage`'s comment named as the anti-pattern:
    # a bare `subprocess.run` on the loop, at ~34 seconds, with the lock held.
    if ctx.conn is not None:
        try:
            ctx.conn.commit()
        except sqlite3.Error:
            logger.debug("!check could not commit the caller's transaction", exc_info=True)

    config = ctx.config

    if not config.is_admin(ctx.user_id):
        results = await asyncio.to_thread(doctor.run_checks, config)
        healthy, summary = doctor.verdict(results)
        return f"**Health Check**\n\n{'OK' if healthy else 'PROBLEMS'} — {summary}"

    def _run():
        results = doctor.run_checks(config, live=True)
        # `render_text` redacts internally through the `secrets` it is handed,
        # so this must not also call `redact` — that would double-scrub.
        return doctor.render_text(results, secrets=doctor.config_secrets(config))

    return "**Health Check**\n\n```\n" + await asyncio.to_thread(_run) + "\n```"


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
# !retry / !resume — user-initiated re-run of a failed/cancelled task
# =============================================================================

# Interactive source types eligible for a user-initiated retry. A retry is a
# fresh conversational turn in the same room, so only room-scoped interactive
# tasks qualify — a scheduled/briefing/heartbeat task retries on its own
# schedule and isn't something the user re-runs by hand here.
_RETRYABLE_SOURCE_TYPES = ("talk", "email", "repl", "web")


async def _resolve_retry_target(ctx: CommandContext) -> "tuple[db.Task | None, str]":
    """Resolve the failed/cancelled task a `!retry`/`!resume` targets.

    Returns ``(task, error)``: on success ``task`` is a ``db.Task`` and
    ``error`` is ``""``; on failure ``task`` is ``None`` and ``error`` is a
    user-facing message. Mirrors ``!more``/``!steer``: an explicit ``#<id>`` if
    given, else the most recent failed/cancelled interactive task in the
    resolved canonical room. Own-task only, unless admin.
    """
    config, conn, user_id, args = ctx.config, ctx.conn, ctx.user_id, ctx.args
    room_token = db.resolve_room_token(conn, ctx.surface, ctx.conversation_token) \
        or ctx.conversation_token

    id_str = args.strip().lstrip("#")
    if id_str:
        if not id_str.isdigit():
            return None, (
                "Usage: `!retry [#<task_id>]` — re-run a failed or cancelled "
                "task (defaults to the last one in this room)."
            )
        task = db.get_task(conn, int(id_str))
        if task is None:
            return None, f"Task #{id_str} not found."
        if task.user_id != user_id and not config.is_admin(user_id):
            return None, f"Task #{task.id} belongs to another user."
        if task.source_type not in _RETRYABLE_SOURCE_TYPES:
            return None, (
                f"Task #{task.id} is a `{task.source_type}` task — only "
                "interactive tasks can be retried this way."
            )
        if task.status in ("running", "locked", "pending"):
            return None, (
                f"Task #{task.id} is still {task.status} — use `!stop` first if "
                "you want to restart it."
            )
        if task.status == "pending_confirmation":
            return None, (
                f"Task #{task.id} is awaiting your confirmation — reply normally "
                "to answer it."
            )
        if task.status == "completed":
            return None, (
                f"Task #{task.id} completed successfully — there's nothing to retry."
            )
        # Only failed/cancelled remain.
        return task, ""

    placeholders = ",".join("?" * len(_RETRYABLE_SOURCE_TYPES))
    row = conn.execute(
        f"""
        SELECT id FROM tasks
        WHERE user_id = ? AND conversation_token = ?
          AND source_type IN ({placeholders})
          AND status IN ('failed', 'cancelled')
        ORDER BY created_at DESC LIMIT 1
        """,
        (user_id, room_token, *_RETRYABLE_SOURCE_TYPES),
    ).fetchone()
    if row is None:
        return None, "No failed or cancelled task in this room to retry."
    task = db.get_task(conn, row["id"])
    if task is None:
        return None, "No failed or cancelled task in this room to retry."
    return task, ""


def _create_retry_task(conn, original: "db.Task", prompt: str) -> int:
    """Create a fresh task copying the retry-relevant fields off ``original``.

    A new row (not a ``set_task_pending_retry`` flip of the old one) keeps the
    retry out of the automatic backoff / ``attempt_count`` pressure, leaves the
    failed attempt intact in history with its trace, and shows up as a fresh
    turn. ``parent_task_id`` keeps the lineage queryable. Delivery-relevant
    fields (``output_target`` / ``talk_delivery_token`` / ``model`` / ``effort``
    / ``skill``) are copied so the retry lands on the same surface as the
    original. ``brain`` is copied for the neighbouring reason: the retry re-runs
    the original attempt, so it runs the brain that attempt ran even if the room
    has since been pointed at another one.

    ``withheld_from_room`` is copied for the same reason (ISSUE-255), and it is
    load-bearing here rather than tidy: the retry inherits `conversation_token`,
    so without it a withheld exchange re-enters every reader that column keys —
    the room's history fallback, its memory namespace, its sleep cycle. A bare
    ``!retry`` typed in the origin room can reach such a task, since
    ``_resolve_retry_target`` picks the newest failed task for the token.
    """
    return db.create_task(
        conn,
        prompt=prompt,
        user_id=original.user_id,
        source_type=original.source_type,
        conversation_token=original.conversation_token,
        parent_task_id=original.id,
        is_group_chat=original.is_group_chat,
        withheld_from_room=original.withheld_from_room,
        output_target=original.output_target,
        talk_delivery_token=original.talk_delivery_token,
        model=original.model,
        effort=original.effort,
        brain=original.brain,
        model_namespace=original.model_namespace,
        skill=original.skill,
        skill_args=original.skill_args,
        priority=original.priority,
    )


def _record_retry_user_turn(
    conn, task_id: int, original: "db.Task", surface: str,
) -> None:
    """Best-effort: store the retry's user turn in the room transcript.

    Body is the *original* clean prompt (never a `!resume` trace injection), so
    the retry reads as the user re-asking the question and future LLM context
    sees what was actually asked. ``task_id`` is the new task's id so the row
    pairs with the eventual assistant turn (``_store_room_turn``). Room surfaces
    only; mirrors `!steer`'s transcript write."""
    # The ownership question, and the conversion is behaviour-preserving for
    # every surface including email.
    #
    # **The asymmetry it cements is deliberate-for-now and unexamined**, and is
    # recorded here so whoever asks finds it from the code: an email `!confirm`
    # records its exchange (`_record_confirm_exchange` gates on
    # `_TRANSCRIPT_SURFACES`, member plus guest) while an email `!retry` records
    # no user turn at all. Both are `role='user'` rows in the same room, and the
    # two gates disagree about email. Which of them is right is a product
    # question nobody has put; naming this one for ownership does not settle it.
    if not is_room_member(surface):
        return
    token = original.conversation_token
    if not token:
        return
    try:
        if db.get_room(conn, token) is not None:
            db.add_message(
                conn, token, role="user", body=original.prompt,
                origin_surface=surface, task_id=task_id,
                # The retry re-asks the original question, so it is the original
                # asker's turn — not the reader's, in a shared room.
                author_user_id=original.user_id,
            )
    except Exception:
        logger.debug("retry transcript user-row write failed", exc_info=True)


def _render_prior_progress(task: "db.Task") -> str | None:
    """Render a failed task's execution trace as a prior-progress block for
    `!resume` injection, or ``None`` when there's no usable trace.

    Uses the verbatim Bash invocation (`raw`, per ISSUE-174) when present so the
    model sees the real command it ran, falling back to the tool description.
    Returns ``None`` for an absent/empty/corrupt trace so the caller degrades to
    `!retry` semantics (ISSUE-183: pre-trace failed tasks have no trace)."""
    if not task.execution_trace:
        return None
    try:
        trace = json.loads(task.execution_trace)
    except (json.JSONDecodeError, TypeError):
        return None
    lines: list[str] = []
    for entry in trace:
        etype = entry.get("type")
        if etype == "tool":
            raw = entry.get("raw")
            desc = (entry.get("text") or "").strip()
            if raw:
                lines.append(f"- ran: {raw}")
            elif desc:
                lines.append(f"- {desc}")
        elif etype == "text":
            text = (entry.get("text") or "").strip()
            if text:
                lines.append(f"- (was thinking) {text}")
    if not lines:
        return None
    return "\n".join(lines)


def _build_resume_prompt(original_prompt: str, progress: str) -> str:
    """Prepend the prior-progress block to the original prompt for `!resume`.

    Injecting into the stored prompt (rather than a dedicated executor context
    section) keeps the change to the command layer — it flows through every
    brain and context path unchanged."""
    return (
        "You were previously working on the task below and got part of the way "
        "through before the attempt ended. Here's what you already did — "
        "continue from where you left off, and don't repeat these steps unless "
        "you actually need a result again:\n\n"
        f"{progress}\n\n"
        "--- Original request ---\n\n"
        f"{original_prompt}"
    )


@command(
    "retry",
    "Re-run a failed/cancelled task from scratch: `!retry` (last in room) or `!retry #<id>`",
)
async def cmd_retry(ctx: CommandContext):
    task, error = await _resolve_retry_target(ctx)
    if task is None:
        return error
    new_id = _create_retry_task(ctx.conn, task, task.prompt)
    _record_retry_user_turn(ctx.conn, new_id, task, ctx.surface)
    ctx.conn.commit()
    preview = task.prompt[:80] + "..." if len(task.prompt) > 80 else task.prompt
    return f"Retrying task #{task.id} as #{new_id}: {preview}"


@command(
    "resume",
    "Re-run a failed/cancelled task, continuing from its prior progress: "
    "`!resume` or `!resume #<id>`",
)
async def cmd_resume(ctx: CommandContext):
    task, error = await _resolve_retry_target(ctx)
    if task is None:
        return error
    progress = _render_prior_progress(task)
    if progress is None:
        # No captured trace to continue from — degrade to a clean re-run.
        new_id = _create_retry_task(ctx.conn, task, task.prompt)
        _record_retry_user_turn(ctx.conn, new_id, task, ctx.surface)
        ctx.conn.commit()
        return (
            f"No prior progress was captured for task #{task.id}, so I'm retrying "
            f"it from scratch as #{new_id}."
        )
    resume_prompt = _build_resume_prompt(task.prompt, progress)
    new_id = _create_retry_task(ctx.conn, task, resume_prompt)
    _record_retry_user_turn(ctx.conn, new_id, task, ctx.surface)
    ctx.conn.commit()
    step_count = progress.count("\n") + 1
    preview = task.prompt[:80] + "..." if len(task.prompt) > 80 else task.prompt
    return (
        f"Resuming task #{task.id} as #{new_id}, continuing from {step_count} "
        f"prior step(s): {preview}"
    )


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
# `skill_overlay` is here because it is the only surface that can answer "why
# does the bot always do X" for a rule that lives in one skill's overlay: the
# rule reaches a prompt only on a task that selected that skill, so a user
# searching for it from a room would otherwise get nothing.
_MEMORY_SOURCE_TYPES = (
    "memory_file", "user_memory", "skill_overlay",
    "channel_memory", "channel_memory_durable",
)
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
      - ``memory_file`` / ``user_memory`` / ``skill_overlay`` rows are the user's
        personal memory — not room-bound, ``is_memory=True``, room token ``None``.

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

        else:  # memory_file / user_memory / skill_overlay — personal, not room-bound
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
    """Search Nextcloud Talk messages via the unified search API.

    Shares its implementation with ``nextcloud talk search`` so one call site
    serves both the user-facing command and the agent. Best-effort: a Talk
    hiccup must not wedge ``!search``.
    """
    from .async_runtime import get_talk_client

    try:
        data = await get_talk_client(config).search_messages(query, limit=limit)
    except Exception as e:
        logger.debug("Talk message search failed: %s", e)
        return []

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


@command(
    "trust",
    "Trust an email sender both ways — their mail is processed without asking, "
    "and you may be mailed at that address without approving each message: "
    "`!trust sender@example.com`",
)
async def cmd_trust(ctx: CommandContext):
    config, conn, user_id, args = ctx.config, ctx.conn, ctx.user_id, ctx.args
    email = args.strip().lower()
    if not email:
        # List trusted senders
        db_senders = db.list_trusted_senders(conn, user_id)
        user_config = config.users.get(user_id)
        config_patterns = user_config.trusted_email_senders if user_config else []

        # One list, two meanings. Since the outbound approval gate shipped,
        # every entry here also authorizes *mailing* that address without
        # per-message approval — and every row written before it was made under
        # the narrower inbound-only meaning. Saying so is the price of not
        # carrying two lists; see `outbound_policy`'s module docstring.
        lines = ["**Trusted email senders** (inbound and outbound):", ""]
        if config_patterns:
            for p in sorted(config_patterns):
                lines.append(f"- `{p}` (config)")
        if db_senders:
            for s in db_senders:
                lines.append(f"- `{s['sender_email']}`")
        if not config_patterns and not db_senders:
            lines.append("No trusted senders configured.")
        lines.append("")
        lines.append(
            "Mail from these is processed without asking, and mail *to* them "
            "is sent without waiting for your approval."
        )
        return "\n".join(lines)

    if "@" not in email:
        return "Usage: `!trust sender@example.com` or `!trust` to list."

    added = db.add_trusted_sender(conn, user_id, email)
    if added:
        return (
            f"Trusted `{email}` — their mail is processed without asking, and "
            "mail to that address is sent without waiting for your approval. "
            "`!untrust` reverses both."
        )
    return f"`{email}` is already trusted, for both incoming and outgoing mail."


def _visible_recipients(draft) -> str:
    """To + Cc by address, Bcc by count.

    `!drafts` is surface-agnostic and works in a multi-user Talk room, so
    anything this returns may be posted where every participant reads it.
    Printing the blind-carbon list there would defeat the one property Bcc has.
    The count is enough for the user to recognise their own message.
    """
    shown = [*draft.to_addrs, *draft.cc_addrs]
    text = ", ".join(shown) or "(no recipients)"
    if draft.bcc_addrs:
        n = len(draft.bcc_addrs)
        text += f" (+{n} bcc)"
    return text


def _draft_line(draft) -> str:
    """One held draft, addressable by id.

    Recipients and subject, and nothing of the body: the listing exists so the
    user can pick which draft to answer, and a body inlined here would push the
    others off a phone screen.
    """
    subject = (draft.subject or "(no subject)").replace("\n", " ")
    return f"- `#{draft.id}` → {_visible_recipients(draft)} — {subject}"


def _drafts_listing(drafts_list, *, lead: str) -> str:
    lines = [lead, ""]
    lines.extend(_draft_line(d) for d in drafts_list)
    lines.append("")
    lines.append("Answer with `!drafts send <id>` or `!drafts discard <id>`.")
    return "\n".join(lines)


@command(
    "drafts",
    "Outbound mail waiting for your approval: `!drafts`, `!drafts send <id>`, "
    "`!drafts discard <id>`",
)
async def cmd_drafts(ctx: CommandContext):
    """List and answer the outbound emails the approval gate held.

    Here so the gate is answerable from Talk, without a web session. The
    resolution rules deliberately mirror `!confirm` — bare verb acts only when
    exactly one draft is pending, several means say which — but the *tiebreak
    reasoning* is the opposite one, and worth stating so nobody "fixes" it into
    symmetry: an ambiguous `!confirm` resolves away from approval because it
    gates untrusted *inbound* mail, whereas an unambiguous `!drafts send` is the
    user releasing their own words. Ambiguity still refuses in both.
    """
    import asyncio

    from . import outbound_drafts as drafts

    conn, user_id = ctx.conn, ctx.user_id
    words = ctx.args.split()

    verb = ""
    if words and not words[0].lstrip("#").isdecimal():
        verb = words.pop(0).lower()
        if verb not in ("send", "discard", "list"):
            return (
                f"Don't know what `{verb}` means here. Try `!drafts`, "
                "`!drafts send <id>` or `!drafts discard <id>`."
            )

    target_id: int | None = None
    if words:
        token = words.pop(0).lstrip("#")
        # `isdecimal`, not `isdigit`: the latter is True for '²', which `int()`
        # then refuses, turning a typo into a traceback.
        if not token.isdecimal():
            return f"`{token}` is not a draft id. Try `!drafts` to see the open ones."
        target_id = int(token)
    if words:
        return (
            f"Too many arguments. Try `!drafts {verb or 'send'} <id>`."
        )

    pending = drafts.pending_for_user(conn, user_id)
    if not pending:
        return "No outbound mail is waiting for your approval."

    if verb in ("", "list"):
        if target_id is not None:
            # A bare `!drafts 41` names a draft but no decision. Refuse rather
            # than pick one: guessing `send` would mail a message off a typo,
            # and guessing `discard` would bin the user's own words.
            return (
                f"`!drafts {target_id}` doesn't say what to do with it. Use "
                f"`!drafts send {target_id}` or `!drafts discard {target_id}`."
            )
        return _drafts_listing(
            pending,
            lead=f"**{len(pending)} draft(s) waiting for your approval:**",
        )

    if target_id is not None:
        draft = next((d for d in pending if d.id == target_id), None)
        if draft is None:
            # One message for "no such draft", "not yours" and "already
            # answered" — the command must not become an oracle for which draft
            # ids exist, and the three read the same to the user.
            return _drafts_listing(
                pending,
                lead=f"Draft #{target_id} isn't waiting for your approval. Open right now:",
            )
    elif len(pending) == 1:
        draft = pending[0]
    else:
        return _drafts_listing(
            pending,
            lead=f"{len(pending)} drafts are waiting — say which:",
        )

    recipients = _visible_recipients(draft)
    if verb == "discard":
        try:
            drafts.discard(conn, draft.id, by=ctx.surface)
        except drafts.DraftError as e:
            return f"Couldn't discard #{draft.id}: {e}"
        # Commit before we say it happened. The Talk poller wraps its whole
        # batch — every conversation, every message — in one transaction and
        # hands us that connection, so an exception anywhere later in the batch
        # would roll this back after the user had already been told their draft
        # was gone. It would then reappear in `!drafts` and be nagged a day
        # later.
        conn.commit()
        return f"Discarded #{draft.id} — nothing was sent to {recipients}."

    # Commit the caller's transaction before handing off, for two reasons.
    #
    # The blocking one: `release` opens its **own** connection and commits a
    # `status='sending'` claim there before it touches SMTP (it has to — the
    # decision to send an irreversible message must be durable before the
    # message goes out, which is impossible inside a transaction it does not
    # own). The Talk poller hands `dispatch` its poll connection, which is
    # already mid-write by the time a `!command` is dispatched — the message
    # cache upsert and the poll cursor both sit in it uncommitted. Second writer
    # against first writer is `database is locked` after the full 30s busy
    # timeout, on the poll loop, every time. This is only reachable from Talk;
    # the web path opens its own connection and holds no write.
    #
    # The correctness one: the user's decision should be durable before the mail
    # is. Committing here costs the Talk poll batch its all-or-nothing property
    # for the messages already processed, which is the right trade — their side
    # effects are done and their cursor entries name messages we have finished
    # with, while a message later in the batch has not written a cursor entry
    # yet and is unaffected.
    ctx.conn.commit()

    # `release` then blocks on the network. Handlers run on the persistent
    # asyncio loop that also carries every Talk request, so the send goes to a
    # thread rather than stalling Talk for an SMTP conversation plus the IMAP
    # append to Sent.
    try:
        message_id = await asyncio.to_thread(
            drafts.release, ctx.config, draft.id, by=ctx.surface,
        )
    except drafts.DraftSentButUnrecorded as e:
        # Checked before the DraftError branch it belongs to. The mail is gone;
        # calling this "failed, try again" would be the one wrong thing to say.
        logger.error("!drafts send: draft %s sent but unrecorded: %s", draft.id, e)
        return (
            f"#{draft.id} **was sent** to {recipients} (`{e.message_id}`), but "
            f"recording it failed: {e.cause}. Do not resend it. A reply on this "
            "thread may not route back to this conversation."
        )
    except drafts.DraftError as e:
        return f"Couldn't send #{draft.id}: {e}"
    except Exception as e:  # noqa: BLE001 — SMTP failure; the row stays pending
        logger.warning("!drafts send failed for draft %s: %s", draft.id, e)
        return (
            f"Sending #{draft.id} failed: {e}. The draft is still waiting — "
            "try `!drafts send` again."
        )
    return f"Sent #{draft.id} to {recipients} (`{message_id}`)."


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
