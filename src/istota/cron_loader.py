"""Load scheduled job definitions from CRON.md files and sync to DB."""

import dataclasses
import hashlib
import json
import logging
import os
import re
import shlex
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import tomli
from croniter import croniter

from . import db
from .rooms import egress
from .workspace import text_files
from .workspace.text_files import MemoryMdLocked
from .workspace.writes import open_or_create_dirs as _open_or_create_dirs
from istota.sandbox.host_paths import owner_path_parts
from .storage import (
    OWNER_FILE_MAX_BYTES,
    USER_CONFIG_READ_CAP_BYTES,
    get_user_scripts_path,
    read_owner_text,
)

# The fence markers (ISSUE-386), and this module is why `toml_fence` states
# its bounds as loosely as it does: a marker that module does not recognise
# is not a parse that fails safely *here*. It is a document with no fence,
# which is `is_template`, and the sync's restore branch then rewrites the
# user's whole CRON.md from the table (`scheduler._sync_cron_files`).
#
# The markers are used directly rather than through
# `toml_fence.find_toml_block` because this is the one caller that has to
# tell "no opener" from "an opener with no closer". Only the first may ever
# reach the fence-less document, and only after the hold guard below has
# ruled out a fence it could not read.
from istota.lib.toml_fence import BACKTICK_RUN_RE as _BACKTICK_RUN_RE
from istota.lib.toml_fence import FENCE_CLOSE_RE as _TOML_FENCE_CLOSE_RE
from istota.lib.toml_fence import FENCE_OPEN_RE as _TOML_FENCE_OPEN_RE

_JOBS_HEADER_RE = re.compile(r"[ \t]*\[\[[ \t]*jobs[ \t]*\]\][ \t]*(#.*)?\r?\n?$")

logger = logging.getLogger("istota.cron_loader")

# Names with this prefix are managed by module integrations (e.g. money) and
# are not subject to CRON.md orphan deletion. Imported by `scheduler` and by
# `commands` (the `!cron` verbs), which have to answer for such a row without
# reaching for the file it is by definition absent from.
_MODULE_JOB_PREFIX = egress.MODULE_JOB_PREFIX

# Effort levels accepted by Claude Code's --effort flag. Loose check — warn,
# don't reject — so a future addition doesn't silently break user CRON.md.
_KNOWN_EFFORT_VALUES = {"low", "medium", "high", "xhigh", "max"}

# Phase 4 — operator CRON.md ``command:`` rows that are pure
# ``istota-skill <name> [args...]`` invocations are equivalent to
# skill-tasks and rewritten on sync to bypass the admin gate. Anything
# with shell metachars, env-var prefix, or non-trivial quoting stays a
# command-task and keeps the admin gate.
_SKILL_CLI_NAME = "istota-skill"
_SHELL_METACHARS = frozenset("|&;<>(){}*?$`\\\n")


def _parse_skill_command(command: str) -> tuple[str, str] | None:
    """If ``command`` is a pure ``istota-skill <skill> [args...]`` invocation,
    return ``(skill_name, skill_args_json)``. Otherwise ``None``.

    "Pure" means: no shell metacharacters, ``istota-skill`` is the head
    token (no env-var prefix like ``MONEY_USER=foo istota-skill ...``),
    and the skill name is a plain identifier.
    """
    command = command.strip()
    if not command:
        return None
    if any(ch in command for ch in _SHELL_METACHARS):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if len(tokens) < 2 or tokens[0] != _SKILL_CLI_NAME:
        return None
    skill_name = tokens[1]
    if not skill_name or not all(c.isalnum() or c == "_" for c in skill_name):
        return None
    args = tokens[2:]
    return skill_name, json.dumps(args)


def _resolve_job_dispatch(fj: "CronJob") -> tuple[str | None, str | None, str | None]:
    """Resolve a CronJob's dispatch shape for the DB row.

    Returns ``(command, skill, skill_args)`` — exactly one of ``command``
    or ``skill`` is set (or all None for prompt-type rows). Operator
    ``command:`` rows that match the pure ``istota-skill ...`` shape are
    promoted to skill-task rows so they bypass the admin gate.
    """
    if not fj.command:
        return None, None, None
    parsed = _parse_skill_command(fj.command)
    if parsed is None:
        return fj.command, None, None
    skill_name, skill_args = parsed
    return None, skill_name, skill_args


def fj_is_disallowed_command(job: "CronJob", is_admin: bool) -> bool:
    """True if a CRON.md job is a non-admin command-type job.

    Pure ``istota-skill <name> [args...]`` invocations are NOT
    command-type — they auto-promote to skill-tasks at sync time, which
    aren't admin-gated. Anything else with ``command:`` set requires
    admin.
    """
    if not job.command:
        return False
    if _parse_skill_command(job.command) is not None:
        return False
    return not is_admin


def fj_brain_or_none(job: "CronJob", is_admin: bool) -> str | None:
    """A CRON.md job's brain pin, or None where the author may not set one.

    CRON.md is a user file on the mount *and* is written by the model through
    the `schedules` skill, so unlike `rooms.brain` — which only an admin may
    write, through a gated surface — this field is model-writable. A brain kind
    decides which process holds the agent loop, which credentials it carries and
    which `SandboxProfile` is built, so it must not become the one enforcement-
    shaped setting a task can choose for its own next run.

    The field is dropped rather than the job refused: `fj_is_disallowed_command`
    refuses a whole job because the command *is* the job, while a brain pin is
    one field of a job that is otherwise fine.

    This gate answers *who* may pin, and deliberately not *what* they may pin.
    The value is bounded a second time at dispatch, where `resolve_brain_kind`
    refuses a kind absent from `[brain] room_selectable` — the same allowlist
    the room pin is held to, and the reason an operator can shorten that list
    without anything having to rewrite CRON.md. So an admin naming an
    unlisted kind is stored here and falls through there.

    And the claim above is bounded by the deployment: `Config.is_admin` reads
    an empty admins file as "everyone is an admin" on a single-user config,
    so there this drops nothing and the model writes CRON.md too. That is the `fj_is_disallowed_command` precedent rather than something
    new — an arbitrary shell command is already admin-gated the same way — but
    it means the enforcement here is the multi-user deployment's, not a
    universal one.
    """
    if not job.brain:
        return None
    if not is_admin:
        return None
    return job.brain


def fj_provider_or_none(job: "CronJob", is_admin: bool) -> str | None:
    """A CRON.md job's provider, or None where the author may not set one.

    The same rule as ``fj_brain_or_none``, for the same reason and one more:
    the provider decides which key a job's requests are billed to
    (model-catalogue D22). Dropped at sync for a non-admin. Judged again at
    dispatch, where `brain._resolve.effective_provider` runs a provider
    `[brain.native] providers` does not list on native's default instead.
    """
    if not job.provider:
        return None
    if not is_admin:
        return None
    return job.provider


def _split_model(model: str, config, pinned_brain: str, provider: str, is_admin: bool):
    """``(model, provider)`` a job stores for ``model``: an alias or the id
    within its provider, and the provider column, split as a room's are
    (`brain._resolve.select_for_person`). What does not resolve is kept as
    written with ``provider``. Never raises."""
    from .brain import resolve_brain_kind
    from .brain._aliases import split_effort
    from .brain._resolve import ResolveError, select_for_person

    try:
        kind = resolve_brain_kind("scheduled", config.brain, override=pinned_brain or None).kind
        picked = select_for_person(
            model, config, kind=kind, provider=provider or None, is_admin=is_admin,
        )
    except Exception:  # noqa: BLE001 — one job's model must not stop the file
        logger.debug("cron model split failed", exc_info=True)
        return model, provider or None
    if isinstance(picked, ResolveError) or picked.resolution.brain_default:
        return model, provider or None
    typed_effort = split_effort(model)[1]
    stored = picked.model
    if typed_effort and stored:
        stored = f"{stored}:{typed_effort}"
    return stored, picked.provider


def _validate_model(
    name: str, user_id: str, model: str, config=None, pinned_brain: str = "",
    errors: list[str] | None = None, provider: str = "",
) -> bool:
    """Whether the job keeps its ``model``; warns on a suspicious one.

    The job's owner chooses, so the rule is a person's (D9): an admin may
    name any model and an unresolvable one is only warned about, as before,
    since the brain sends it as written. A non-admin's model must resolve
    to an alias or to a model in ``[models] enabled`` that the job's brain
    runs; otherwise the field is dropped, like a non-admin's ``brain``, and
    the job runs on the brain's default. No patterns in a file (D10).

    Select the scheduled brain's namespace without constructing its runtime
    client. A malformed endpoint must not abort loading the whole CRON.md file.
    """
    from .brain import BrainConfig, resolve_brain_kind

    if any(c.isspace() for c in model):
        logger.warning(
            "Job '%s' (user %s): model %r contains whitespace — likely a typo",
            name, user_id, model,
        )
        return True

    brain_config = config.brain if config is not None else BrainConfig()
    resolved_config = resolve_brain_kind(
        "scheduled", brain_config, override=pinned_brain or None,
    )
    is_admin = config is None or config.is_admin(user_id)
    if config is not None:
        from .brain._resolve import ResolveError, describe_resolve_error, select_for_person

        try:
            resolved = select_for_person(
                model, config, kind=resolved_config.kind, provider=provider or None,
                is_admin=is_admin,
            )
        except Exception:  # noqa: BLE001 — one job's model must not stop the file
            logger.debug("cron model resolution failed", exc_info=True)
            resolved = None
        if isinstance(resolved, ResolveError) and not is_admin:
            reason = describe_resolve_error(resolved)
            logger.warning(
                "Job '%s' (user %s): dropping model %r: %s", name, user_id, model, reason,
            )
            if errors is not None:
                errors.append(
                    f"Job '{name}': model {model!r} is not one you can pick ({reason}); "
                    "the job runs on the default model"
                )
            return False
        if resolved is not None and not isinstance(resolved, ResolveError):
            return True

    if resolved_config.kind == "native":
        from .brain.native import NativeBrain

        valid = NativeBrain.is_valid_model_reference(model)
    else:
        from .brain.claude_code import ClaudeCodeBrain

        valid = ClaudeCodeBrain().is_valid_model_reference(model)
    if valid:
        return True

    logger.warning(
        "Job '%s' (user %s): invalid model reference %r for %s — likely a typo",
        name, user_id, model, resolved_config.kind,
    )
    return True


def _validate_effort(name: str, user_id: str, effort: str) -> None:
    """Warn when effort isn't in the known set; never reject."""
    if effort not in _KNOWN_EFFORT_VALUES:
        logger.warning(
            "Job '%s' (user %s): effort %r not in known set %s",
            name, user_id, effort, sorted(_KNOWN_EFFORT_VALUES),
        )


def _validate_brain(name: str, user_id: str, brain: str) -> None:
    """Warn on a brain kind nothing can build; never reject.

    The operator's allowlist is not checked here — `cron_loader` has no
    `Config`, and `resolve_brain_kind` refuses an unlisted kind at dispatch
    anyway, which is what lets an operator shorten the list without anything
    having to rewrite CRON.md. `_validate_model`'s docstring records the same
    limitation for the same reason.

    The value is kept either way. Storing a kind nothing recognises is what
    lets `!cron` show the operator what their file actually says, and the
    dispatch layer warns a second time and falls through to the configured
    brain, so the job runs rather than failing.
    """
    from .brain import KNOWN_BRAIN_KINDS

    if brain not in KNOWN_BRAIN_KINDS:
        logger.warning(
            "Job '%s' (user %s): brain %r is not a known brain kind (%s) — "
            "it will be ignored and the job will run the configured brain",
            name, user_id, brain, ", ".join(sorted(KNOWN_BRAIN_KINDS)),
        )


# Leaf surfaces a CRON.md `target` descriptor may name — the surfaces that have
# a registered transport / delivery path today. Aliases (both/all/none) are
# expanded away by parse_output_target before this check, so only real leaves
# appear here. `web` is included: WebTransport is registered and routable, and a
# `target = "web:<token>"` (or bare `web`, which resolves to the user's default
# room) delivers a system message into that room. Surfaces with no transport yet
# (matrix) are intentionally absent so a `target = "matrix:…"` warns instead of
# validating clean and then silently dropping at delivery. Unknown leaves
# warn-and-drop; this is warn-only.
#
# `room` is not a surface at all — it is the meta-destination
# `routing._expand_room_destinations` replaces at resolve time with the room's
# live bindings — and it is here because this check is about what a descriptor
# *delivers*, not about what the registry can name. It was correctly absent
# while the expansion answered nothing for a scheduled job: the warning was the
# only signal anywhere that such a job was broken, so calling it an unknown
# surface was wrong about the cause and right about the outcome. Since
# ISSUE-511 the descriptor delivers, and a working one that warns on every sync
# is worse than no warning at all.
#
# `sms` and `whatsapp` were missing on the same grounds (ISSUE-870): the bare
# descriptor resolves the user's own binding, and it is what `rooms list` and the
# prompt header hand out for a private phone room. `whatsapp:<id>` ignores the
# id for that binding, and a group JID never delivers from a cron job (it is
# answered only from the group's own turns), so that one spelling warns.
# `tests/test_cron_loader.py` holds this set to the surfaces `_resolve_one` names.
_KNOWN_TARGET_SURFACES = frozenset({
    "talk", "email", "sms", "whatsapp", "ntfy", "istota_file", "stream", "web",
    "room",
})

