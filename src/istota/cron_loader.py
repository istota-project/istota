"""Load scheduled job definitions from CRON.md files and sync to DB."""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import tomli

from . import db
from .storage import get_user_cron_path

logger = logging.getLogger("istota.cron_loader")

_TOML_BLOCK_RE = re.compile(r"```toml\s*\n(.*?)```", re.DOTALL)


@dataclass
class CronJob:
    name: str
    cron: str
    prompt: str = ""
    command: str = ""
    target: str = ""  # "talk", "email", or ""
    room: str = ""  # conversation_token
    enabled: bool = True
    silent_unless_action: bool = False
    once: bool = False


def load_cron_jobs(config, user_id: str) -> list[CronJob] | None:
    """
    Load scheduled job definitions from a user's CRON.md file.

    Returns list of CronJob, or None if file doesn't exist or mount not configured.
    """
    if not config.use_mount:
        return None

    cron_path = config.nextcloud_mount_path / get_user_cron_path(user_id, config.bot_dir_name).lstrip("/")
    if not cron_path.exists():
        return None

    try:
        content = cron_path.read_text()
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
        if not name or not cron:
            logger.warning(
                "Skipping incomplete job in CRON.md for %s: name=%r cron=%r",
                user_id, name, cron,
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
        jobs.append(CronJob(
            name=name,
            cron=cron,
            prompt=prompt,
            command=command,
            target=j.get("target", ""),
            room=j.get("room", ""),
            enabled=j.get("enabled", True),
            silent_unless_action=j.get("silent_unless_action", False),
            once=j.get("once", False),
        ))

    return jobs


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
            lines.append(f'command = "{job.command}"')
        elif "\n" in job.prompt:
            lines.append(f'prompt = """{job.prompt}"""')
        else:
            lines.append(f'prompt = "{job.prompt}"')
        if job.target:
            lines.append(f'target = "{job.target}"')
        if job.room:
            lines.append(f'room = "{job.room}"')
        if not job.enabled:
            lines.append("enabled = false")
        if job.silent_unless_action:
            lines.append("silent_unless_action = true")
        if job.once:
            lines.append("once = true")

    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def sync_cron_jobs_to_db(conn, user_id: str, file_jobs: list[CronJob]) -> None:
    """
    Sync CRON.md job definitions into the scheduled_jobs DB table.

    - New jobs are inserted
    - Existing jobs have definition fields updated (preserving state fields)
    - Orphaned DB jobs (not in file) are deleted
    - enabled logic: file false → DB 0; file true → no override (preserves !cron disable)
    """
    db_jobs = db.get_user_scheduled_jobs(conn, user_id)
    db_by_name = {j.name: j for j in db_jobs}
    file_names = {j.name for j in file_jobs}

    for fj in file_jobs:
        existing = db_by_name.get(fj.name)
        if existing:
            # Update definition fields, preserve state
            updates = {
                "cron_expression": fj.cron,
                "prompt": fj.prompt,
                "command": fj.command or None,
                "conversation_token": fj.room or None,
                "output_target": fj.target or None,
                "silent_unless_action": 1 if fj.silent_unless_action else 0,
                "once": 1 if fj.once else 0,
            }
            # Only force-disable from file; don't re-enable (preserves !cron disable)
            if not fj.enabled:
                updates["enabled"] = 0

            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [existing.id]
            conn.execute(
                f"UPDATE scheduled_jobs SET {set_clause} WHERE id = ?",
                values,
            )
        else:
            # Insert new job
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (user_id, name, cron_expression, prompt, command,
                    conversation_token, output_target, enabled, silent_unless_action,
                    once)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id, fj.name, fj.cron, fj.prompt,
                    fj.command or None,
                    fj.room or None, fj.target or None,
                    1 if fj.enabled else 0,
                    1 if fj.silent_unless_action else 0,
                    1 if fj.once else 0,
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

    Returns True if a file was written.
    """
    if not config.use_mount:
        return False

    cron_path = config.nextcloud_mount_path / get_user_cron_path(user_id, config.bot_dir_name).lstrip("/")
    if cron_path.exists() and not overwrite:
        return False

    db_jobs = db.get_user_scheduled_jobs(conn, user_id)
    if not db_jobs:
        return False

    file_jobs = [
        CronJob(
            name=j.name,
            cron=j.cron_expression,
            prompt=j.prompt,
            command=j.command or "",
            target=j.output_target or "",
            room=j.conversation_token or "",
            enabled=j.enabled,
            silent_unless_action=j.silent_unless_action,
            once=j.once,
        )
        for j in db_jobs
    ]

    cron_path.parent.mkdir(parents=True, exist_ok=True)
    cron_path.write_text(generate_cron_md(file_jobs))
    logger.info(
        "Migrated %d DB scheduled job(s) to CRON.md for user %s",
        len(file_jobs), user_id,
    )
    return True


def remove_job_from_cron_md(config, user_id: str, job_name: str) -> bool:
    """
    Remove a job by name from the user's CRON.md file.

    Loads the file, filters out the named job, and rewrites cleanly.
    Returns True if the job was found and removed.
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

    cron_path = config.nextcloud_mount_path / get_user_cron_path(user_id, config.bot_dir_name).lstrip("/")
    cron_path.write_text(generate_cron_md(jobs))
    logger.info("Removed job '%s' from CRON.md for user %s", job_name, user_id)
    return True
