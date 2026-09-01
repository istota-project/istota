"""Load scheduled job definitions from CRON.md files and sync to DB."""

import hashlib
import json
import logging
import re
import shlex
from dataclasses import dataclass

import tomli

from . import db
from .storage import get_user_scripts_path

logger = logging.getLogger("istota.cron_loader")

_TOML_BLOCK_RE = re.compile(r"```toml\s*\n(.*?)```", re.DOTALL)

# Names with this prefix are managed by module integrations (e.g. money) and
# are not subject to CRON.md orphan deletion.
_MODULE_JOB_PREFIX = "_module."

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


def _validate_model(name: str, user_id: str, model: str) -> None:
    """Warn on suspicious model names; never reject.

    Catches obvious typos (no canonical prefix, embedded whitespace) without
    hardcoding a model allowlist that goes stale every release. Provider
    shortcuts (``opus``), role tiers (``smart``), an optional ``:effort``
    modifier (``opus:high``), and operator-defined custom aliases are all
    accepted: anything the active brain or the operator alias-override table
    knows about passes silently.
    """
    from .brain import BrainConfig, get_alias_overrides, make_brain, split_effort

    if any(c.isspace() for c in model):
        logger.warning(
            "Job '%s' (user %s): model %r contains whitespace — likely a typo",
            name, user_id, model,
        )
        return

    if model.startswith("claude-"):
        return

    # Known to the active brain? Defaults to claude_code at module import time;
    # the actual brain config isn't available to cron_loader, but every brain
    # exposes the same alias surface so this is good enough for warn-only.
    brain = make_brain(BrainConfig())
    if brain.resolve_alias(model) is not None:
        return
    # Custom operator alias (strip any :effort modifier) known to the override
    # table but not to the default brain's own table.
    if split_effort(model)[0].lower() in get_alias_overrides():
        return

    logger.warning(
        "Job '%s' (user %s): model %r is neither a canonical id, alias, nor role — likely a typo",
        name, user_id, model,
    )