# The known surfaces `_resolve_one` drops when `make_registry` did not register
# their transport, which it does only when the transport is enabled.
_REGISTRY_REQUIRED_TARGET_SURFACES = ("sms", "whatsapp")


def _validate_room_pairing(name: str, user_id: str, target: str, room: str) -> None:
    """Warn when a ``room:`` or ``web:`` target and the job's ``room`` field name different
    rooms; never reject.

    ``room`` is the job's ``conversation_token``, and it is what
    ``transcript_room_for_task`` resolves — so a job that targets a room and
    does not name it here still delivers, and the answer arrives as an
    unsolicited ``role='system'`` note rather than as a turn in the room
    (ISSUE-511). That is a job which half works, and the half that is missing is
    invisible from the file. The skill docs already say to pair them; this is
    what says so when they are not.
    """
    from .transport import parse_output_target

    for dest in parse_output_target(target):
        # Both descriptors carry the canonical room token. Talk channels may
        # instead be binding refs, so comparing them to `room` would warn on
        # correctly paired promoted rooms.
        if dest.surface not in ("room", "web") or not dest.channel:
            continue
        if room.strip() == dest.channel:
            continue
        logger.warning(
            "Job '%s' (user %s): target names room %r but `room` is %s, so the "
            "result will arrive as a standalone note rather than as a turn in "
            "the room. Set room = \"%s\".",
            name, user_id, dest.channel,
            f"{room.strip()!r}" if room.strip() else "unset",
            dest.channel,
        )


def _validate_target(
    name: str, user_id: str, target: str, config=None,
) -> None:
    """Warn when a target descriptor names an unknown surface; never reject.

    The `target` is an output_target descriptor (talk/email/ntfy/both/all/
    talk:<token>/comma lists). Unknown leaves are dropped at delivery, so a typo
    is a warning, not a hard failure (mirrors model/effort handling). With a
    `config`, a known SMS or WhatsApp leaf on a deployment where that transport
    is off warns too, since it is dropped all the same, and so does a WhatsApp
    group JID."""
    from .transport import parse_output_target
    from .transport.registry import make_registry
    from .transport.whatsapp.identity import normalize_group_jid

    registered = None
    for dest in parse_output_target(target):
        if dest.surface not in _KNOWN_TARGET_SURFACES:
            logger.warning(
                "Job '%s' (user %s): target surface %r not recognized; it will "
                "be dropped at delivery",
                name, user_id, dest.surface,
            )
            continue
        if config is not None and dest.surface in _REGISTRY_REQUIRED_TARGET_SURFACES:
            if registered is None:
                registered = set(make_registry(config).names())
            if dest.surface not in registered:
                logger.warning(
                    "Job '%s' (user %s): target surface %r is not enabled on "
                    "this deployment; it will be dropped at delivery",
                    name, user_id, dest.surface,
                )
                continue
        if dest.surface == "whatsapp" and normalize_group_jid(dest.channel):
            logger.warning(
                "Job '%s' (user %s): a scheduled job cannot post into a WhatsApp "
                "group; this target will be dropped at delivery",
                name, user_id,
            )


def _coerce_bool(
    name: str, user_id: str, field: str, value, default: bool,
    errors: list[str] | None = None,
) -> bool:
    """A TOML bool, or the default with a warning. Never rejects the job.

    TOML has a real boolean type, so anything else here is a mistake in the
    file: ``enabled = "false"`` is a truthy string and used to leave the job
    running, which is the opposite of what was written. ``enabled = 1`` is an
    integer and gets the same treatment — TOML would have accepted ``true``.

    Warn-and-default rather than reject, matching how ``model``, ``effort``
    and ``target`` already behave: a typo in ``once`` would otherwise silently
    drop a job the user can see in their own file.
    """
    if isinstance(value, bool):
        return value
    # The value is truncated because this runs on the sync tick, once a
    # minute, for as long as the file says what it says -- and a TOML array
    # or inline table has no bound on its length. The type is what names the
    # mistake; the value is there to find it in the file.
    shown = repr(value)
    if len(shown) > 80:
        shown = shown[:77] + "..."
    logger.warning(
        "Job '%s' (user %s): %s must be a TOML boolean, got %s %s; using %s",
        name, user_id, field, type(value).__name__, shown, default,
    )
    if errors is not None:
        errors.append(
            f"Job '{name}': {field} must be a TOML boolean (true or false), "
            f"got {type(value).__name__} {shown}"
        )
    return default


@dataclass
class CronJob:
    name: str
    cron: str
    prompt: str = ""
    command: str = ""
    prompt_file: str = ""  # path relative to mount; resolved to prompt at load time
    target: str = ""  # "talk", "email", or ""
    room: str = ""  # conversation_token
    enabled: bool = True
    silent_unless_action: bool = False
    skip_log_channel: bool = False
    once: bool = False
    model: str = ""  # Per-job Claude model override; empty = use config default
    effort: str = ""  # Per-job effort override (low/medium/high/xhigh/max); empty = use config default
    # Per-job brain kind pin (claude_code / native); empty =
    # resolve from config. Admin-only: `fj_brain_or_none` drops it at sync
    # time for a non-admin author.
    brain: str = ""
    # Per-job provider for a native job (model-catalogue D22); empty = native's
    # default. Admin-only: `fj_provider_or_none` drops it at sync.
    provider: str = ""
    # admin-shared-briefing-blocks: publish this job's result text into shared_kv
    # on success. "<ns>/<key>" or bare "<key>" (→ briefing_shared_blocks). Empty =
    # no publish. Gated on is_shared_kv_writer at write time (admin-only).
    publish_shared_kv: str = ""
    publish_shared_kv_trusted: bool = False
    # "limited", "public" or "full", empty where the file states none. A request and
    # never a grant: the file is model-writable, so past what
    # `[security] scheduled_job_max_unapproved` allows the job runs limited until its
    # owner approves its exact text (`rooms.egress`).
    reach: str = ""
    # Hosts and `skill:` entries this job asks its owner for, in normal form. A
    # request and never a grant, like `reach`: it runs with what it has been given.
    egress: tuple[str, ...] = ()
    # `skill:` entries the owner cannot approve today. Asks for nothing, and the file
    # keeps them, as it keeps a refused `model`.
    egress_kept: tuple[str, ...] = field(default=(), compare=False)
    # Set at parse when the owner may not pick ``model`` (a non-admin naming a
    # model outside `[models] enabled`). The file keeps the field, so a rewrite
    # never deletes what the user wrote; the sync stores no model for it.
    model_refused: bool = field(default=False, compare=False)
    # ``(model, provider)`` the sync stores, set at parse: ``model`` split
    # into an id within its provider and the provider column (D22). None
    # stores ``model`` and ``provider`` as written.
    model_split: tuple | None = field(default=None, compare=False)


@dataclass
class CronDocument:
    """A user's CRON.md, kept whole rather than reduced to its jobs.

    ``load_cron_jobs`` answers "what jobs does this file define" and throws
    the rest away, which loses the two facts a writer needs. It cannot tell an
    **empty** toml fence from a **missing** one — both are ``[]`` — so the
    sync read "the user deleted their last job" and "this is a seeded
    template" as the same event and restored the deleted job from the table
    (ISSUE-369 defect 2). And it keeps no offsets, so every rewrite had to
    regenerate the whole document, discarding whatever prose the user had put
    around the fence.

    ``block`` is the toml source inside the first fence, ``None`` when the
    file has no fence at all. ``block_span`` are its offsets within
    ``content``, which is what the splicing writer replaces.
    ``skipped_entries`` counts the ``[[jobs]]`` tables the parser refused, so
    a caller can tell "this file lists no jobs" from "this file lists jobs
    and none of them are usable" — see :meth:`states_no_jobs`.
    """

    content: str
    block: str | None
    block_span: tuple[int, int] | None
    jobs: list[CronJob]
    skipped_entries: int = 0
    # Jobs whose `prompt_file` the path rule refused (ISSUE-596). The sync
    # deletes their rows even when it otherwise holds, since a pre-fix row
    # can carry another user's text as its prompt.
    refused_names: frozenset[str] = frozenset()
    # Jobs whose `prompt_file` could not be read for now (a mount fault). The
    # orphan sweep leaves their rows, or a passing fault would cost a job what
    # its owner approved for it.
    unread_names: frozenset[str] = frozenset()
    # Why a job was dropped, a field ignored, or a command job will not run
    # for this author, worded for the user. The sync does not read it; the
    # web editor's validator does (`cron_document_errors`).
    errors: tuple[str, ...] = ()

    @property
    def is_template(self) -> bool:
        """Nothing has authored a job list here yet.

        **The spec assumed this was ``block is None`` and the shipped
        template disagrees.** ``storage.CRON_TEMPLATE`` seeds a fence holding
        five commented-out example lines, so a freshly seeded CRON.md parses
        to zero jobs with ``block`` set — and reading that as "the user
        deleted everything" hands the sync's orphan sweep every row that user
        has. It is not a hypothetical shape: ``ensure_user_directories_v2``
        re-seeds the file whenever it is absent, and it runs on every task,
        every inbound email and every scheduler pass, so a CRON.md lost to a
        mount fault comes back as this and the next tick deletes the
        schedule it was supposed to protect.

        So the question is lexical rather than semantic — both shapes parse
        to ``{}``:

        - no fence, or a fence holding **only comments and whitespace**:
          nothing has been stated, treat it as a template.
        - a fence holding **nothing at all**: the user's own empty list, and
          exactly what ``render_jobs_block([])`` writes when the last job is
          removed. Deleting the last job has to mean deleting it, which is
          the whole of ISSUE-369 defect 2.

        The residue is that emptying the fence by hand *while leaving a
        comment in it* still restores from the table. That is the pre-change
        behaviour for every empty-fence shape, it is the safe direction, and
        it is one blank line away from the answer the user wanted.
        """
        if self.block is None:
            return True
        stripped = self.block.strip()
        if not stripped:
            return False
        return all(
            line.lstrip().startswith("#") for line in stripped.splitlines()
        )

    @property
    def states_no_jobs(self) -> bool:
        """The file says, in as many words, that there are no jobs to run.

        False when every entry it *did* list was refused, which is a
        different fact and must not authorize the orphan sweep: an
        unreadable ``prompt_file`` (a mount fault, or a prompts directory the
        user moved) drops its job at parse time, so a one-job file would
        otherwise delete the row while the definition sits in the file — and
        nothing brings it back, because the next tick reads the same file the
        same way.
        """
        return not self.jobs and not self.skipped_entries


def _str_field(
    name: str, user_id: str, field: str, value, errors: list[str] | None = None,
) -> str:
    """A TOML string, stripped, or ``""`` with a warning. Never raises.

    Every field here used to be read as ``j.get(field, "").strip()``, which
    is an ``AttributeError`` for anything that is not a string — and the
    ``try/except`` around the parse closes before the loop, so ``name = 5``
    in a CRON.md raised out of the loader into the scheduler's per-user
    handler, taking every *other* job in that file with it. Warn-and-default,
    like ``_coerce_bool`` and the three validators beside it.
    """
    if isinstance(value, str):
        return value.strip()
    shown = repr(value)
    if len(shown) > 80:
        shown = shown[:77] + "..."
    logger.warning(
        "Job '%s' (user %s): %s must be a TOML string, got %s %s; ignoring it",
        name or "(unnamed)", user_id, field, type(value).__name__, shown,
    )
    if errors is not None:
        errors.append(
            f"Job '{name or '(unnamed)'}': {field} must be a TOML string, "
            f"got {type(value).__name__} {shown}"
        )
    return ""


def _validate_reach(name: str, user_id: str, value, errors: list[str] | None = None) -> str:
    """What a job's `reach` says, empty where it says nothing. `private` is the earlier
    spelling of `limited`. Anything else is limited with a warning: a typo must not
    read as a request for more."""
    if value is None:
        return ""
    if not isinstance(value, str):
        _str_field(name, user_id, "reach", value, errors)
        return "limited"
    if value in ("", "limited", "public", "full"):
        return value
    if value == "private":
        return "limited"
    shown = repr(value[:80])
    logger.warning("Job '%s' (user %s): reach %s is not limited, public or full; using limited",
                   name, user_id, shown)
    if errors is not None:
        errors.append(f"Job '{name}': reach must be \"limited\", \"public\" or \"full\", got {shown}")
    return "limited"


def _validate_egress(
    name: str, config, user_id: str, value, errors: list[str] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The requests a job's `egress` may hold, normal form, duplicates gone, and the
    skills refused for now, which the file keeps.

    Names no entry in a log line: a hostname can carry data and this file is
    model-writable. `open:` and `public:` are refused because they name a job's
    text, which has its own request. A skill its owner could not approve is
    dropped, or a `once` job would wait on a button that refuses.
    """
    if errors is None:
        errors = []
    if not isinstance(value, list):
        logger.warning("Job '%s' (user %s): egress is not a list; ignoring it", name, user_id)
        errors.append(f"Job '{name}': egress must be a list of hosts and skill:<name> entries")
        return (), ()
    entries: dict[str, None] = {}
    kept: list[str] = []
    skills = None
    for position, raw in enumerate(value, start=1):
        try:
            parsed = egress.parse_entry(raw)
        except egress.EntryRefused as exc:
            logger.warning("Job '%s' (user %s): egress entry %d refused", name, user_id, position)
            errors.append(f"Job '{name}': egress entry {position}: {exc}")
            continue
        if parsed.kind not in ("host", "skill"):
            logger.warning("Job '%s' (user %s): egress entry %d refused", name, user_id, position)
            errors.append(
                f"Job '{name}': egress entry {position} must be a host or skill:<name>; "
                'use reach = "public" to ask for public access'
            )
            continue
        if parsed.kind == "skill":
            if skills is None:
                skills = _approvable_skills(config, user_id)
            if skills is not None and parsed.value not in skills:
                logger.warning(
                    "Job '%s' (user %s): egress entry %d refused", name, user_id, position,
                )
                errors.append(
                    f"Job '{name}': egress entry {position}: skill:{parsed.value} is not a "
                    "skill that needs approval and that you can use"
                )
                kept.append(parsed.text)
                continue
        entries[parsed.text] = None
    if len(entries) > egress.MAX_DECLARED:
        errors.append(
            f"Job '{name}': egress lists more than {egress.MAX_DECLARED} entries; "
            "the rest are ignored"
        )
    return tuple(entries)[:egress.MAX_DECLARED], tuple(kept)


def _approvable_skills(config, user_id: str):
    """The skills this owner could approve, or None when that cannot be read now. A
    passing fault must not cost a job its request, so None keeps the entry."""
    from .skills import _loader

    try:
        return _loader.usable_external_skills(config, user_id)
    except Exception as exc:  # noqa: BLE001 - one job's field must not stop the file
        logger.warning("cron_egress_skills_unavailable: %s", type(exc).__name__)
        return None


def load_cron_document(config, user_id: str) -> "CronDocument | None":
    """Load a user's CRON.md as a document: the bytes, the fence, the jobs.

    ``None`` for every case the file cannot be read as one — no mount, no
    file, an unreadable or non-UTF-8 one, or a fence whose TOML does not
    parse. A caller that gets ``None`` knows nothing about what the user
    wants and must not write anything back on the strength of it; the
    previous definitions stay in force.
    """
    if not config.has_workspace:
        return None

    # Hardened like every other host-side read of `{bot_dir}/config/`
    # (ISSUE-339). This one runs on the scheduler's own cron-sync tick, so a
    # FIFO planted at CRON.md blocked that loop rather than one task, and a
    # symlink fed an arbitrary daemon-readable file to the TOML parser.
    from .storage import read_user_config_file  # noqa: PLC0415 - import cycle

    content = read_user_config_file(config, user_id, "CRON.md")
    if content is None or not content:
        return None

    return parse_cron_document(content, config, user_id)


def parse_cron_document(content: str, config, user_id: str) -> "CronDocument | None":
    """Parse already-read bytes with the same hold guard as the scheduler."""
    return _parse_cron_document(content, config, user_id)[0]


def cron_document_errors(content: str, config, user_id: str) -> list[str]:
    """Why the scheduler would hold this CRON.md or drop part of it.

    The web editor's validator. The same parse the sync runs, so it accepts
    exactly the language the daemon loads: an empty list means every job the
    file states will be scheduled as written.
    """
    doc, errors = _parse_cron_document(content, config, user_id)
    if doc is not None:
        return list(doc.errors)
    return errors


def _parse_cron_document(
    content: str, config, user_id: str,
) -> "tuple[CronDocument | None, list[str]]":
    """The document, or None with the reason the whole file is held."""
    opener = _TOML_FENCE_OPEN_RE.search(content)
    if opener is None:
        if _BACKTICK_RUN_RE.search(content):
            # Backticks, but not a marker this module recognises: a fence
            # indented under a list item, one wrapped in four backticks
            # because the jobs themselves contain three, one inside a
            # blockquote, a `see ```toml` in a sentence. Reading any of those
            # as "the user has authored no jobs" hands the file to the
            # restore branch, which rewrites the whole document from the
            # table and takes the user's prose with it. Hold instead. The
            # cost is a schedule that stops tracking the file until it is
            # fixed; the alternative is losing the file.
            logger.warning(
                "CRON.md for %s has backtick fences but no toml block this "
                "reader can resolve; the previous job definitions stay in "
                "force and the file is left alone", user_id,
            )
            return None, [
                "The file has backtick fences but no ```toml block on a line "
                "of its own, so no jobs can be read from it"
            ]
        # A document with no toml fence. Distinct from a fence holding no
        # jobs, and the whole reason this function exists.
        return CronDocument(content=content, block=None, block_span=None, jobs=[]), []

    closer = _TOML_FENCE_CLOSE_RE.search(content, opener.end())
    if closer is None:
        # An opener with no closer. Not "no fence": the user is halfway
        # through an edit, or the file was written short, and the jobs
        # they are typing are in there. Reading it as a template hands it
        # to the restore branch, which writes a whole fresh document over
        # the top — so refuse it the way an unparseable fence is refused
        # and let the next tick find a finished file.
        logger.warning(
            "CRON.md for %s opens a toml fence and never closes it; the "
            "previous job definitions stay in force", user_id,
        )
        return None, ["The ```toml fence is opened and never closed"]

    # The block is everything between the two marker lines. Neither marker's
    # own indent is inside it, so a splice through `block_span` writes both
    # back exactly as the user indented them.
    block_span = (opener.end(), closer.start())
    toml_str = content[block_span[0]:block_span[1]]
    try:
        data = tomli.loads(toml_str)
    except Exception as e:
        logger.warning(
            "Failed to parse CRON.md for %s: %s; the previous job definitions "
            "stay in force", user_id, e,
        )
        return None, [f"TOML error: {e}"]

    errors: list[str] = []
    unread: set[str] = set()
    jobs, skipped, refused_names = _parse_jobs(data, config, user_id, errors, unread)
    return CronDocument(
        content=content,
        block=toml_str,
        block_span=block_span,
        jobs=jobs,
        skipped_entries=skipped,
        refused_names=refused_names,
        unread_names=frozenset(unread),
        errors=tuple(errors),
    ), errors


def load_cron_jobs(config, user_id: str) -> list[CronJob] | None:
    """
    Load scheduled job definitions from a user's CRON.md file.

    Returns list of CronJob, or None if file doesn't exist or mount not
    configured. A thin wrapper over :func:`load_cron_document`, for the
    callers that want the jobs and nothing else.
    """
    doc = load_cron_document(config, user_id)
    return None if doc is None else doc.jobs


def _parse_jobs(
    data: dict, config, user_id: str, errors: list[str] | None = None,
    unread: set[str] | None = None,
) -> tuple[list[CronJob], int, frozenset[str]]:
    """Build the ``CronJob`` list from a parsed toml block. Never raises.

    Everything reachable from here is user-authored TOML, so nothing may
    assume a type: ``jobs`` is whatever the file says it is, an entry in it
    likewise, and every scalar goes through ``_str_field`` or
    ``_coerce_bool``. A malformed entry costs itself and no other.

    Returns the jobs **and how many entries were refused**, which is what
    lets ``CronDocument.states_no_jobs`` tell an empty job list from a job
    list nothing survived. The count is derived at the end rather than
    incremented at each of the six ``continue``s below, so a seventh cannot
    forget to keep it.

    The one exception is a ``prompt_file`` the path rule refuses
    (``refused``): that is the file stating a job that can never run, not a
    fault that may clear, so it must not hold the sync. Holding it kept the
    row a pre-ISSUE-596 read wrote, with another user's text as its prompt,
    running for as long as the file said what it says.

    ``errors``, when given, collects each drop and each ignored field as a
    sentence for the user, plus a non-admin ``command`` job: that one parses
    and is dropped at sync, so without saying so here the author never
    learns why it does not run.
    """
    if errors is None:
        errors = []
    jobs = []
    refused: set[str] = set()
    raw_jobs = data.get("jobs", [])
    if not isinstance(raw_jobs, list):
        logger.warning(
            "Ignoring 'jobs' in CRON.md for %s: expected an array of tables, "
            "got %s", user_id, type(raw_jobs).__name__,
        )
        errors.append("'jobs' must be an array of [[jobs]] tables")
        # One refused entry rather than none: the file did state something
        # under `jobs`, so this must not read as "the user has no jobs" and
        # authorize the orphan sweep.
        return jobs, 1, frozenset()
    for j in raw_jobs:
        if not isinstance(j, dict):
            logger.warning(
                "Skipping entry in CRON.md for %s: expected a [[jobs]] table, "
                "got %s", user_id, type(j).__name__,
            )
            errors.append("An entry under 'jobs' is not a [[jobs]] table")
            continue
        name = _str_field("", user_id, "name", j.get("name", ""), errors)
        cron = _str_field(name, user_id, "cron", j.get("cron", ""), errors)
        prompt = _str_field(name, user_id, "prompt", j.get("prompt", ""), errors)
        command = _str_field(name, user_id, "command", j.get("command", ""), errors)
        prompt_file = _str_field(
            name, user_id, "prompt_file", j.get("prompt_file", ""), errors)
        if not name or not cron:
            logger.warning(
                "Skipping incomplete job in CRON.md for %s: name=%r cron=%r",
                user_id, name, cron,
            )
            errors.append(
                f"Job '{name or '(unnamed)'}': needs both a name and a cron schedule"
            )
            continue
        # Resolve prompt_file to prompt contents
        if prompt_file:
            if prompt or command:
                logger.warning(
                    "Skipping job '%s' in CRON.md for %s: prompt_file cannot be combined with prompt or command",
                    name, user_id,
                )
                errors.append(
                    f"Job '{name}': prompt_file cannot be combined with "
                    "prompt or command"
                )
                continue
            text, reason, definitive = read_owner_text(config, user_id, prompt_file)
            if text is None:
                logger.warning(
                    "Skipping job '%s' in CRON.md for %s: cannot read prompt_file %s: %s",
                    name, user_id, prompt_file, reason,
                )
                errors.append(
                    f"Job '{name}': cannot read prompt_file {prompt_file}: {reason}"
                )
                if definitive:
                    refused.add(name)
                elif unread is not None:
                    unread.add(name)
                continue
            prompt = text.strip()
        if prompt and command:
            logger.warning(
                "Skipping job '%s' in CRON.md for %s: cannot have both prompt and command",
                name, user_id,
            )
            errors.append(f"Job '{name}': cannot have both prompt and command")
            continue
        if not prompt and not command:
            logger.warning(
                "Skipping job '%s' in CRON.md for %s: must have either prompt or command",
                name, user_id,
            )
            errors.append(f"Job '{name}': needs a prompt, a prompt_file or a command")
            continue
        model = _str_field(name, user_id, "model", j.get("model", ""), errors)
        effort = _str_field(name, user_id, "effort", j.get("effort", ""), errors)
        if effort:
            _validate_effort(name, user_id, effort)
        # Through `_str_field` like `model` and `effort` beside it. Read as
        # written, `brain = ["a"]` is a `TypeError` out of `_validate_brain`'s
        # frozenset membership, and the `try/except` around the parse closes
        # before this loop — so one job's typo costs every *other* job in the
        # file, which is the failure `_str_field` exists for. A scalar is
        # quieter and no better: it reaches a sqlite bind through
        # `_file_owned_values` and `brain = 5` is stored as the string "5".
        brain = _str_field(name, user_id, "brain", j.get("brain", ""), errors)
        if brain:
            _validate_brain(name, user_id, brain)
        # Admin-only like `brain` (`fj_provider_or_none`); read here so the
        # model is resolved within the provider the job will run on.
        provider = _str_field(name, user_id, "provider", j.get("provider", ""), errors)
        owner_is_admin = config.is_admin(user_id)
        model_refused = bool(model) and not _validate_model(
            name, user_id, model, config,
            brain if owner_is_admin else "",
            errors,
            provider=provider if owner_is_admin else "",
        )
        model_split = None
        if model and not model_refused:
            model_split = _split_model(
                model, config, brain if owner_is_admin else "",
                provider if owner_is_admin else "", owner_is_admin,
            )
        target = _str_field(name, user_id, "target", j.get("target", ""), errors)
        if target:
            _validate_target(name, user_id, target, config)
            _validate_room_pairing(
                name, user_id, target,
                _str_field(name, user_id, "room", j.get("room", "")),
            )
        declared, egress_kept = _validate_egress(
            name, config, user_id, j.get("egress", []), errors,
        )
        job = CronJob(
            name=name,
            cron=cron,
            prompt=prompt,
            command=command,
            prompt_file=prompt_file,
            target=target,
            room=_str_field(name, user_id, "room", j.get("room", ""), errors),
            enabled=_coerce_bool(
                name, user_id, "enabled", j.get("enabled", True), True, errors),
            silent_unless_action=_coerce_bool(
                name, user_id, "silent_unless_action",
                j.get("silent_unless_action", False), False, errors),
            skip_log_channel=_coerce_bool(
                name, user_id, "skip_log_channel",
                j.get("skip_log_channel", False), False, errors),
            once=_coerce_bool(
                name, user_id, "once", j.get("once", False), False, errors),
            model=model,
            effort=effort,
            brain=brain,
            provider=provider,
            # Through `_str_field` like its neighbours rather than `str()`:
            # this one names a shared_kv namespace, so `publish_shared_kv = 5`
            # coerced to the namespace "5" is the one string field where a
            # silent coercion has a consequence outside the job.
            publish_shared_kv=_str_field(
                name, user_id, "publish_shared_kv",
                j.get("publish_shared_kv", ""), errors),
            publish_shared_kv_trusted=_coerce_bool(
                name, user_id, "publish_shared_kv_trusted",
                j.get("publish_shared_kv_trusted", False), False, errors),
            reach=_validate_reach(name, user_id, j.get("reach"), errors),
            egress=declared,
            egress_kept=egress_kept,
            model_refused=model_refused,
            model_split=model_split,
        )
        if config is not None and fj_is_disallowed_command(job, config.is_admin(user_id)):
            errors.append(
                f"Job '{name}': command jobs are admin-only; use a prompt, or "
                "a plain `istota-skill <skill> ...` command"
            )
        jobs.append(job)

    return jobs, len(raw_jobs) - len(jobs) - len(refused), frozenset(refused)


# Every character TOML requires an escape for inside a basic string, plus the
# tab — legal raw, but invisible to whoever opens CRON.md next.
_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
    # Not a TOML rule, and no longer the load-bearing guard it was written
    # as. When ISSUE-385 added this, the fence was closed by the first ```
    # the reader saw anywhere, so a value holding three backticks truncated
    # the block mid-string and froze the whole schedule — that failure
    # arriving through the container rather than the grammar. ISSUE-386 then
    # anchored both markers to a line of their own, and every value here
    # renders on one line, so a rendered ``` can no longer begin one.
    # Kept as defence in depth, since it is one line and it stops either
    # half's assumption from silently becoming the other's problem. Escaped
    # as a code point, which ``tomllib`` decodes straight back, so the value
    # round-trips and only the rendered spelling changes.
    "`": "\\u0060",
}


def _toml_string(key: str, value: str) -> str:
    """Format a TOML key-value pair as an escaped single-line basic string.

    **Every string field goes through here**, including the ones that used to
    be interpolated raw (``name``, ``cron``, ``target``, ``room``, ``model``,
    ``effort``). A basic string interprets backslash escapes, so a value
    passed through untouched breaks the document three different ways, and
    only the first is loud (ISSUE-385):

    - an unescaped ``\\`` that is not a valid escape, a raw ``"``, or a raw
      control character make the line invalid, so ``tomllib`` refuses the
      whole fence and the user's entire schedule freezes until they repair
      the file by hand;
    - a backslash that *is* a valid escape (``\\b``, ``\\t``) parses cleanly and
      comes back as a different character, so the job silently becomes a
      different job;
    - the old triple-quoted branch ate a leading newline, because TOML trims
      one directly after the opening delimiter.

    That the list is round-tripped rather than merely written is what makes
    all three destructive: the daemon re-renders every job from the parsed
    list on any rewrite, so a value typed correctly by hand is destroyed the
    first time ``!cron disable`` touches the file.

    **One form, not two.** The triple-quoted branch this replaces was not a
    way out — it has the same escaping rule and failed the same way, and it
    carried two sub-rules of its own (a run of three quotes, a trailing
    quote) that are places for a bug to hide. A multiline value now renders
    on one line with ``\\n`` escapes, which is the whole cost, and it lands
    almost nowhere: ``_externalize_multiline_prompts`` moves a multiline
    ``prompt`` into ``scripts/prompts/*.txt`` before any write, so what
    reaches this with a newline in it is a multiline ``command`` — or, rarely,
    another field that a hand-written triple-quoted value put an interior
    newline into, since ``_str_field`` strips the ends and nothing else.
    """
    out = []
    for ch in value:
        escape = _TOML_ESCAPES.get(ch)
        if escape is not None:
            out.append(escape)
        elif ord(ch) < 0x20 or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return f'{key} = "{"".join(out)}"'


def render_jobs_block(jobs: list[CronJob]) -> str:
    """The toml source for a job list: no fence, no header, no document.

    This is the part of CRON.md that is *regenerated*. Everything else in the
    file — the header, prose above or below the fence, a second fence — is
    the user's and is spliced around this by ``_write_cron_md`` rather than
    rebuilt. Ends with a newline when there is anything to render and is
    empty otherwise, so that the closing fence lands on its own line either
    way.

    What it cannot preserve is anything inside the fence the parsed ``CronJob``
    list never held: a comment, a key the parser does not read, and a
    ``[[jobs]]`` table that was skipped (a typo in a key, an unreadable
    ``prompt_file``). The writers therefore do not render a document holding a
    skipped table (``document_hides_jobs``). Outside the fence is kept byte for
    byte, and inside it is regenerated.

    Two cosmetic limits of the splice, for the same reason: the rendered
    lines end in ``\\n``, so a CRLF document comes back with LF inside the
    fence and CRLF around it, and a fence indented inside a list item is
    re-emitted at column 0. Both still parse and both round-trip, so they are
    stated rather than handled.
    """
    lines = []

    for i, job in enumerate(jobs):
        if i > 0:
            lines.append("")
        lines.append("[[jobs]]")
        lines.append(_toml_string("name", job.name))
        lines.append(_toml_string("cron", job.cron))
        if job.command:
            lines.append(_toml_string("command", job.command))
        elif job.prompt_file:
            lines.append(_toml_string("prompt_file", job.prompt_file))
        else:
            lines.append(_toml_string("prompt", job.prompt))
        if job.target:
            lines.append(_toml_string("target", job.target))
        if job.room:
            lines.append(_toml_string("room", job.room))
        if not job.enabled:
            lines.append("enabled = false")
        if job.silent_unless_action:
            lines.append("silent_unless_action = true")
        if job.skip_log_channel:
            lines.append("skip_log_channel = true")
        if job.once:
            lines.append("once = true")
        if job.model:
            lines.append(_toml_string("model", job.model))
        if job.effort:
            lines.append(_toml_string("effort", job.effort))
        if job.brain:
            lines.append(_toml_string("brain", job.brain))
        if job.provider:
            lines.append(_toml_string("provider", job.provider))
        if job.publish_shared_kv:
            lines.append(_toml_string("publish_shared_kv", job.publish_shared_kv))
        if job.publish_shared_kv_trusted:
            lines.append("publish_shared_kv_trusted = true")
        if job.reach:
            lines.append(_toml_string("reach", job.reach))
        if job.egress or job.egress_kept:
            # `parse_entry`'s normal form holds nothing a basic string has to escape.
            quoted = ", ".join(f'"{entry}"' for entry in (*job.egress, *job.egress_kept))
            lines.append(f"egress = [{quoted}]")

    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def generate_cron_md(jobs: list[CronJob]) -> str:
    """Generate a whole CRON.md document from a list of CronJob definitions.

    Byte for byte what it always emitted. Used only where there is no
    existing document to splice into: the one-time migration from the table,
    and the seeded-template restore. Anywhere a file already exists, the
    splice in ``_write_cron_md`` is what runs.
    """
    return "# Scheduled Jobs\n\n```toml\n" + render_jobs_block(jobs) + "```\n"


def _prompt_file_name(name: str) -> str:
    """Return a readable filename for a generated cron prompt."""
    filename = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    filename = filename or "job"
    if len(filename.encode()) <= 200:
        return filename
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"{filename[:191]}-{digest}"


def _disambiguate_prompt_file_name(filename: str, name: str) -> str:
    """Add a stable job-name digest while keeping the filename bounded."""
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"{filename[:191]}-{digest}"


def _write_generated_prompt(dir_fd: int, filename: str, prompt: str) -> None:
    """Create a generated prompt file without replacing unrelated content.

    Opened relative to ``dir_fd`` with ``O_EXCL | O_NOFOLLOW``, so a link
    planted at the name is ``FileExistsError`` rather than followed. An
    existing file is compared through the same hardened reader the load path
    uses, and anything that is not this exact prompt raises.
    """
    from .skills._loader import read_overlay_bytes  # noqa: PLC0415 - import cycle

    data = prompt.encode("utf-8")
    try:
        fd = os.open(
            filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644,
            dir_fd=dir_fd,
        )
    except FileExistsError:
        existing, reason, _size = read_overlay_bytes(
            Path(filename), max_bytes=OWNER_FILE_MAX_BYTES, dir_fd=dir_fd,
        )
        if reason is not None or existing != data:
            raise
        return
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _externalize_multiline_prompts(config, user_id: str, jobs: list[CronJob]) -> None:
    """Move inline multiline prompts into files before CRON.md is rewritten.

    Raises ``OSError`` on a refusal, which ``_write_cron_md`` turns into a
    refused write. The reference written into CRON.md is checked against
    ``skill_host_paths.owner_path_parts`` before anything is created, so a file this
    writes is one ``storage.read_owner_text`` will read back.
    """
    prompts_dir_ref = f"{get_user_scripts_path(user_id, config.bot_dir_name)}/prompts"
    pending = [
        job for job in jobs
        if not (
            job.command
            or job.prompt_file
            or ("\r" not in job.prompt and "\n" not in job.prompt)
        )
    ]
    if not pending:
        return

    parts = owner_path_parts(f"{prompts_dir_ref}/x", user_id)
    root = config.workspace_root(user_id)
    if parts is None or root is None:
        raise PermissionError(f"prompts directory outside the owner's tree for {user_id}")
    dir_fd = _open_or_create_dirs(root, parts[:-1])
    try:
        assigned_names: dict[str, str] = {}
        for job in pending:
            stem = _prompt_file_name(job.name)
            if stem in assigned_names and assigned_names[stem] != job.name:
                stem = _disambiguate_prompt_file_name(stem, job.name)
            assigned_names[stem] = job.name
            filename = f"{stem}.txt"
            try:
                _write_generated_prompt(dir_fd, filename, job.prompt)
            except FileExistsError:
                digest = hashlib.sha256(job.prompt.encode()).hexdigest()[:8]
                filename = f"{stem}-{digest}.txt"
                _write_generated_prompt(dir_fd, filename, job.prompt)
            job.prompt_file = f"{prompts_dir_ref}/{filename}"
    finally:
        os.close(dir_fd)


def _write_cron_md(
    config,
    user_id: str,
    jobs: list[CronJob],
    doc: "CronDocument | None" = None,
    *,
    externalize_prompts: bool = True,
    block: str | None = None,
) -> bool:
    """Write CRON.md, externalizing inline multiline prompts first.

    ``block``, with a ``doc``, is spliced in as it stands instead of a rendering
    of ``jobs``.

    **With a ``doc``, only the toml block is replaced.** Every other byte of
    the file the caller read is written back unchanged: the header, a
    paragraph of the user's own notes above or below the fence, a second
    toml fence further down. Without one — the migration paths, where there
    is no document to preserve — it writes ``generate_cron_md`` as before.

    A ``doc`` whose ``block_span`` is ``None`` (a file with no fence at all)
    also takes the whole-document path, since there is nothing to splice
    into. Defensive rather than reached: the document-passing callers return
    False before the write when the file has no fence, because a document
    with no fence parses to no jobs and none of them finds the job it was
    asked to change.

    Through the contained directory and the hardened writer, like every other
    host-side write into `{bot_dir}/config/` (ISSUE-339): `mkdir(parents=True)`
    on an unresolved path follows a link at `config/`, and a plain `write_text`
    follows one at `CRON.md`.

    **Returns whether the file now says what the caller asked for**, which is
    what every caller was already claiming and none of them knew. This runs on
    an rclone FUSE mount, so a write is a thing that fails — and
    ``migrate_db_jobs_to_file``, ``update_job_enabled_in_cron_md`` and
    ``remove_job_from_cron_md`` below all returned an unconditional ``True``,
    so ``!cron disable`` reported success, disabled the table row alone, and
    the next sync tick read the unchanged file and switched the job back on
    (ISSUE-369).

    **False, never an exception**, which is the contract ``write_regular_file``
    already states and the reason the two steps in front of it are guarded
    here. ``write_regular_file`` is safe on its own; the two ``mkdir`` calls
    that precede it are not, and ``exist_ok`` covers only ``FileExistsError``
    — a directory that cannot be *created* (an unwritable parent, or the
    ``ENOTCONN``/``EIO`` a dropped FUSE mount answers with, which is exactly
    the failure this whole change is about) raised straight through. The
    scheduler's once-job caller is why that matters, and ISSUE-387 changed
    which failure it would cause. It used to run inside the still-open write
    transaction that had just deleted the job row, so an exception rolled the
    task's own completion back with it and the one-shot ran a second time. It
    now runs *after* that transaction has committed, so an exception instead
    escapes ``process_one_task`` with the task already recorded complete: the
    result is never delivered to Talk or email, no terminal task event is
    emitted, and nothing retries. Either way it is strictly worse than the
    file being stale, which is what the contract exists to guarantee — and the
    contract is restated on ``remove_job_from_cron_md``, since that is the
    name the scheduler actually depends on.

    One thing survives a ``False``: ``_externalize_multiline_prompts`` writes
    ``scripts/prompts/*.txt`` and stamps ``job.prompt_file`` before anything is
    written here, so a refused write leaves a prompt file no CRON.md refers
    to. Harmless and idempotent (``_write_generated_prompt`` compares content
    on a collision), but it is not evidence the write landed.
    """
    from .storage import resolve_user_config_dir, write_regular_file  # noqa: PLC0415

    try:
        if externalize_prompts:
            _externalize_multiline_prompts(config, user_id, jobs)
    except OSError as e:
        logger.warning(
            "cron_md_write_refused user=%s reason=prompt_externalize errno=%s",
            user_id, e.errno,
        )
        return False
    config_dir = resolve_user_config_dir(config, user_id)
    if config_dir is None:
        logger.warning(
            "cron_md_write_refused user=%s reason=config_dir_outside_user_tree", user_id,
        )
        return False
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(
            "cron_md_write_refused user=%s reason=config_dir_uncreatable errno=%s",
            user_id, e.errno,
        )
        return False
    return write_regular_file(config_dir / "CRON.md", _cron_md_content(jobs, doc, block))


def _cron_md_content(
    jobs: list[CronJob], doc: "CronDocument | None", block: str | None = None,
) -> str:
    """The whole file for this job list: spliced into ``doc``, or generated."""
    if doc is not None and doc.block_span is not None:
        start, end = doc.block_span
        new_block = render_jobs_block(jobs) if block is None else block
        return doc.content[:start] + new_block + doc.content[end:]
    return generate_cron_md(jobs)


# ---------------------------------------------------------------------------
# Column ownership
#
# Two authors write a ``scheduled_jobs`` row. The user authors the definition
# in CRON.md — name, schedule, what to run, where to send it, the flags — and
# the file is authoritative for all of it. The daemon authors the runtime
# state, and the table is authoritative for that.
#
# The split used to hold by omission: the state columns survived a sync
# because nobody had put them in the ``updates`` dict. Nothing said so and
# nothing checked, so the next column added to the table inherited whichever
# behaviour its author happened to give it. These three sets state the split,
# the sync writes exactly ``FILE_OWNED_COLUMNS`` on both paths rather than
# restating the list, and ``TestColumnOwnership`` requires the three to
# partition ``PRAGMA table_info(scheduled_jobs)`` exactly — so a column added
# to the table without an owner fails the suite.
#
# Two things the sets do not say, and a reader filing a new column needs
# both. They are about **who wins on a sync tick**, not about who may ever
# write a column: ``db.disable_scheduled_job`` writes ``enabled`` from the
# ``!cron disable`` verb, which writes the file in the same breath, so the
# sync reading it back is the mechanism working rather than an arbitration.
# The daemon's failure path writes ``auto_disabled_at`` instead and this sync
# never overwrites it. And they describe this sync alone —
# ``scheduler._sync_module_jobs`` is a third writer with its own rules for
# ``_module.*`` rows, which the sync filters out of both the file loop and the
# orphan sweep.
# ---------------------------------------------------------------------------

#: Columns CRON.md authors. The sync writes exactly these on every tick.
FILE_OWNED_COLUMNS = frozenset({
    "cron_expression",
    "prompt",
    "command",
    "skill",
    "skill_args",
    "conversation_token",
    "output_target",
    "enabled",
    "silent_unless_action",
    "skip_log_channel",
    "once",
    "model",
    "effort",
    "brain",
    "provider",
    "publish_shared_kv",
    "publish_shared_kv_trusted",
    "reach",
    "egress_request",
})

#: Columns the daemon authors. The sync never writes one *from the file*, and
#: the three exceptions are resets rather than values out of CRON.md: the
#: ``last_run_at`` reset on a cron-expression change or on an edit to what a
#: ``once`` job dispatches, the
#: ``auto_disabled_at`` / ``consecutive_failures`` / ``last_error`` reset on a
#: change to what the job dispatches, and the ``disabled_at`` reset when the
#: file says ``enabled = true``, which retracts a ``!cron disable``. All three
#: are appended to the SET clause at their own sites so they read as the
#: deliberate exceptions they are.
DAEMON_OWNED_COLUMNS = frozenset({
    "last_run_at",
    "last_success_at",
    "consecutive_failures",
    "last_error",
    # The scheduler's own disable. CRON.md has no way to express it, which is
    # correct: it is not the user's fact to state. The sync writes it in one
    # place only — clearing it when the user edits what the job dispatches, at
    # its own site below.
    "auto_disabled_at",
    # The `!cron disable` verb's own record (ISSUE-392). Filed here rather
    # than beside `enabled` because these sets are about who wins on a sync
    # tick and not about who may write a column: CRON.md has no way to say
    # "the verb did this", so the sync must not write it from the file. It
    # clears it in one place — with `enabled = 1`, at its own site below —
    # because a running job carrying a disable stamp is a lie to whoever reads
    # the column next. A file-authored disable is deliberately *not* stamped:
    # that fact is already recorded in the file.
    "disabled_at",
})

#: Row identity, never updated. ``user_id`` and ``name`` are written once at
#: insert; ``id`` and ``created_at`` come from the schema's own defaults.
IDENTITY_COLUMNS = frozenset({"id", "user_id", "name", "created_at"})

#: A change to one of these in CRON.md lifts a scheduler suspension — the user
#: has edited what the job actually runs, which is the plausible shape of a
#: fix. Deliberately narrower than ``FILE_OWNED_COLUMNS``; see the site in
#: :func:`sync_cron_jobs_to_db`. Each name is both a column and the
#: ``ScheduledJob`` attribute holding it, which is how the comparison there
#: reads them off the existing row.
#:
#: ``brain`` is outside it by decision, not by omission. The counter-argument
#: is real — a job suspended because its pinned brain is unavailable is the
#: case where changing the pin *is* the remedy — but ``model`` and ``effort``
#: are already outside for the same reason, and "what the job runs" is a rule
#: a reader can hold where "what the job runs, plus brain, but not model" is
#: not. ``!cron enable`` remains the documented remedy and the inbox already
#: raises the suspension.
_SUSPENSION_CLEARING_COLUMNS = (
    "cron_expression",
    "prompt",
    "command",
    "skill",
    "skill_args",
)


def _file_owned_values(
    fj: "CronJob",
    cmd_val: str | None,
    skill_val: str | None,
    skill_args_val: str | None,
    brain_val: str | None,
    provider_val: str | None = None,
) -> dict[str, object]:
    """What the file now says, for every column CRON.md authors.

    The keys are exactly ``FILE_OWNED_COLUMNS``, which
    ``test_every_file_owned_column_has_a_value`` is what holds: the sync
    projects this mapping through that set, so a column declared there and
    missing here is a ``KeyError`` rather than a column silently written as
    NULL. That is a test-time guarantee, not a runtime one — at runtime the
    scheduler's per-user ``except`` would swallow it into one log line.

    ``brain_val`` takes the same *shape* as ``cmd_val`` / ``skill_val`` — a
    parameter rather than a read off ``fj`` — for a different reason. Those
    three come from a shared resolver; this one depends on ``is_admin``, which
    this function does not receive.
    """
    return {
        "cron_expression": fj.cron,
        "prompt": fj.prompt,
        "command": cmd_val,
        "skill": skill_val,
        "skill_args": skill_args_val,
        "conversation_token": fj.room or None,
        "output_target": fj.target or None,
        # The file is authoritative for enabled state, symmetrically:
        # file false -> 0, file true -> 1.
        "enabled": 1 if fj.enabled else 0,
        "silent_unless_action": 1 if fj.silent_unless_action else 0,
        "skip_log_channel": 1 if fj.skip_log_channel else 0,
        "once": 1 if fj.once else 0,
        "model": None if fj.model_refused else (_stored_model(fj, provider_val)[0] or None),
        "effort": fj.effort or None,
        # Already gated by `fj_brain_or_none` at the call site — a non-admin's
        # pin arrives here as None and is written as NULL like any other
        # cleared field, rather than surviving by omission.
        "brain": brain_val,
        # Gated like `brain` (`fj_provider_or_none`); an admin's `provider/id`
        # in `model` may name it too (D22).
        "provider": None if fj.model_refused else _stored_model(fj, provider_val)[1],
        "publish_shared_kv": fj.publish_shared_kv or None,
        "publish_shared_kv_trusted": 1 if fj.publish_shared_kv_trusted else 0,
        "reach": fj.reach or egress.REACH_UNSTATED,
        "egress_request": json.dumps(list(fj.egress)) if fj.egress else None,
    }


def _stored_model(fj: "CronJob", provider_val: str | None) -> tuple[str, str | None]:
    """``(model, provider)`` the row stores for ``fj``: the split made at
    parse, with the owner's authority (a non-admin's never names a provider,
    since `select_for_person` refuses that), else the model as written beside
    ``provider_val``."""
    if fj.model_split is not None:
        return fj.model_split
    return fj.model, provider_val


def sync_cron_jobs_to_db(
    conn,
    user_id: str,
    file_jobs: list[CronJob],
    *,
    is_admin: bool = True,
    unread_names: frozenset[str] = frozenset(),
    ceiling: str = "limited",
) -> list[str]:
    """
    Sync CRON.md job definitions into the scheduled_jobs DB table.

    Returns the egress subjects with a new refusal row, for the caller to hand to
    `rooms.egress.raise_refused_bell` once this has committed.

    - New jobs are inserted
    - Existing jobs have definition fields updated (preserving state fields)
    - Orphaned DB jobs (not in file) are deleted
    - enabled logic: file is authoritative (symmetric: file false → DB 0, file true → DB 1)
    - a scheduler suspension (``auto_disabled_at``) survives every tick, and is
      lifted — with the failure count — only when the file changes what the job
      dispatches
    - command-type jobs are rejected for non-admin users (arbitrary user
      shell-command tasks must remain admin-only)
    - a new job has no egress grants; an edit to what it dispatches drops its
      `open:` and `public:` approvals; a removed job takes its rows with it; and
      `reach` and `egress` ask the owner for what ``ceiling``, the deployment's
      `scheduled_job_max_unapproved`, does not cover, and grant nothing
      (`_sync_job_approvals`)
    - ``unread_names`` are jobs whose `prompt_file` could not be read this tick:
      kept, not orphaned

    Both write paths name exactly ``FILE_OWNED_COLUMNS``, so the daemon's
    state columns are left alone rather than surviving by omission.
    """
    # Constant per call; sorted only so the generated clauses are stable.
    from istota.briefings.group_migrate import publisher_retired

    file_columns = sorted(FILE_OWNED_COLUMNS)
    db_jobs = db.get_user_scheduled_jobs(conn, user_id)
    # Module-managed jobs are owned by their integration (see jobs.py in the
    # respective module package); CRON.md must not touch them.
    db_jobs = [j for j in db_jobs if not j.name.startswith(_MODULE_JOB_PREFIX)]
    db_by_name = {j.name: j for j in db_jobs}
    file_names = {j.name for j in file_jobs if not (fj_is_disallowed_command(j, is_admin))}
    file_names |= unread_names
    refused_subjects: list[str] = []

    for fj in file_jobs:
        if fj.name.startswith(_MODULE_JOB_PREFIX):
            logger.warning(
                "Skipping CRON.md job '%s' for %s: '%s' prefix is reserved",
                fj.name, user_id, _MODULE_JOB_PREFIX,
            )
            continue
        if fj_is_disallowed_command(fj, is_admin):
            logger.warning(
                "Skipping CRON.md job '%s' for %s: command-type jobs are admin-only",
                fj.name, user_id,
            )
            continue
        cmd_val, skill_val, skill_args_val = _resolve_job_dispatch(fj)
        brain_val = fj_brain_or_none(fj, is_admin)
        if fj.brain and brain_val is None:
            logger.warning(
                "Dropping brain pin %r on CRON.md job '%s' for %s: pinning a "
                "brain is admin-only; the job will run the configured brain",
                fj.brain, fj.name, user_id,
            )
        provider_val = fj_provider_or_none(fj, is_admin)
        if fj.provider and provider_val is None:
            logger.warning(
                "Dropping provider %r on CRON.md job '%s' for %s: choosing a "
                "provider is admin-only; the job will run native's default",
                fj.provider, fj.name, user_id,
            )
        # The definition, as the file now states it. Both paths below write
        # exactly the file-owned columns and name no other, which is what
        # leaves the daemon's state columns alone.
        file_values = _file_owned_values(
            fj, cmd_val, skill_val, skill_args_val, brain_val, provider_val,
        )
        if publisher_retired(conn, user_id, fj.name):
            file_values["enabled"] = 0
        existing = db_by_name.get(fj.name)
        if _sync_job_approvals(conn, user_id, fj, existing, file_values, ceiling):
            refused_subjects.append(egress.cron_subject(fj.name))
        if existing:
            # Update definition fields, preserve state
            updates = {col: file_values[col] for col in file_columns}

            # Reset last_run_at when cron expression changes to prevent
            # catch-up runs for past slots in the new expression
            cron_changed = fj.cron != existing.cron_expression
            if cron_changed:
                logger.info(
                    "Cron expression changed for job '%s' (user %s): "
                    "'%s' -> '%s', resetting last_run_at",
                    fj.name, user_id, existing.cron_expression, fj.cron,
                )

            # An edit to what the job *dispatches* lifts a suspension. Without
            # this the split is a worse trap than the bug it fixes: today a
            # user whose job is failing edits the prompt and it runs again on
            # the next tick, because the suspension never held. After the
            # split it would stay suspended silently, and the only way out
            # would be a chat verb they may not know exists.
            #
            # These five fields and no others. Changing `target`, `room`,
            # `model`, `effort`, `brain` or a flag is not plausibly a fix for a
            # job that is failing, and a rule keyed on the dispatch fields is
            # one a reader can hold without consulting the frozenset. See the
            # note on `brain` at the frozenset for the one arguable case.
            #
            # Ungated on the row's current state, deliberately, because the
            # edit is visible for exactly one tick: this same UPDATE writes the
            # file's values into the row, so on every later tick the two agree
            # and there is no change left to read. Gating on
            # `existing.auto_disabled_at` therefore loses the edit whenever the
            # suspension lands *after* the tick that carried it — an ordinary
            # sequence, not a tight race, since a task queued against the old
            # definition can be in flight for minutes: the user fixes the
            # prompt, the sync writes it, the old run posts failure N, the job
            # suspends on evidence from a definition that no longer exists, and
            # the documented remedy is spent. Clearing an already-NULL column
            # is a no-op, and clearing a suspension written between this read
            # and this write is the right answer for the same reason.
            #
            # The failure count goes with it. The other two lifts both zero it
            # (`enable_scheduled_job`, `reset_scheduled_job_failures`) and
            # leaving it would make this one nominal: a job resumed at
            # `consecutive_failures = max` is re-suspended by its first bad run
            # afterwards, so the user gets one attempt rather than a working
            # job. The counter is a count of failures against a definition the
            # file no longer contains.
            changed_dispatch = [
                col for col in _SUSPENSION_CLEARING_COLUMNS
                if file_values[col] != getattr(existing, col)
            ]
            if changed_dispatch and existing.auto_disabled_at:
                logger.info(
                    "Lifting suspension on job '%s' (user %s): %s changed in "
                    "CRON.md (suspended at %s)",
                    fj.name, user_id, ", ".join(changed_dispatch),
                    existing.auto_disabled_at,
                )

            if (
                skill_val
                and existing.command
                and not existing.skill
            ):
                logger.info(
                    "Promoting CRON.md job '%s' (user %s) from command-task "
                    "to skill-task: skill=%s",
                    fj.name, user_id, skill_val,
                )

            set_parts = [f"{k} = ?" for k in updates]
            values = list(updates.values())
            if cron_changed:
                set_parts.append("last_run_at = datetime('now')")
            elif fj.once and existing.once and egress.task_text_digest(
                file_values,
            ) != egress.task_text_digest(
                {column: getattr(existing, column) for column in egress.TASK_TEXT_COLUMNS},
            ):
                # The slot an earlier occurrence spent was not spent on this text. A
                # dated `once` job would otherwise next come due in a year (#920).
                set_parts.append("last_run_at = NULL")
            if file_values["enabled"]:
                # The third deliberate exception, and a reset rather than a
                # value out of the file: `disabled_at` records that the
                # `!cron disable` verb switched this job off, so a file that
                # now says `enabled = true` has retracted it. Leaving the
                # stamp on a running job would misreport who last stopped it.
                # Unconditional because clearing an already-NULL column is a
                # no-op, which is cheaper than reading the row to find out.
                set_parts.append("disabled_at = NULL")
            if changed_dispatch:
                set_parts.append("auto_disabled_at = NULL")
                set_parts.append("consecutive_failures = 0")
                set_parts.append("last_error = NULL")
            set_clause = ", ".join(set_parts)
            values.append(existing.id)
            conn.execute(
                f"UPDATE scheduled_jobs SET {set_clause} WHERE id = ?",
                values,
            )
        else:
            if skill_val:
                logger.info(
                    "Inserting CRON.md job '%s' (user %s) as skill-task: skill=%s",
                    fj.name, user_id, skill_val,
                )
            # Insert new job: identity, then every file-owned column. The
            # daemon-owned columns take their schema defaults.
            columns = ["user_id", "name", *file_columns]
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"""INSERT INTO scheduled_jobs ({", ".join(columns)})
                    VALUES ({placeholders})""",
                [user_id, fj.name, *(file_values[col] for col in file_columns)],
            )

    # Delete orphaned DB jobs (not in file)
    for db_job in db_jobs:
        if db_job.name not in file_names:
            db.delete_scheduled_job(conn, db_job.id)
            logger.info(
                "Removed orphaned scheduled job '%s' for user %s",
                db_job.name, user_id,
            )

    conn.commit()
    return refused_subjects


def _sync_job_approvals(
    conn, user_id: str, fj: "CronJob", existing, file_values: dict[str, object],
    ceiling: str = "limited",
) -> bool:
    """What this tick's edit does to a job's egress grants. True if it asked the
    owner something new.

    Compared against the row before the sync's own write, so an edit is seen on
    the one tick it is visible. A `reach` the ceiling does not cover is asked whenever
    no row holds it, so the request returns if the ceiling narrows; a dismissed row is
    a row, so a dismissed request stays dismissed. A row just inserted starts from
    nothing, whatever an earlier job of that name left behind.

    An edit whose new text already holds its grant asks nothing: the owner kept the
    approval when saving on the page, which may land on either side of this tick.
    """
    from istota.notifications.resolvers import egress_refused

    subject = egress.cron_subject(fj.name)
    digest = egress.job_digest(file_values)
    if existing is None:
        egress.forget_subject(conn, user_id, subject)
        dropped: list[str] = []
        text_changed = True
        old_request = None
    else:
        old_request = egress.REACH_REQUESTS.get(existing.reach)
        old = {column: getattr(existing, column) for column in egress.DIGEST_COLUMNS}
        text_changed = egress.job_digest(old) != digest
        dropped = (
            egress.drop_job_approvals(conn, user_id, subject, keep_digest=digest)
            if text_changed else []
        )
    request = egress.REACH_REQUESTS.get(fj.reach)
    runs_as = egress.unapproved_reach(fj.reach, ceiling)
    if old_request and old_request != request and old_request not in dropped:
        egress.clear_refused(conn, user_id, subject, old_request)
    # The deployment covers it: nothing is left to ask, whatever an edit or an earlier
    # tick did.
    covered = egress.REACH_REQUESTS.get(runs_as)
    grants = egress.grants_for(conn, user_id, subject)
    ask = [
        entry for entry in dropped
        if entry != covered and f"{entry}:{digest}" not in grants
    ]
    if covered:
        egress.clear_refused(conn, user_id, subject, covered)
    if request and request != covered and request not in ask:
        rows = {row["entry"] for row in egress.list_refused(conn, user_id, subject)}
        if (
            f"{request}:{digest}" not in grants
            and (text_changed or request not in rows)
        ):
            ask.append(request)
    for entry in ask:
        egress.note_refused(conn, user_id, subject, entry, reopen=True)
    declared_new = egress.sync_declared(
        conn, user_id, subject, file_values["egress_request"],
        existing.egress_request if existing is not None else None,
        ask=runs_as == egress.REACH_LIMITED,
    )
    egress_refused.resolve_if_settled(conn, user_id, subject, by="system")
    return bool(ask) or declared_new


def migrate_db_jobs_to_file(conn, config, user_id: str, overwrite: bool = False) -> bool:
    """
    Generate CRON.md from existing DB jobs (one-time migration).

    Args:
        overwrite: If True, overwrite an existing file (used when file exists
                   but is empty/template-only while DB has real jobs).

    Returns True if a file was written — which now includes the write
    itself having succeeded, not just having been attempted.

    Under the file's lock, and the "absent or template" judgement is made again
    inside it: the sync decided to restore from a read taken outside the lock,
    and a web save of real jobs in between must not be overwritten by the
    table.
    """
    if not config.has_workspace:
        return False

    from .storage import resolve_user_config_dir  # noqa: PLC0415 - import cycle

    config_dir = resolve_user_config_dir(config, user_id)
    if config_dir is None:
        return False
    try:
        with text_files.file_lock(config, user_id, config_dir / "CRON.md"):
            return _migrate_db_jobs_locked(conn, config, user_id, config_dir, overwrite)
    except MemoryMdLocked:
        logger.warning("cron_md_write_refused user=%s reason=locked", user_id)
        return False
    except OSError as e:
        # The anchor could not be made or opened. False, never an exception:
        # the contract `remove_job_from_cron_md`'s caller depends on.
        logger.warning(
            "cron_md_write_refused user=%s reason=lock_unavailable errno=%s",
            user_id, e.errno,
        )
        return False


def _migrate_db_jobs_locked(conn, config, user_id: str, config_dir, overwrite: bool) -> bool:
    if (config_dir / "CRON.md").exists():
        if not overwrite:
            return False
        current = load_cron_document(config, user_id)
        if current is None or not current.is_template:
            return False

    db_jobs = db.get_user_scheduled_jobs(conn, user_id)
    # Module-managed jobs are owned by their integrations and must not be
    # serialized into CRON.md.
    db_jobs = [j for j in db_jobs if not j.name.startswith(_MODULE_JOB_PREFIX)]
    if not db_jobs:
        return False

    file_jobs = []
    for j in db_jobs:
        # Round-trip skill-task rows back to the CRON.md ``command:`` shape
        # operators write. ``sync_cron_jobs_to_db`` will re-parse and
        # re-promote them on the next sync — idempotent.
        cmd = j.command or ""
        if not cmd and j.skill:
            try:
                args = json.loads(j.skill_args or "[]")
            except (ValueError, TypeError):
                args = []
            if isinstance(args, list) and all(isinstance(a, str) for a in args):
                cmd = " ".join(
                    shlex.quote(t) for t in [_SKILL_CLI_NAME, j.skill, *args]
                )
        file_jobs.append(CronJob(
            name=j.name,
            cron=j.cron_expression,
            prompt=j.prompt,
            command=cmd,
            target=j.output_target or "",
            room=j.conversation_token or "",
            enabled=j.enabled,
            silent_unless_action=j.silent_unless_action,
            skip_log_channel=j.skip_log_channel,
            once=j.once,
            model=j.model or "",
            effort=j.effort or "",
            brain=j.brain or "",
            provider=j.provider or "",
            publish_shared_kv=j.publish_shared_kv or "",
            publish_shared_kv_trusted=bool(j.publish_shared_kv_trusted),
            reach="" if j.reach == egress.REACH_UNSTATED else j.reach,
            egress=egress.declared_entries(j.egress_request),
        ))

    if not _write_cron_md(config, user_id, file_jobs):
        return False
    logger.info(
        "Migrated %d DB scheduled job(s) to CRON.md for user %s",
        len(file_jobs), user_id,
    )
    return True


def _with_cron_md_lock(config, user_id: str, fn) -> bool:
    """Run a CRON.md read-modify-write under the file's lock. False if held.

    The read has to be inside the lock, not only the write: a web save landing
    between the two would otherwise be overwritten by a document built from
    the text it replaced.
    """
    from .storage import resolve_user_config_dir  # noqa: PLC0415 - import cycle

    config_dir = resolve_user_config_dir(config, user_id)
    if config_dir is None:
        return False
    try:
        with text_files.file_lock(config, user_id, config_dir / "CRON.md"):
            return fn()
    except MemoryMdLocked:
        logger.warning("cron_md_write_refused user=%s reason=locked", user_id)
        return False
    except OSError as e:
        # The anchor could not be made or opened. False, never an exception:
        # the contract `remove_job_from_cron_md`'s caller depends on.
        logger.warning(
            "cron_md_write_refused user=%s reason=lock_unavailable errno=%s",
            user_id, e.errno,
        )
        return False


def update_job_enabled_in_cron_md(config, user_id: str, job_name: str, enabled: bool) -> bool:
    """
    Update a job's enabled state in the user's CRON.md file.

    Loads the document, updates the target job's enabled field, and splices
    the regenerated toml block back into it — so a user's own notes around
    the fence survive a ``!cron disable``.

    Returns True if the job was found **and the file now says so**: a refused
    or failed write is False, so a caller that reports success is reporting
    the file's state rather than its own intention (ISSUE-369).
    """
    if not config.has_workspace:
        return False
    return _with_cron_md_lock(
        config, user_id,
        lambda: _update_job_enabled_locked(config, user_id, job_name, enabled),
    )


def _update_job_enabled_locked(config, user_id: str, job_name: str, enabled: bool) -> bool:
    doc = load_cron_document(config, user_id)
    if doc is None or document_hides_jobs(doc):
        return False
    jobs = doc.jobs

    found = False
    for job in jobs:
        if job.name == job_name:
            job.enabled = enabled
            found = True
            break

    if not found:
        return False

    if not _write_cron_md(config, user_id, jobs, doc):
        return False
    logger.info(
        "%s job '%s' in CRON.md for user %s",
        "Enabled" if enabled else "Disabled", job_name, user_id,
    )
    return True


def update_job_reach_in_cron_md(config, user_id: str, job_name: str, reach: str) -> bool:
    """Set a job's `reach` in CRON.md. True only if the file now says so.

    False, with the file untouched, when an entry could not be read: the rewrite is
    built from the jobs that parsed, so it would delete a job with a typo or one whose
    `prompt_file` a mount fault hid, and this is a settings page, not an edit of the file.
    """
    if not config.has_workspace:
        return False
    return _with_cron_md_lock(
        config, user_id,
        lambda: _update_job_reach_locked(config, user_id, job_name, reach),
    )


def document_hides_jobs(doc: "CronDocument") -> bool:
    """Whether the file holds a job ``doc.jobs`` lacks. A rewrite is built from the jobs
    that parsed, so writing this document would delete that job from the file."""
    return bool(doc.skipped_entries or doc.refused_names or doc.unread_names)


def _update_job_reach_locked(config, user_id: str, job_name: str, reach: str) -> bool:
    doc = load_cron_document(config, user_id)
    if doc is None or document_hides_jobs(doc):
        return False
    for job in doc.jobs:
        if job.name == job_name:
            job.reach = reach
            return _write_cron_md(config, user_id, doc.jobs, doc)
    return False


def remove_job_from_cron_md(config, user_id: str, job_name: str) -> bool:
    """
    Remove a job by name from the user's CRON.md file.

    Loads the document, filters out the named job, and splices the
    regenerated toml block back into it, leaving the rest of the file alone.
    Returns True if the job was found **and the file now says so**; a refused
    or failed write is False, like ``update_job_enabled_in_cron_md``.

    A file holding an entry that did not load is not rewritten from the jobs that
    parsed, which would delete that entry. Only the named job's table is cut out
    of the block (``_cut_job_from_block``). Refusing instead would leave a finished
    ``once`` job in the file, where the next sync re-creates its row and runs it
    again (ISSUE-917).

    **False, never an exception**, on every path. ``_write_cron_md`` states
    that contract and explains why it matters; ``remove_finished_once_job``
    shares this path, so it is restated here. The two steps in front of that
    call hold it too: ``load_cron_document`` returns ``None`` for every case the
    file cannot be read as one (its reader, ``storage.read_user_config_file``,
    logs a refusal rather than raising), and filtering the job list cannot fail.
    The scheduler's once-job caller runs after its write transaction has
    committed (ISSUE-387), so an exception here would leave the task recorded
    complete and its answer undelivered.
    """
    if not config.has_workspace:
        return False
    return _with_cron_md_lock(
        config, user_id, lambda: _remove_job_locked(config, user_id, job_name),
    )


def _entry_names_job(entry, job_name: str) -> bool:
    if not isinstance(entry, dict):
        return False
    name = entry.get("name")
    return isinstance(name, str) and name.strip() == job_name


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _cut_job_from_block(block: str, job_name: str) -> str | None:
    """``block`` without the named job's tables, everything else kept.

    None when the job is not there or the text cannot be split with certainty.
    Tables are found by their header lines, which a multi-line string can imitate,
    so the result is parsed back and must state exactly the same document minus
    those entries.

    A comment after a removed table stays. If the result states nothing it is
    empty, comments included: a block of only comments reads as the seeded template
    (``CronDocument.is_template``), and the sync would restore the file from the table.
    """
    data = tomli.loads(block)
    raw_jobs = data.get("jobs")
    if not isinstance(raw_jobs, list):
        return None
    if not any(_entry_names_job(entry, job_name) for entry in raw_jobs):
        return None

    lines = block.splitlines(keepends=True)
    headers = [i for i, line in enumerate(lines) if _JOBS_HEADER_RE.match(line)]
    if len(headers) != len(raw_jobs):
        return None
    ends = headers[1:] + [len(lines)]
    kept = lines[:headers[0]]
    for entry, start, end in zip(raw_jobs, headers, ends):
        if not _entry_names_job(entry, job_name):
            kept.extend(lines[start:end])
            continue
        table_end = end
        while table_end > start + 1 and _is_blank_or_comment(lines[table_end - 1]):
            table_end -= 1
        trailing = lines[table_end:end]
        if any(line.strip() for line in trailing):
            kept.extend(trailing)
    new_block = "".join(kept)

    expected = dict(data)
    expected["jobs"] = [e for e in raw_jobs if not _entry_names_job(e, job_name)]
    try:
        reparsed = tomli.loads(new_block)
    except Exception:
        return None
    reparsed.setdefault("jobs", [])
    if reparsed != expected:
        return None
    return new_block if reparsed != {"jobs": []} else ""


def _document_names_job(doc: "CronDocument", job_name: str) -> bool:
    """Whether the block states this job, loaded or not."""
    if doc.block is None:
        return False
    raw_jobs = tomli.loads(doc.block).get("jobs")
    return isinstance(raw_jobs, list) and any(
        _entry_names_job(entry, job_name) for entry in raw_jobs
    )


ONCE_REMOVED = "removed"
ONCE_EDITED = "edited"
ONCE_NOT_REMOVED = "not_removed"


def remove_finished_once_job(config, user_id: str, job_name: str, ran_text: str) -> str:
    """Take a finished ``once`` job out of CRON.md, unless its entry has been edited.

    ``ran_text`` is ``egress.task_text_digest`` of what the occurrence ran. An entry
    saying anything else is text that has not run, including an edit no sync has read
    yet, so it stays and the answer is ``ONCE_EDITED``. So does an entry no longer
    marked ``once``. A `prompt_file` that could not be read this tick says nothing about
    an edit, and is removed as before. Compared under the file's lock, so a save that
    takes the lock cannot land between this and the write.

    An entry that names the job and did not load is left in the file and answers
    ``ONCE_NOT_REMOVED``, because it may be an edit nobody can read yet. The row still
    goes: the sync would hold it unchanged where the entry is the file's only job, and
    it would fire the text that just ran at every slot until the file is fixed.

    ``ONCE_REMOVED``, or ``ONCE_NOT_REMOVED`` wherever ``remove_job_from_cron_md`` says
    False, and it holds the same never-raises contract.
    """
    if not config.has_workspace:
        return ONCE_NOT_REMOVED
    outcome = ONCE_NOT_REMOVED

    def locked() -> bool:
        nonlocal outcome
        doc = load_cron_document(config, user_id)
        if doc is None:
            return False
        job = next((j for j in doc.jobs if j.name == job_name), None)
        if job is None:
            if job_name not in doc.unread_names and _document_names_job(doc, job_name):
                return False
        else:
            cmd_val, skill_val, skill_args_val = _resolve_job_dispatch(job)
            if not job.once or egress.task_text_digest(_file_owned_values(
                job, cmd_val, skill_val, skill_args_val, None,
            )) != ran_text:
                outcome = ONCE_EDITED
                return False
        if _remove_job_from_document(config, user_id, job_name, doc):
            outcome = ONCE_REMOVED
            return True
        return False

    _with_cron_md_lock(config, user_id, locked)
    return outcome


def _remove_job_locked(config, user_id: str, job_name: str) -> bool:
    doc = load_cron_document(config, user_id)
    if doc is None:
        return False
    return _remove_job_from_document(config, user_id, job_name, doc)


def _remove_job_from_document(config, user_id: str, job_name: str, doc: "CronDocument") -> bool:
    if document_hides_jobs(doc):
        block = _cut_job_from_block(doc.block or "", job_name)
        if block is None:
            if _document_names_job(doc, job_name):
                logger.warning(
                    "cron_md_write_refused user=%s reason=entry_not_separable job=%s",
                    user_id, job_name,
                )
            return False
        if not _write_cron_md(config, user_id, [], doc, block=block):
            return False
        logger.info("Removed job '%s' from CRON.md for user %s", job_name, user_id)
        return True

    jobs = doc.jobs
    original_count = len(jobs)
    jobs = [j for j in jobs if j.name != job_name]
    if len(jobs) == original_count:
        return False  # Job not found

    if not _write_cron_md(config, user_id, jobs, doc):
        return False
    logger.info("Removed job '%s' from CRON.md for user %s", job_name, user_id)
    return True


def set_job_enabled(config, conn, job, enabled: bool, *, by: str) -> str:
    """Switch one job on or off: the verb behind `!cron enable|disable` and the admin pane.

    CRON.md first and the row second, since the sync reads the file back on
    every tick. Returns ``"module"`` for a `_module.*` row, which is in nobody's
    CRON.md and whose table row is the only authority (ISSUE-392); ``"file"``
    when CRON.md now says so; ``"db_only"`` when it does not (no file, no such
    job in it, an entry that did not load, or a refused write), where the next
    sync reverts the row.
    """
    from istota.notifications.resolvers import cron_job as cron_job_source  # noqa: PLC0415

    if job.name.startswith(_MODULE_JOB_PREFIX):
        outcome = "module"
    elif update_job_enabled_in_cron_md(config, job.user_id, job.name, enabled):
        outcome = "file"
    else:
        outcome = "db_only"
    if enabled:
        db.enable_scheduled_job(conn, job.id)
    else:
        db.disable_scheduled_job(conn, job.id)
    # Closed on disable too: the inbox row an earlier suspension raised would
    # otherwise keep asking the user to re-enable a job switched off on purpose.
    cron_job_source.resolve_for_job(conn, job.user_id, job.id, by=by)
    return outcome


def remove_job_definition(config, user_id: str, job_name: str) -> bool:
    """Take a job out of CRON.md so the sync will not re-create its row.

    True when the file no longer names the job: it was removed, or it was never
    there (no workspace, no file, a row the file does not carry). False when the
    file still names it or exists and cannot be read, which is when deleting the
    row would be undone by the next sync. Must not be called inside a write
    transaction: the write lands on the mount (ISSUE-387).
    """
    if not config.has_workspace or remove_job_from_cron_md(config, user_id, job_name):
        return True
    doc = load_cron_document(config, user_id)
    if doc is not None:
        return not _document_names_job(doc, job_name)
    from .storage import resolve_user_config_dir  # noqa: PLC0415 - import cycle

    config_dir = resolve_user_config_dir(config, user_id)
    return config_dir is None or not os.path.lexists(config_dir / "CRON.md")


# ---------------------------------------------------------------------------
# The page's writer: one whole job definition
#
# `validate_job_input` turns a form's fields into a `CronJob`, and
# `save_job_definition` splices it in under the file's lock. Refusal text goes
# back to the browser, so it names no value from the body, and the checks shared
# with the parser are handed `_FORM_JOB` rather than the job's name, since they
# log what they are given and a job name is model-writable.
# ---------------------------------------------------------------------------

_FORM_JOB = "(page)"
_MAX_JOB_NAME_CHARS = 100
_MAX_JOB_PROMPT_BYTES = 48 * 1024
_FORM_REACHES = ("limited", "public", "full")
_ROUTE_REFUSED = (
    "A job cannot deliver there. Choose an output this deployment has, or one of "
    "your own rooms that nobody else reads."
)


class JobFieldError(ValueError):
    """One field of a job the page sent cannot be saved. The text is for the person."""

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(message)
        self.field = field_name
        self.message = message


@dataclass(frozen=True)
class JobSaveOutcome:
    status: str  # ok | locked | unreadable | changed | name_taken | not_found | too_large | invalid | failed
    errors: tuple[str, ...] = ()


def job_revision(job: CronJob) -> str:
    """What the page read, for the writer to compare. The job as the file renders it,
    plus the text a `prompt_file` holds, which the rendering only names: the form shows
    that text, so an edit of the file behind the page is a change too."""
    digest = hashlib.sha256(render_jobs_block([job]).encode("utf-8", "surrogatepass"))
    if job.prompt_file:
        digest.update(job.prompt.encode("utf-8", "surrogatepass"))
    return digest.hexdigest()


def job_text_digest(job: CronJob, *, is_admin: bool) -> str:
    """The digest the next sync will compute for this job, by the sync's own steps."""
    cmd_val, skill_val, skill_args_val = _resolve_job_dispatch(job)
    return egress.job_digest(_file_owned_values(
        job, cmd_val, skill_val, skill_args_val,
        fj_brain_or_none(job, is_admin), fj_provider_or_none(job, is_admin),
    ))


def _form_text(payload: dict, key: str, default: str) -> str:
    """A text field, stripped as the parser strips it, so the digest taken now is the
    one the sync computes. Absent keeps ``default``, and null is a clear."""
    if key not in payload:
        return default
    value = payload[key]
    if value is None:
        return ""
    if not isinstance(value, str):
        raise JobFieldError(key, "This must be text.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise JobFieldError(key, "This holds characters that cannot be stored.") from None
    return value.strip()


def _form_bool(payload: dict, key: str, default: bool) -> bool:
    if key not in payload:
        return default
    value = payload[key]
    if not isinstance(value, bool):
        raise JobFieldError(key, "This must be true or false.")
    return value


def _check_job_name(name: str) -> None:
    if not name:
        raise JobFieldError("name", "A job needs a name.")
    if len(name) > _MAX_JOB_NAME_CHARS:
        raise JobFieldError("name", f"A job name is at most {_MAX_JOB_NAME_CHARS} characters.")
    if any(unicodedata.category(ch) == "Cc" for ch in name):
        raise JobFieldError("name", "A job name cannot hold control characters.")
    if name.startswith(_MODULE_JOB_PREFIX):
        raise JobFieldError("name", f"Names starting with {_MODULE_JOB_PREFIX} are reserved.")


def _apply_job_text(job: CronJob, payload: dict, stored: CronJob | None, is_admin: bool) -> None:
    """The prompt or command. Unchanged text on a `prompt_file` job keeps the reference;
    changed text clears it, the writer externalizes as usual, and the old file stays."""
    kind_default = "" if stored is None else ("command" if stored.command else "prompt")
    kind = _form_text(payload, "kind", kind_default)
    if kind == "prompt":
        # The newlines `read_owner_text` hands back from an externalized prompt, or the
        # digest taken now would not be the one the sync computes.
        prompt = _form_text(payload, "prompt", job.prompt).replace("\r\n", "\n").replace("\r", "\n")
        if not prompt:
            raise JobFieldError("prompt", "A prompt job needs a prompt.")
        if stored is None or prompt != stored.prompt:
            if len(prompt.encode("utf-8")) > _MAX_JOB_PROMPT_BYTES:
                raise JobFieldError("prompt", "A prompt is at most 48 KiB.")
            job.prompt_file = ""
        job.prompt = prompt
        job.command = ""
    elif kind == "command":
        command = _form_text(payload, "command", job.command)
        if not command:
            raise JobFieldError("command", "A command job needs a command.")
        job.command = command
        job.prompt = ""
        job.prompt_file = ""
        if fj_is_disallowed_command(job, is_admin):
            raise JobFieldError(
                "command",
                "Command jobs are admin-only. Use a prompt, or a plain "
                "`istota-skill <skill> ...` command.",
            )
    else:
        raise JobFieldError("kind", "A job is a prompt or a command.")


def _check_job_target(config, user_id: str, target: str) -> None:
    """A target the page may save. A `room:<token>` leaf delivers into that room as a
    `web:<token>` leaf does, so it takes the same room check, which the surface list and
    the descriptor check do not otherwise apply to it."""
    from .transport import parse_output_target
    from .transport.route_checks import validate_descriptor_rooms, validate_descriptor_surfaces

    leaves = []
    for leaf in parse_output_target(target):
        if leaf.surface == "room":
            if not leaf.channel:
                raise JobFieldError("target", "A room target names its room.")
            leaves.append(f"web:{leaf.channel}")
        else:
            leaves.append(f"{leaf.surface}:{leaf.channel}" if leaf.channel else leaf.surface)
    descriptor = ",".join(leaves)
    try:
        validate_descriptor_surfaces(config, descriptor)
        validate_descriptor_rooms(config, descriptor, user_id)
    except ValueError:
        raise JobFieldError("target", _ROUTE_REFUSED) from None


def _job_room(
    config, user_id: str, payload: dict, job: CronJob, stored: CronJob | None,
) -> str:
    """The `room` a job stores for its target. The server sets it for a `room:` or `web:`
    leaf, so the answer lands as a turn (ISSUE-511). A Talk target takes the token sent.
    A pairing the file already has on an unchanged target, such as `sms` with its private
    room, is left as it stands, and anything else names no room."""
    from .transport import parse_output_target
    from .transport.route_checks import validate_talk_route_token

    sent = _form_text(payload, "room", job.room)
    leaves = parse_output_target(job.target)
    for leaf in leaves:
        if leaf.surface in ("room", "web") and leaf.channel:
            return leaf.channel
    if any(leaf.surface == "talk" for leaf in leaves):
        if stored is None or sent != stored.room:
            try:
                validate_talk_route_token(config, sent, user_id)
            except ValueError:
                raise JobFieldError("room", _ROUTE_REFUSED) from None
        return sent
    if stored is not None and job.target == stored.target and sent == stored.room:
        return sent
    return ""


def _is_new_value(value: str, stored: CronJob | None, name: str) -> bool:
    """Whether a restricted field needs judging. A value unchanged from the file is
    never judged again, so a job whose owner lost a right can still be edited, and
    clearing one is always allowed."""
    return bool(value) and value != (getattr(stored, name) if stored is not None else "")


def _apply_brain_and_provider(
    config, user_id: str, payload: dict, job: CronJob, stored: CronJob | None,
) -> None:
    from .brain import native_providers, resolve_brain_kind, room_selectable_kinds
    from .brain._resolve import native_reach_ids

    is_admin = config.is_admin(user_id)
    brain = _form_text(payload, "brain", job.brain)
    if _is_new_value(brain, stored, "brain"):
        if not is_admin:
            raise JobFieldError("brain", "Only an admin can pin a job to a brain.")
        if brain not in room_selectable_kinds(config.brain):
            raise JobFieldError("brain", "This deployment does not offer that brain.")
    job.brain = brain

    provider = _form_text(payload, "provider", job.provider)
    if _is_new_value(provider, stored, "provider"):
        if not is_admin:
            raise JobFieldError("provider", "Only an admin can choose a job's provider.")
        kind = resolve_brain_kind("scheduled", config.brain, override=brain or None).kind
        if kind != "native":
            raise JobFieldError("provider", "Only a job on the native brain has a provider.")
        if provider not in native_reach_ids(config) or provider not in native_providers(config.brain):
            raise JobFieldError("provider", "The native brain may not send to that provider.")
    job.provider = provider


def _apply_model_and_effort(
    config, user_id: str, payload: dict, job: CronJob, stored: CronJob | None,
) -> None:
    """After the brain and provider, which the model is judged against. A model unchanged
    from the file stays, even one its owner may no longer pick: the sync drops it, as it
    does today. Whitespace is refused here because `_validate_model` lets it through for
    the file's sake, before it asks who may pick what."""
    is_admin = config.is_admin(user_id)
    model = _form_text(payload, "model", job.model)
    if model != (stored.model if stored is not None else ""):
        if any(ch.isspace() for ch in model):
            raise JobFieldError("model", "A model name has no spaces.")
        if model and not _validate_model(
            _FORM_JOB, user_id, model, config,
            job.brain if is_admin else "", [],
            provider=job.provider if is_admin else "",
        ):
            raise JobFieldError("model", "You cannot pick that model for this job.")
        job.model_refused = False
        job.model_split = None
    job.model = model

    effort = _form_text(payload, "effort", job.effort)
    if _is_new_value(effort, stored, "effort") and effort not in _KNOWN_EFFORT_VALUES:
        raise JobFieldError("effort", "That is not an effort level.")
    job.effort = effort


def _apply_publish(
    config, user_id: str, payload: dict, job: CronJob, stored: CronJob | None,
) -> None:
    from istota.briefings.sources.kv import parse_shared_kv_target
    from istota.sandbox.kv_namespaces import is_reserved_namespace

    target = _form_text(payload, "publish_shared_kv", job.publish_shared_kv)
    if _is_new_value(target, stored, "publish_shared_kv"):
        if not config.is_shared_kv_writer(user_id):
            raise JobFieldError("publish_shared_kv", "You cannot publish to shared content.")
        namespace, key = parse_shared_kv_target(target)
        if not namespace or not key:
            raise JobFieldError("publish_shared_kv", "A publish target is <namespace>/<key>, or a key.")
        if is_reserved_namespace(namespace):
            raise JobFieldError("publish_shared_kv", "That namespace is reserved.")
    job.publish_shared_kv = target

    trusted = _form_bool(payload, "publish_shared_kv_trusted", job.publish_shared_kv_trusted)
    if trusted and not (stored is not None and stored.publish_shared_kv_trusted):
        if not config.is_shared_kv_writer(user_id):
            raise JobFieldError("publish_shared_kv_trusted", "You cannot publish to shared content.")
        if not target:
            raise JobFieldError("publish_shared_kv_trusted", "Trust needs a publish target.")
    job.publish_shared_kv_trusted = trusted and bool(target)


def _apply_egress(
    config, user_id: str, payload: dict, job: CronJob, stored: CronJob | None,
) -> None:
    """The requests a job makes. `_validate_egress`'s own skill sentence names the entry,
    so that refusal is worded here. `egress_kept` is not the form's to change: an entry
    in it stays and is not judged, sent or not."""
    if "egress" not in payload:
        return
    raw = payload["egress"]
    if not isinstance(raw, list):
        raise JobFieldError("egress", "This must be a list of hosts and skill:<name> entries.")
    held = set(stored.egress_kept) if stored is not None else set()
    judged = [entry for entry in raw if not (isinstance(entry, str) and entry.strip() in held)]
    errors: list[str] = []
    declared, refused_skills = _validate_egress(_FORM_JOB, config, user_id, judged, errors)
    if refused_skills:
        raise JobFieldError("egress", "That is not a skill that needs approval and that you can use.")
    if errors:
        raise JobFieldError("egress", errors[0].removeprefix(f"Job '{_FORM_JOB}': "))
    job.egress = declared


def validate_job_input(config, user_id: str, payload, stored: CronJob | None) -> CronJob:
    """The candidate job from the page's fields, or `JobFieldError`.

    Starts from ``stored`` where there is one, so what the form does not send is kept.
    Run before the file's lock is taken: `save_job_definition` compares ``stored``'s
    revision under the lock, so a job edited since is refused there.
    """
    if not isinstance(payload, dict):
        raise JobFieldError("body", "This is not a job.")
    is_admin = config.is_admin(user_id)
    job = dataclasses.replace(stored) if stored is not None else CronJob(name="", cron="")

    name = _form_text(payload, "name", job.name)
    if stored is None or name != stored.name:
        _check_job_name(name)
    job.name = name

    job.cron = _form_text(payload, "cron", job.cron)
    if not job.cron or not croniter.is_valid(job.cron):
        raise JobFieldError("cron", "That is not a cron schedule.")

    _apply_job_text(job, payload, stored, is_admin)

    job.target = _form_text(payload, "target", job.target)
    if job.target and (stored is None or job.target != stored.target):
        _check_job_target(config, user_id, job.target)
    job.room = _job_room(config, user_id, payload, job, stored)

    job.enabled = _form_bool(payload, "enabled", job.enabled)
    job.once = _form_bool(payload, "once", job.once)
    job.silent_unless_action = _form_bool(payload, "silent_unless_action", job.silent_unless_action)
    job.skip_log_channel = _form_bool(payload, "skip_log_channel", job.skip_log_channel)

    # On edit the Access control sets it, through `update_job_reach_in_cron_md`.
    if stored is None:
        reach = _form_text(payload, "reach", "")
        if reach and reach not in _FORM_REACHES:
            raise JobFieldError("reach", "Access is limited, public or full.")
        job.reach = reach

    _apply_brain_and_provider(config, user_id, payload, job, stored)
    _apply_model_and_effort(config, user_id, payload, job, stored)
    _apply_publish(config, user_id, payload, job, stored)
    _apply_egress(config, user_id, payload, job, stored)
    return job


def _candidate_errors(config, user_id: str, job: CronJob, stored: CronJob | None) -> list[str]:
    """What the sync's own parser says of this job alone. Two things the file may hold
    and the parser reports are left out when unchanged from ``stored``: a model its owner
    may no longer pick and a kept `egress` entry, both of which a save keeps as they are."""
    probe = dataclasses.replace(job)
    if stored is not None:
        if job.model == stored.model:
            probe.model = ""
        probe.egress_kept = tuple(e for e in job.egress_kept if e not in stored.egress_kept)
    content = f"```toml\n{render_jobs_block([probe])}```\n"
    return cron_document_errors(content, config, user_id)


def save_job_definition(
    config, user_id: str, job: CronJob, *,
    original_name: str | None, expected_revision: str | None,
) -> JobSaveOutcome:
    """Add a job (``original_name`` None) or replace one, under the file's lock.

    Must not be called inside a write transaction: the write lands on the mount
    (ISSUE-387). The row moves on the daemon's next sync, not here, since a scheduler
    tick holding the older text could otherwise run after this and write it back.
    """
    if not config.has_workspace:
        return JobSaveOutcome("unreadable")
    outcome: JobSaveOutcome | None = None

    def locked() -> bool:
        nonlocal outcome
        # A copy: the writer stamps `prompt_file` on what it externalizes, even when
        # the write then fails.
        outcome = _save_job_locked(
            config, user_id, dataclasses.replace(job), original_name, expected_revision,
        )
        return outcome.status == "ok"

    _with_cron_md_lock(config, user_id, locked)
    return outcome if outcome is not None else JobSaveOutcome("locked")


def job_file_writable(config, user_id: str, doc: "CronDocument | None") -> bool:
    """Whether a whole job may be written over ``doc``, this user's CRON.md as just read.
    No document is fine only where there is no file."""
    from .storage import resolve_user_config_dir  # noqa: PLC0415 - import cycle

    if doc is None:
        config_dir = resolve_user_config_dir(config, user_id)
        return config_dir is not None and not os.path.lexists(config_dir / "CRON.md")
    return not (document_hides_jobs(doc) or doc.block is None)


def _save_job_locked(
    config, user_id: str, job: CronJob,
    original_name: str | None, expected_revision: str | None,
) -> JobSaveOutcome:
    doc = load_cron_document(config, user_id)
    if not job_file_writable(config, user_id, doc):
        return JobSaveOutcome("unreadable")
    jobs = list(doc.jobs) if doc is not None else []

    position = None
    stored = None
    if original_name is not None:
        for index, candidate in enumerate(jobs):
            if candidate.name == original_name:
                position, stored = index, candidate
                break
        if stored is None:
            return JobSaveOutcome("not_found")
        if job_revision(stored) != expected_revision:
            return JobSaveOutcome("changed")
    if job.name != original_name and any(other.name == job.name for other in jobs):
        return JobSaveOutcome("name_taken")

    errors = _candidate_errors(config, user_id, job, stored)
    if errors:
        return JobSaveOutcome("invalid", tuple(errors))

    if position is None:
        jobs.append(job)
    else:
        jobs[position] = job
    if len(_cron_md_content(jobs, doc).encode("utf-8")) > USER_CONFIG_READ_CAP_BYTES:
        return JobSaveOutcome("too_large")
    if not _write_cron_md(config, user_id, jobs, doc):
        return JobSaveOutcome("failed")
    return JobSaveOutcome("ok")