def _validate_effort(name: str, user_id: str, effort: str) -> None:
    """Warn when effort isn't in the known set; never reject."""
    if effort not in _KNOWN_EFFORT_VALUES:
        logger.warning(
            "Job '%s' (user %s): effort %r not in known set %s",
            name, user_id, effort, sorted(_KNOWN_EFFORT_VALUES),
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
_KNOWN_TARGET_SURFACES = frozenset({
    "talk", "email", "ntfy", "istota_file", "stream", "web",
})


def _validate_target(name: str, user_id: str, target: str) -> None:
    """Warn when a target descriptor names an unknown surface; never reject.

    The `target` is an output_target descriptor (talk/email/ntfy/both/all/
    talk:<token>/comma lists). Unknown leaves are dropped at delivery, so a typo
    is a warning, not a hard failure (mirrors model/effort handling)."""
    from .transport import parse_output_target
    for dest in parse_output_target(target):
        if dest.surface not in _KNOWN_TARGET_SURFACES:
            logger.warning(
                "Job '%s' (user %s): target surface %r not recognized; it will "
                "be dropped at delivery",
                name, user_id, dest.surface,
            )


def _coerce_bool(name: str, user_id: str, field: str, value, default: bool) -> bool:
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
    # admin-shared-briefing-blocks: publish this job's result text into shared_kv
    # on success. "<ns>/<key>" or bare "<key>" (→ briefing_shared_blocks). Empty =
    # no publish. Gated on is_shared_kv_writer at write time (admin-only).
    publish_shared_kv: str = ""
    publish_shared_kv_trusted: bool = False


def load_cron_jobs(config, user_id: str) -> list[CronJob] | None:
    """
    Load scheduled job definitions from a user's CRON.md file.

    Returns list of CronJob, or None if file doesn't exist or mount not configured.
    """
    if not config.use_mount:
        return None

    # Hardened like every other host-side read of `{bot_dir}/config/`
    # (ISSUE-339). This one runs on the scheduler's own cron-sync tick, so a
    # FIFO planted at CRON.md blocked that loop rather than one task, and a
    # symlink fed an arbitrary daemon-readable file to the TOML parser.
    from .storage import read_user_config_file  # noqa: PLC0415 - import cycle

    content = read_user_config_file(config, user_id, "CRON.md")
    if content is None or not content:
        return None

    try:
        match = _TOML_BLOCK_RE.search(content)
        if not match:
            return []
        toml_str = match.group(1)
        data = tomli.loads(toml_str)
    except Exception as e:
        logger.warning("Failed to parse CRON.md for %s: %s", user_id, e)
        return None

    jobs = []
    for j in data.get("jobs", []):
        name = j.get("name", "").strip()
        cron = j.get("cron", "").strip()
        prompt = j.get("prompt", "").strip()
        command = j.get("command", "").strip()
        prompt_file = j.get("prompt_file", "").strip()
        if not name or not cron:
            logger.warning(
                "Skipping incomplete job in CRON.md for %s: name=%r cron=%r",
                user_id, name, cron,
            )
            continue
        # Resolve prompt_file to prompt contents
        if prompt_file:
            if prompt or command:
                logger.warning(
                    "Skipping job '%s' in CRON.md for %s: prompt_file cannot be combined with prompt or command",
                    name, user_id,
                )
                continue
            file_path = config.nextcloud_mount_path / prompt_file.lstrip("/")
            try:
                prompt = file_path.read_text().strip()
            except OSError as e:
                logger.warning(
                    "Skipping job '%s' in CRON.md for %s: cannot read prompt_file %s: %s",
                    name, user_id, prompt_file, e,
                )
                continue
        if prompt and command:
            logger.warning(
                "Skipping job '%s' in CRON.md for %s: cannot have both prompt and command",
                name, user_id,
            )
            continue
        if not prompt and not command:
            logger.warning(
                "Skipping job '%s' in CRON.md for %s: must have either prompt or command",
                name, user_id,
            )
            continue
        model = j.get("model", "").strip()
        effort = j.get("effort", "").strip()
        if model:
            _validate_model(name, user_id, model)
        if effort:
            _validate_effort(name, user_id, effort)
        target = j.get("target", "")
        if target:
            _validate_target(name, user_id, target)
        jobs.append(CronJob(
            name=name,
            cron=cron,
            prompt=prompt,
            command=command,
            prompt_file=prompt_file,
            target=target,
            room=j.get("room", ""),
            enabled=_coerce_bool(
                name, user_id, "enabled", j.get("enabled", True), True),
            silent_unless_action=_coerce_bool(
                name, user_id, "silent_unless_action",
                j.get("silent_unless_action", False), False),
            skip_log_channel=_coerce_bool(
                name, user_id, "skip_log_channel",
                j.get("skip_log_channel", False), False),
            once=_coerce_bool(
                name, user_id, "once", j.get("once", False), False),
            model=model,
            effort=effort,
            publish_shared_kv=str(j.get("publish_shared_kv", "")).strip(),
            publish_shared_kv_trusted=_coerce_bool(
                name, user_id, "publish_shared_kv_trusted",
                j.get("publish_shared_kv_trusted", False), False),
        ))

    return jobs


def _toml_string(key: str, value: str) -> str:
    """Format a TOML key-value pair, using triple quotes when needed."""
    if "\n" in value or '"' in value:
        return f'{key} = """{value}"""'
    return f'{key} = "{value}"'


def generate_cron_md(jobs: list[CronJob]) -> str:
    """Generate CRON.md content from a list of CronJob definitions."""
    lines = ["# Scheduled Jobs", "", "```toml"]

    for i, job in enumerate(jobs):
        if i > 0:
            lines.append("")
        lines.append("[[jobs]]")
        lines.append(f'name = "{job.name}"')
        lines.append(f'cron = "{job.cron}"')
        if job.command:
            lines.append(_toml_string("command", job.command))
        elif job.prompt_file:
            lines.append(_toml_string("prompt_file", job.prompt_file))
        else:
            lines.append(_toml_string("prompt", job.prompt))
        if job.target:
            lines.append(f'target = "{job.target}"')
        if job.room:
            lines.append(f'room = "{job.room}"')
        if not job.enabled:
            lines.append("enabled = false")
        if job.silent_unless_action:
            lines.append("silent_unless_action = true")
        if job.skip_log_channel:
            lines.append("skip_log_channel = true")
        if job.once:
            lines.append("once = true")
        if job.model:
            lines.append(f'model = "{job.model}"')
        if job.effort:
            lines.append(f'effort = "{job.effort}"')
        if job.publish_shared_kv:
            lines.append(_toml_string("publish_shared_kv", job.publish_shared_kv))
        if job.publish_shared_kv_trusted:
            lines.append("publish_shared_kv_trusted = true")

    lines.append("```")
    lines.append("")
    return "\n".join(lines)


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


def _write_generated_prompt(path, prompt: str) -> None:
    """Create a generated prompt file without replacing unrelated content."""
    try:
        with path.open("x") as f:
            f.write(prompt)
    except FileExistsError:
        if path.read_text() != prompt:
            raise


def _externalize_multiline_prompts(config, user_id: str, jobs: list[CronJob]) -> None:
    """Move inline multiline prompts into files before CRON.md is rewritten."""
    prompts_dir_ref = f"{get_user_scripts_path(user_id, config.bot_dir_name)}/prompts"
    prompts_dir = config.nextcloud_mount_path / prompts_dir_ref.lstrip("/")
    assigned_names: dict[str, str] = {}

    for job in jobs:
        if (
            job.command
            or job.prompt_file
            or ("\r" not in job.prompt and "\n" not in job.prompt)
        ):
            continue

        prompts_dir.mkdir(parents=True, exist_ok=True)
        stem = _prompt_file_name(job.name)
        if stem in assigned_names and assigned_names[stem] != job.name:
            stem = _disambiguate_prompt_file_name(stem, job.name)
        assigned_names[stem] = job.name
        prompt_path = prompts_dir / f"{stem}.txt"
        try:
            _write_generated_prompt(prompt_path, job.prompt)
        except FileExistsError:
            digest = hashlib.sha256(job.prompt.encode()).hexdigest()[:8]
            prompt_path = prompts_dir / f"{stem}-{digest}.txt"
            _write_generated_prompt(prompt_path, job.prompt)
        job.prompt_file = f"{prompts_dir_ref}/{prompt_path.name}"


def _write_cron_md(config, user_id: str, jobs: list[CronJob]) -> bool:
    """Write CRON.md, externalizing inline multiline prompts first.

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
    scheduler's once-job caller is why that matters: it runs inside an open
    write transaction that has already deleted the job row, so an exception
    there rolls the task's own completion back and the one-shot runs a second
    time — strictly worse than the file being stale.

    One thing survives a ``False``: ``_externalize_multiline_prompts`` writes
    ``scripts/prompts/*.txt`` and stamps ``job.prompt_file`` before anything is
    written here, so a refused write leaves a prompt file no CRON.md refers
    to. Harmless and idempotent (``_write_generated_prompt`` compares content
    on a collision), but it is not evidence the write landed.
    """
    from .storage import resolve_user_config_dir, write_regular_file  # noqa: PLC0415

    try:
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
    return write_regular_file(config_dir / "CRON.md", generate_cron_md(jobs))


def sync_cron_jobs_to_db(
    conn,
    user_id: str,
    file_jobs: list[CronJob],
    *,
    is_admin: bool = True,
) -> None:
    """
    Sync CRON.md job definitions into the scheduled_jobs DB table.

    - New jobs are inserted
    - Existing jobs have definition fields updated (preserving state fields)
    - Orphaned DB jobs (not in file) are deleted
    - enabled logic: file is authoritative (symmetric: file false → DB 0, file true → DB 1)
    - command-type jobs are rejected for non-admin users (arbitrary user
      shell-command tasks must remain admin-only)
    """
    db_jobs = db.get_user_scheduled_jobs(conn, user_id)
    # Module-managed jobs are owned by their integration (see jobs.py in the
    # respective module package); CRON.md must not touch them.
    db_jobs = [j for j in db_jobs if not j.name.startswith(_MODULE_JOB_PREFIX)]
    db_by_name = {j.name: j for j in db_jobs}
    file_names = {j.name for j in file_jobs if not (fj_is_disallowed_command(j, is_admin))}

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
        existing = db_by_name.get(fj.name)
        if existing:
            # Update definition fields, preserve state
            updates = {
                "cron_expression": fj.cron,
                "prompt": fj.prompt,
                "command": cmd_val,
                "skill": skill_val,
                "skill_args": skill_args_val,
                "conversation_token": fj.room or None,
                "output_target": fj.target or None,
                "silent_unless_action": 1 if fj.silent_unless_action else 0,
                "skip_log_channel": 1 if fj.skip_log_channel else 0,
                "once": 1 if fj.once else 0,
                "model": fj.model or None,
                "effort": fj.effort or None,
                "publish_shared_kv": fj.publish_shared_kv or None,
                "publish_shared_kv_trusted": 1 if fj.publish_shared_kv_trusted else 0,
            }
            # File is authoritative for enabled state (symmetric sync)
            updates["enabled"] = 1 if fj.enabled else 0

            # Reset last_run_at when cron expression changes to prevent
            # catch-up runs for past slots in the new expression
            cron_changed = fj.cron != existing.cron_expression
            if cron_changed:
                logger.info(
                    "Cron expression changed for job '%s' (user %s): "
                    "'%s' -> '%s', resetting last_run_at",
                    fj.name, user_id, existing.cron_expression, fj.cron,
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
            # Insert new job
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, command,
                    skill, skill_args,
                    conversation_token, output_target, enabled, silent_unless_action,
                    skip_log_channel, once, model, effort,
                    publish_shared_kv, publish_shared_kv_trusted)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id, fj.name, fj.cron, fj.prompt,
                    cmd_val,
                    skill_val, skill_args_val,
                    fj.room or None, fj.target or None,
                    1 if fj.enabled else 0,
                    1 if fj.silent_unless_action else 0,
                    1 if fj.skip_log_channel else 0,
                    1 if fj.once else 0,
                    fj.model or None,
                    fj.effort or None,
                    fj.publish_shared_kv or None,
                    1 if fj.publish_shared_kv_trusted else 0,
                ),
            )

    # Delete orphaned DB jobs (not in file)
    for db_job in db_jobs:
        if db_job.name not in file_names:
            conn.execute("DELETE FROM scheduled_jobs WHERE id = ?", (db_job.id,))
            logger.info(
                "Removed orphaned scheduled job '%s' for user %s",
                db_job.name, user_id,
            )

    conn.commit()


def migrate_db_jobs_to_file(conn, config, user_id: str, overwrite: bool = False) -> bool:
    """
    Generate CRON.md from existing DB jobs (one-time migration).

    Args:
        overwrite: If True, overwrite an existing file (used when file exists
                   but is empty/template-only while DB has real jobs).

    Returns True if a file was written — which now includes the write
    itself having succeeded, not just having been attempted.
    """
    if not config.use_mount:
        return False

    from .storage import resolve_user_config_dir  # noqa: PLC0415 - import cycle

    config_dir = resolve_user_config_dir(config, user_id)
    if config_dir is None:
        return False
    if (config_dir / "CRON.md").exists() and not overwrite:
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
            publish_shared_kv=j.publish_shared_kv or "",
            publish_shared_kv_trusted=bool(j.publish_shared_kv_trusted),
        ))

    if not _write_cron_md(config, user_id, file_jobs):
        return False
    logger.info(
        "Migrated %d DB scheduled job(s) to CRON.md for user %s",
        len(file_jobs), user_id,
    )
    return True


def update_job_enabled_in_cron_md(config, user_id: str, job_name: str, enabled: bool) -> bool:
    """
    Update a job's enabled state in the user's CRON.md file.

    Loads all jobs, updates the target job's enabled field, and rewrites the file.
    Returns True if the job was found **and the file now says so**: a refused
    or failed write is False, so a caller that reports success is reporting
    the file's state rather than its own intention (ISSUE-369).
    """
    if not config.use_mount:
        return False

    jobs = load_cron_jobs(config, user_id)
    if jobs is None:
        return False

    found = False
    for job in jobs:
        if job.name == job_name:
            job.enabled = enabled
            found = True
            break

    if not found:
        return False

    if not _write_cron_md(config, user_id, jobs):
        return False
    logger.info(
        "%s job '%s' in CRON.md for user %s",
        "Enabled" if enabled else "Disabled", job_name, user_id,
    )
    return True


def remove_job_from_cron_md(config, user_id: str, job_name: str) -> bool:
    """
    Remove a job by name from the user's CRON.md file.

    Loads the file, filters out the named job, and rewrites cleanly.
    Returns True if the job was found **and the file now says so**; a refused
    or failed write is False, like ``update_job_enabled_in_cron_md``.
    """
    if not config.use_mount:
        return False

    jobs = load_cron_jobs(config, user_id)
    if jobs is None:
        return False

    original_count = len(jobs)
    jobs = [j for j in jobs if j.name != job_name]
    if len(jobs) == original_count:
        return False  # Job not found

    if not _write_cron_md(config, user_id, jobs):
        return False
    logger.info("Removed job '%s' from CRON.md for user %s", job_name, user_id)
    return True
