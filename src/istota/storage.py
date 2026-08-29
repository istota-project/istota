"""Bot-managed Nextcloud storage operations."""

import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger("istota.storage")

BOT_USER_BASE = "/Users"
CHANNEL_BASE = "/Channels"

WORKSPACE_README = """\
# Istota

This is a shared collaboration folder — both you and Istota have \
read/write access. Everything you interact with lives here.

## Files

Configuration files live in the `config/` subfolder:

- **config/USER.md** — Persistent memory
- **config/TASKS.md** — Task queue (`- [ ] do something`)
- **config/BRIEFINGS.md** — Briefing schedule configuration
- **config/HEARTBEAT.md** — Health monitoring configuration
- **config/CRON.md** — Scheduled recurring jobs
- **config/PERSONA.md** — Bot personality (editable copy of global persona)

See `examples/` for detailed documentation and configuration reference.
"""

WORKSPACE_README_EXAMPLE = """\
# Istota

This is a shared collaboration folder — both you and Istota have \
read/write access. Everything you interact with lives here.

## Files

Configuration files live in the `config/` subfolder:

- **config/USER.md** — Persistent memory. Istota reads this at the start of every \
task and appends to it when you ask it to remember something. A development \
workflow for coding tasks is written here too — see `examples/WORKFLOW.md`.
- **config/TASKS.md** — Task queue. Write `- [ ] do something` and Istota picks \
it up automatically. Status updates are written back to the file.
- **config/BRIEFINGS.md** — (Optional) Briefing schedule configuration. \
Control your own briefing times, delivery channel, and components.
- **config/HEARTBEAT.md** — (Optional) Health monitoring configuration. \
Set up periodic checks that alert you when something needs attention.
- **config/CRON.md** — (Optional) Scheduled recurring jobs. \
Configure tasks that run on a cron schedule with results delivered to Talk or email.
- **config/PERSONA.md** — (Optional) Bot personality. \
Edit this to customize how Istota behaves and communicates with you.

## Other content

Istota saves drafts, summaries, research, and anything else you ask it to \
produce in this folder. You can also drop files here for Istota to read \
in future conversations.

Additionally, you can share any of your own Nextcloud folders with Istota \
for direct access to your files.
"""

BRIEFINGS_TEMPLATE = """\
# Briefing Schedule

See `examples/BRIEFINGS.md` for all available options.

```toml
# [[briefings]]
# name = "morning"
# cron = "0 7 * * 1-5"         # 7am weekdays (in your timezone)
# conversation_token = "{conversation_token}"
# output = "talk"               # talk / email / ntfy (or a comma list, e.g. "talk,email")
#
# [briefings.components]
# markets = true
# news = true
# calendar = true
# todos = true
# reminders = true
# notes = true
```
"""

BRIEFINGS_EXAMPLE = """\
# Briefing Schedule

Control your briefing times, delivery channel, and components.
The scheduler reads this file automatically — changes take effect within ~60 seconds.

## Example

```toml
[[briefings]]
name = "morning"
cron = "0 7 * * 1-5"         # 7am weekdays (in your timezone)
conversation_token = "abc123"
output = "talk"               # talk / email / ntfy (or a comma list, e.g. "talk,email")

[briefings.components]
markets = true
news = true
calendar = true
todos = true
reminders = true
notes = true

[[briefings]]
name = "evening"
cron = "0 18 * * 1-5"        # 6pm weekdays
conversation_token = "abc123"
output = "talk"

[briefings.components]
markets = true
news = true
calendar = true
```

## Component Reference

- **calendar** — Today's calendar events
- **todos** — Pending items from your configured TODO file resource
- **markets** — Market data from configured symbols
- **news** — Headlines from configured news sources
- **reminders** — Random reminder from your configured reminders file resource
- **notes** — Summary of recent notes

Components set to `true` expand using admin-configured defaults.
Use a dict to override, e.g.: `markets = { enabled = true, futures = ["ES=F"] }`

## Output Options

- `output = "talk"` — Send to Nextcloud Talk room (requires `conversation_token`)
- `output = "email"` — Send via email
- `output = "ntfy"` — Send as an ntfy push notification
- `output = "talk,email"` — Comma-separate surfaces to deliver to several at once

## Cron Format

Standard 5-field cron: `minute hour day-of-month month day-of-week`

- `0 7 * * 1-5` — 7am weekdays
- `0 18 * * *` — 6pm every day
- `30 8 * * 1` — 8:30am Mondays only
- `0 */6 * * *` — Every 6 hours

Evaluated in the user's configured timezone.
"""


def _build_briefings_seed(config: "Config", user_id: str) -> str:
    """Build seed BRIEFINGS.md content, filling conversation_token from admin config."""
    token = ""
    user_config = config.users.get(user_id)
    if user_config:
        for b in user_config.briefings:
            if b.conversation_token:
                token = b.conversation_token
                break
    return BRIEFINGS_TEMPLATE.format(conversation_token=token)


# Template for initial HEARTBEAT.md file
HEARTBEAT_TEMPLATE = """\
# Heartbeat Monitoring

See `examples/HEARTBEAT.md` for all check types and options.

```toml
# [settings]
# conversation_token = "{conversation_token}"  # Talk room for alerts
# quiet_hours = ["22:00-07:00"]                # Suppress alerts during these hours
# default_cooldown_minutes = 60                # Time between repeat alerts

# [[checks]]
# name = "backup-fresh"
# type = "file-watch"
# path = "/Users/{user_id}/backups/latest.log"
# max_age_hours = 25
```
"""

HEARTBEAT_EXAMPLE = """\
# Heartbeat Monitoring

Configure periodic health checks that alert you when something needs attention.
HEARTBEAT.md is for monitoring — checking conditions and alerting on failures.
For running tasks on a schedule (including AI-powered checks), use CRON.md instead.

The scheduler evaluates these checks automatically — changes take effect within ~60 seconds.

## Example

```toml
[settings]
conversation_token = "abc123"          # Talk room for alerts
quiet_hours = ["22:00-07:00"]          # Suppress alerts during these hours
default_cooldown_minutes = 60          # Time between repeat alerts

[[checks]]
name = "backup-fresh"
type = "file-watch"
path = "/Users/alice/backups/latest.log"
max_age_hours = 25
cooldown_minutes = 120                 # Override default cooldown
interval_minutes = 15                  # Run every 15 min (default: every cycle)

[[checks]]
name = "disk-space"
type = "shell-command"
command = "df -h / | tail -1 | awk '{print $5}' | tr -d '%'"
condition = "< 90"
message = "Disk usage at {value}%"

[[checks]]
name = "api-health"
type = "url-health"
url = "https://api.example.com/health"
expected_status = 200
timeout = 10

[[checks]]
name = "schedule-conflicts"
type = "calendar-conflicts"
lookahead_hours = 24

[[checks]]
name = "overdue-tasks"
type = "task-deadline"
source = "file"
warn_hours_before = 24

[[checks]]
name = "system-health"
type = "self-check"
interval_minutes = 30                  # Run every 30 min (expensive: spawns Claude)
cooldown_minutes = 60

[checks.config]
execution_test = true                  # Test actual Claude CLI invocation
```

## Check Types

- **file-watch** — Check file age or existence (`path`, `max_age_hours`)
- **shell-command** — Run command, evaluate condition (`command`, `condition`, `message`, `timeout`)
- **url-health** — HTTP health check (`url`, `expected_status`, `timeout`)
- **calendar-conflicts** — Find overlapping events (`lookahead_hours`)
- **task-deadline** — Check for overdue tasks (`source`, `warn_hours_before`)
- **self-check** — System health diagnostics: Claude binary, bwrap, DB, failure rate, execution test (`execution_test`)

## Per-Check Fields

- `cooldown_minutes` — Override `default_cooldown_minutes` for this check
- `interval_minutes` — Run this check every N minutes instead of every cycle (~60s). Useful for expensive checks like `self-check`. Omit to run every cycle.

## Conditions (shell-command)

- `< N` / `> N` — Numeric comparison
- `== value` — Exact string match
- `contains:text` — Substring match
- `not-contains:text` — Negative substring match

## Quiet Hours

Time ranges like `22:00-07:00` suppress alert delivery, but checks still run.
Cross-midnight ranges are supported. When quiet hours end, the next failure triggers an alert.

## Cooldown

After an alert, no repeat alerts are sent until the cooldown expires.
Set `cooldown_minutes` per-check to override `default_cooldown_minutes`.
"""


def _build_heartbeat_seed(config: "Config", user_id: str) -> str:
    """Build seed HEARTBEAT.md content, filling conversation_token and user_id."""
    token = ""
    user_config = config.users.get(user_id)
    if user_config:
        for b in user_config.briefings:
            if b.conversation_token:
                token = b.conversation_token
                break
    return HEARTBEAT_TEMPLATE.format(conversation_token=token, user_id=user_id)




# Template for initial TASKS.md file
TASKS_FILE_TEMPLATE = """\
# Tasks
"""

TASKS_FILE_EXAMPLE = """\
# Tasks

Write a task as `- [ ] do something` and Istota picks it up automatically.
Status updates are written back to this file.

## Status Markers

- `[ ]` — Pending (Istota will pick this up)
- `[~]` — In progress (Istota is working on it)
- `[x]` — Completed
- `[!]` — Failed

## Examples

```markdown
- [ ] summarize my inbox
- [ ] check the weather forecast for this weekend
- [ ] draft a reply to the last email from Alice
```

Tasks are identified by content hash, so you can reorder freely.
Completed/failed tasks can be deleted or kept for reference.
"""

# Template for initial memory file.
#
# The HTML comment at the top is read by Claude when USER.md is loaded
# into the prompt. It's a hint, not enforcement — the structural fix
# is the runtime classification gate in the memory skill — but it
# survives parser round-trips (preamble) and can nudge the model when
# it's unsure where a memory belongs.
MEMORY_TEMPLATE = """<!-- agents: This file holds behavioral instructions and stable context only. Temporal events (purchases, decisions, status changes — anything you'd date-stamp) and stable factual claims (allergies, family, biography) belong in the knowledge graph via `istota-skill memory_search add-fact`. Append behavioral instructions only via `istota-skill memory append --heading "<existing heading>"`. Never use `echo >>` on this file. -->
# User Memory

This file contains remembered information about the user.
The bot can append to this file to remember things for future conversations.

## Notes

"""


def get_user_base_path(user_id: str) -> str:
    """Get the base path for a user's bot-managed directory."""
    return f"{BOT_USER_BASE}/{user_id}"


def get_user_memory_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's memory file (USER.md in bot dir config/)."""
    return f"{get_user_config_path(user_id, bot_dir)}/USER.md"


def get_user_memories_path(user_id: str) -> str:
    """Get the path to a user's dated memories directory."""
    return f"{get_user_base_path(user_id)}/memories"


def get_user_bot_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's bot directory (e.g. /Users/{uid}/istota/)."""
    return f"{get_user_base_path(user_id)}/{bot_dir}"


def get_user_config_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's bot config/ directory."""
    return f"{get_user_bot_path(user_id, bot_dir)}/config"



def get_user_tasks_file_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's TASKS.md file."""
    return f"{get_user_config_path(user_id, bot_dir)}/TASKS.md"


def get_user_shared_path(user_id: str) -> str:
    """Get the path to a user's shared folder (for auto-organized shared files)."""
    return f"{get_user_base_path(user_id)}/shared"


def get_user_scripts_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's scripts directory (inside bot dir)."""
    return f"{get_user_base_path(user_id)}/{bot_dir}/scripts"


def get_user_playbooks_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's learned-playbooks directory (inside bot dir)."""
    return f"{get_user_base_path(user_id)}/{bot_dir}/playbooks"


def get_user_briefings_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's BRIEFINGS.md file."""
    return f"{get_user_config_path(user_id, bot_dir)}/BRIEFINGS.md"


def get_user_heartbeat_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's HEARTBEAT.md file."""
    return f"{get_user_config_path(user_id, bot_dir)}/HEARTBEAT.md"




def get_user_cron_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's CRON.md file."""
    return f"{get_user_config_path(user_id, bot_dir)}/CRON.md"



def get_user_persona_path(user_id: str, bot_dir: str) -> str:
    """Get the path to a user's PERSONA.md file."""
    return f"{get_user_config_path(user_id, bot_dir)}/PERSONA.md"


def get_user_skill_overlays_path(user_id: str, bot_dir: str) -> str:
    """Directory of per-skill user overlay files.

    One flat ``<skill-name>.md`` per skill, appended to that skill's bundled
    body whenever the skill is loaded. Distinct from the *operator* override at
    ``config/skills/<name>/skill.md``, which replaces the body outright — an
    overlay is additive, so upstream skill edits keep flowing under it.
    """
    return f"{get_user_config_path(user_id, bot_dir)}/skills"


def resolve_user_skill_overlays_dir(config: "Config", user_id: str) -> Path | None:
    """The on-disk overlay directory for a user, or None where there is none.

    Both load paths — the eager one in ``executor`` and ``skills show`` — call
    this rather than joining the mount themselves. That is the same argument as
    injecting inside ``load_skills``, one level up: two call sites deriving one
    path independently is a wrong ``bot_dir`` or a missing ``lstrip`` away from
    leaving one path silently inert while both test suites stay green.

    None without a mount. Overlays are filesystem reads, so an rclone-remote
    deployment has none — the condition ``load_persona`` already applies to a
    per-user ``PERSONA.md``.

    None as well when the directory leads outside the user's own tree.
    ``config`` and ``skills`` are ordinary entries under a root bound
    read-write into that user's sandbox, so either can be replaced with a
    symlink; the loader's ``O_NOFOLLOW`` covers only the overlay file itself,
    and the files at the far end of a redirected directory are ordinary
    regular files that pass every leaf-level guard. Returning None degrades to
    exactly the prompt the skill would have had with no overlay at all, which
    is what every other overlay failure path already does.

    The **resolved** path is what comes back, so a caller cannot re-walk by the
    unresolved name after the check.
    """
    if not config.use_mount:
        return None
    from .skills._loader import contained_overlay_dir  # noqa: PLC0415 - import cycle

    overlay_dir = _get_mount_path(
        config, get_user_skill_overlays_path(user_id, config.bot_dir_name)
    )
    user_root = _get_mount_path(config, f"Users/{user_id}")
    return contained_overlay_dir(overlay_dir, user_root)


CRON_TEMPLATE = """\
# Scheduled Jobs

See `examples/CRON.md` for all options and cron format reference.

```toml
# [[jobs]]
# name = "daily-report"
# cron = "0 9 * * *"             # 9am daily (in your timezone)
# prompt = "Generate my daily report"
# target = "talk"                 # "talk", "email", or omit
# room = "{conversation_token}"   # Talk room token (required for target = "talk")
```
"""

CRON_EXAMPLE = """\
# Scheduled Jobs

CRON.md is for running tasks and commands on a schedule.
For monitoring conditions and alerting on failures, use HEARTBEAT.md instead.

Configure recurring tasks that run on a schedule.
The scheduler reads this file automatically — changes take effect within ~60 seconds.

## Example

```toml
[[jobs]]
name = "morning-briefing"
cron = "0 9 * * *"               # 9am daily (in your timezone)
prompt = "Generate my morning briefing"
target = "talk"                   # Post result to Talk room
room = "abc123"                   # Conversation token

[[jobs]]
name = "weekly-review"
cron = "0 18 * * 0"              # 6pm Sundays
prompt = "Generate weekly review of completed tasks"
target = "email"                  # Send result via email

[[jobs]]
name = "check-deadlines"
cron = "0 8 * * 1-5"             # 8am weekdays
prompt = "Check for any upcoming deadlines this week"
target = "talk"
room = "abc123"
silent_unless_action = true       # Only post if something needs attention
```

## Fields

- **name** — Unique identifier for the job (e.g., `daily-report`, `weekly-cleanup`)
- **cron** — Standard 5-field cron expression (minute hour day month weekday)
- **prompt** — The full prompt text that will be executed as a task
- **target** — Where to deliver results: `"talk"` or `"email"` (omit for no delivery)
- **room** — Talk conversation token (required when target is `"talk"`)
- **enabled** — Set to `false` to pause the job (default: true)
- **silent_unless_action** — When true, only posts output if response starts with \
`ACTION:` (default: false)

## Runtime Control

Use `!cron` in Talk to manage jobs at runtime:

- `!cron` — List all jobs and their status
- `!cron enable <name>` — Re-enable a disabled job (resets failure count)
- `!cron disable <name>` — Disable a job

Jobs auto-disable after 5 consecutive failures. Use `!cron enable` to re-activate.

## Cron Format

Standard 5-field cron: `minute hour day-of-month month day-of-week`

- `0 9 * * *` — Every day at 9:00 AM
- `0 9 * * 1-5` — Weekdays at 9:00 AM
- `30 18 * * 0` — Sundays at 6:30 PM
- `0 */6 * * *` — Every 6 hours
- `0 8 1 * *` — First of every month at 8:00 AM

Evaluated in the user's configured timezone.
"""


WORKFLOW_EXAMPLE = """\
# Development workflow

There is no `config/WORKFLOW.md`. A development workflow is written in
`config/USER.md`, or in a project room's `CHANNEL.md`, under a heading of your
own. This file is the vocabulary — what you can set, not what you should set.

The developer skill ships a default for each decision below and yields to
whatever you write. Say nothing about a decision and its default applies, so a
workflow of three lines is a perfectly good one.

## Where to write it

- **config/USER.md** — applies to every coding task, whichever room it came from.
- **CHANNEL.md** in a project's room — applies to tasks from that room only.
- Where both carry a workflow and they disagree, `CHANNEL.md` wins for a task \
from that room. It is the more specific statement, and the room is the project.

## What you can set

- **Worktree per task** — whether each task cuts its own worktree, or works \
somewhere you name.
- **Change tiers** — how much process a change gets, and what decides which tier \
it is.
- **When a test gets written** — before the implementation, alongside it, or by \
a rule of your own.
- **When tests run, and which** — the scope of the pass. The default is the \
tests covering the change plus lint and typecheck over the whole repository; \
ask here for a whole suite if you want one.
- **Commit granularity** — coherent steps, one commit per task, or your own rule.
- **Whether a review runs** — at which tiers, and whether at all.
- **An MR or PR rather than a merge** — how work lands. Usually a property of \
the project rather than of you, so a room's `CHANNEL.md` is the better home.
- **Report shape** — the block a finished task reports in.

## What you cannot set

Deployment mechanics do not yield: the forge boundary and its refused verbs, the
network allowlist, the ceiling on how long one command may run, the credential
rules, where builds and tests run, the pre-submission checks, and every delete
path. An instruction that collides with one of those is reported back to you
rather than followed.

## Example

```markdown
## Development workflow

- Worktree per task, always.
- No review below Standard tier.
- Land as a merge request; never merge to the default branch yourself.
```

Every decision that block does not mention keeps its default.
"""


def _build_cron_seed(config: "Config", user_id: str) -> str:
    """Build seed CRON.md content, filling conversation_token from admin config."""
    token = ""
    user_config = config.users.get(user_id)
    if user_config:
        for b in user_config.briefings:
            if b.conversation_token:
                token = b.conversation_token
                break
    return CRON_TEMPLATE.format(conversation_token=token)



def _rclone_run(args: list[str], **kwargs) -> subprocess.CompletedProcess | None:
    """Run an rclone command, or return None if rclone could not be run at all.

    Every caller below documents "returns None / False on failure", and a
    missing `rclone` binary is a failure — but `subprocess.run` raises
    `FileNotFoundError` for it rather than returning a non-zero status, so the
    exception escaped past all five of them. Reachable in ordinary operation:
    the rclone path is the fallback for a deployment with no mount, and such a
    deployment need not have rclone installed either.

    Covers the five helpers in this module. `skills/files/__init__.py` is a
    separate copy of this API with the same problem and its own version of this
    helper; neither module imports the other.

    `setdefault` rather than fixed keywords, so a future caller passing
    `text=False` for a binary `rclone cat` gets its own value rather than
    "multiple values for keyword argument".
    """
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    try:
        return subprocess.run(args, **kwargs)
    except OSError as exc:
        logger.warning("rclone unavailable (%s); treating %s as a failure", exc, args[1:2])
        return None


def _rclone_mkdir(remote: str, path: str) -> bool:
    """Create a directory via rclone. Returns True on success."""
    result = _rclone_run(["rclone", "mkdir", f"{remote}:{path}"])
    return result is not None and result.returncode == 0


def _rclone_path_exists(remote: str, path: str) -> bool:
    """Check if a path exists via rclone lsjson."""
    result = _rclone_run(["rclone", "lsjson", f"{remote}:{path}"])
    return result is not None and result.returncode == 0


def _rclone_cat(remote: str, path: str) -> str | None:
    """Read a file via rclone cat. Returns None on failure."""
    result = _rclone_run(["rclone", "cat", f"{remote}:{path}"])
    if result is None or result.returncode != 0:
        return None
    return result.stdout


def _rclone_rcat(remote: str, path: str, content: str) -> bool:
    """Write content to a file via rclone rcat. Returns True on success."""
    result = _rclone_run(["rclone", "rcat", f"{remote}:{path}"], input=content)
    return result is not None and result.returncode == 0


def ensure_user_directories(remote: str, user_id: str, bot_dir: str) -> bool:
    """
    Create the bot-managed directory structure for a user.

    Returns True if all directories were created or already exist.
    """
    base = get_user_base_path(user_id)
    subdirs = ["inbox", "memories", bot_dir, "shared"]

    success = True
    for subdir in subdirs:
        path = f"{base}/{subdir}"
        if not _rclone_mkdir(remote, path):
            # mkdir may fail if it already exists, so check existence
            if not _rclone_path_exists(remote, path):
                success = False

    # Create bot_dir subdirectories
    for sub in ["exports", "scripts", "notes"]:
        sub_path = f"{base}/{bot_dir}/{sub}"
        if not _rclone_mkdir(remote, sub_path):
            if not _rclone_path_exists(remote, sub_path):
                success = False

    return success


def user_directories_exist(remote: str, user_id: str, bot_dir: str) -> dict[str, bool]:
    """
    Check which user directories exist.

    Returns dict mapping directory name to existence status.
    """
    base = get_user_base_path(user_id)
    subdirs = ["inbox", "memories", bot_dir, "shared"]

    result = {}
    for subdir in subdirs:
        path = f"{base}/{subdir}"
        result[subdir] = _rclone_path_exists(remote, path)

    return result


def read_user_memory(remote: str, user_id: str, bot_dir: str) -> str | None:
    """
    Read the user's memory file.

    Returns the content of the memory file, or None if it doesn't exist or is empty.
    """
    memory_path = get_user_memory_path(user_id, bot_dir)
    content = _rclone_cat(remote, memory_path)

    if content is None or not content.strip():
        return None

    return content


def init_user_memory(remote: str, user_id: str, bot_dir: str) -> bool:
    """
    Initialize the user's memory file with a template.

    Returns True on success.
    """
    memory_path = get_user_memory_path(user_id, bot_dir)
    return _rclone_rcat(remote, memory_path, MEMORY_TEMPLATE)


def get_memory_line_count(remote: str, user_id: str, bot_dir: str) -> int | None:
    """
    Get the line count of a user's memory file.

    Returns None if file doesn't exist.
    """
    content = read_user_memory(remote, user_id, bot_dir)
    if content is None:
        return None
    return len(content.splitlines())


def get_user_inbox_path(user_id: str) -> str:
    """Get the path to a user's inbox directory."""
    return f"{get_user_base_path(user_id)}/inbox"


def upload_file_to_inbox(
    remote: str,
    user_id: str,
    local_path: Path,
    remote_filename: str | None = None,
) -> str | None:
    """
    Upload a local file to the user's inbox directory.

    Args:
        remote: rclone remote name
        user_id: User ID
        local_path: Local file path to upload
        remote_filename: Optional filename to use on remote (defaults to local filename)

    Returns:
        The remote path on success, None on failure.
    """
    if not local_path.exists():
        return None

    filename = remote_filename or local_path.name
    inbox_path = get_user_inbox_path(user_id)
    remote_path = f"{inbox_path}/{filename}"

    result = _rclone_run(["rclone", "copyto", str(local_path), f"{remote}:{remote_path}"])

    if result is None or result.returncode != 0:
        return None

    return remote_path


# =============================================================================
# Mount-aware storage functions
# =============================================================================


def _get_mount_path(config: "Config", path: str) -> Path:
    """Get the local mount path for a Nextcloud path."""
    return config.nextcloud_mount_path / path.lstrip("/")


def _migrate_old_layout(user_base: Path) -> None:
    """
    Migrate from old directory layout to new one.

    Old layout:
        context/memory.md → USER.md
        context/YYYY-MM-DD.md → memories/YYYY-MM-DD.md

    Only runs if context/ exists and target files don't. Safe to call repeatedly.
    """
    context_dir = user_base / "context"
    if not context_dir.is_dir():
        return

    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")

    # Migrate memory.md → USER.md
    old_memory = context_dir / "memory.md"
    new_memory = user_base / "USER.md"
    if old_memory.exists() and not new_memory.exists():
        shutil.copy2(old_memory, new_memory)
        logger.info("Migrated %s → %s", old_memory, new_memory)

    # Migrate dated files → memories/
    memories_dir = user_base / "memories"
    memories_dir.mkdir(exist_ok=True)
    for f in context_dir.iterdir():
        if f.is_file() and date_pattern.match(f.name):
            dest = memories_dir / f.name
            if not dest.exists():
                shutil.copy2(f, dest)
                logger.info("Migrated %s → %s", f, dest)


def _migrate_notes_to_workspace(user_base: Path) -> None:
    """
    Migrate from notes/ to workspace/ directory.

    Only runs if notes/ exists and workspace/ doesn't. Safe to call repeatedly.
    """
    notes_dir = user_base / "notes"
    workspace_dir = user_base / "workspace"
    if notes_dir.is_dir() and not workspace_dir.exists():
        notes_dir.rename(workspace_dir)
        logger.info("Migrated %s → %s", notes_dir, workspace_dir)


def _migrate_workspace_files(user_base: Path) -> None:
    """
    Migrate USER.md and TASKS.md from user root into workspace/.

    Only runs if workspace/ already exists (from a previous migration).
    Does not create workspace/ — the bot_dir layout supersedes it.
    """
    workspace_dir = user_base / "workspace"
    if not workspace_dir.is_dir():
        return

    for filename in ("USER.md", "TASKS.md"):
        src = user_base / filename
        dst = workspace_dir / filename
        if src.exists() and not dst.exists():
            shutil.move(str(src), str(dst))
            logger.info("Migrated %s → %s", src, dst)


# Config files that live in bot_name/config/
_CONFIG_FILES = (
    "USER.md", "TASKS.md", "BRIEFINGS.md", "HEARTBEAT.md",
    "CRON.md",
)


def _migrate_workspace_to_bot_dir(user_base: Path, bot_dir: str) -> None:
    """
    Migrate from workspace/ to bot directory layout.

    1. If workspace/ exists and bot dir doesn't → rename workspace/ → bot_dir/
    2. Move config .md files from bot_dir/ root into bot_dir/config/

    Safe to call repeatedly.
    """
    workspace_dir = user_base / "workspace"
    bot_dir_path = user_base / bot_dir

    # Step 1: rename workspace/ → bot_dir/
    if workspace_dir.is_dir() and not bot_dir_path.exists():
        workspace_dir.rename(bot_dir_path)
        logger.info("Migrated %s → %s", workspace_dir, bot_dir_path)

    # Step 2: move config files from bot_dir/ root into bot_dir/config/
    if bot_dir_path.is_dir():
        config_dir = bot_dir_path / "config"
        config_dir.mkdir(exist_ok=True)
        for filename in _CONFIG_FILES:
            src = bot_dir_path / filename
            dst = config_dir / filename
            if src.is_file() and not dst.exists():
                shutil.move(str(src), str(dst))
                logger.info("Migrated %s → %s", src, dst)


def ensure_user_directories_v2(config: "Config", user_id: str) -> bool:
    """
    Create the bot-managed directory structure for a user (mount-aware).

    Returns True if all directories were created or already exist.
    """
    bot_dir = config.bot_dir_name
    if config.use_mount:
        base = _get_mount_path(config, get_user_base_path(user_id))

        # Run migrations before creating directories
        _migrate_old_layout(base)
        _migrate_notes_to_workspace(base)
        _migrate_workspace_files(base)
        _migrate_workspace_to_bot_dir(base, bot_dir)

        subdirs = ["inbox", "memories", bot_dir, "shared"]
        for subdir in subdirs:
            path = base / subdir
            path.mkdir(parents=True, exist_ok=True)
        logger.debug("Ensured user directories for %s via mount", user_id)

        # Ensure bot dir subdirectories
        bot_dir_path = base / bot_dir
        config_dir = bot_dir_path / "config"
        config_dir.mkdir(exist_ok=True)
        exports_dir = bot_dir_path / "exports"
        exports_dir.mkdir(exist_ok=True)
        scripts_dir = bot_dir_path / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        notes_dir = bot_dir_path / "notes"
        notes_dir.mkdir(exist_ok=True)

        # Migrate scripts/ from user root into bot dir
        old_scripts = base / "scripts"
        if old_scripts.is_dir() and any(old_scripts.iterdir()):
            for item in old_scripts.iterdir():
                dst = scripts_dir / item.name
                if not dst.exists():
                    shutil.move(str(item), str(dst))
                    logger.info("Migrated %s → %s", item, dst)

        # Migrate old exports/ to bot_dir/exports/
        old_exports = base / "exports"
        if old_exports.is_dir() and any(old_exports.iterdir()):
            for item in old_exports.iterdir():
                dst = exports_dir / item.name
                if not dst.exists():
                    shutil.move(str(item), str(dst))
                    logger.info("Migrated %s → %s", item, dst)

        # Seed bot dir with README
        readme = bot_dir_path / "README.md"
        if not readme.exists():
            readme.write_text(WORKSPACE_README)
            logger.debug("Created %s README for %s", bot_dir, user_id)

        # Seed config/ with default files
        tasks_file = config_dir / "TASKS.md"
        if not tasks_file.exists():
            tasks_file.write_text(TASKS_FILE_TEMPLATE)
            logger.debug("Created %s/config/TASKS.md for %s", bot_dir, user_id)

        briefings_file = config_dir / "BRIEFINGS.md"
        if not briefings_file.exists():
            briefings_file.write_text(_build_briefings_seed(config, user_id))
            logger.debug("Created %s/config/BRIEFINGS.md for %s", bot_dir, user_id)

        heartbeat_file = config_dir / "HEARTBEAT.md"
        if not heartbeat_file.exists():
            heartbeat_file.write_text(_build_heartbeat_seed(config, user_id))
            logger.debug("Created %s/config/HEARTBEAT.md for %s", bot_dir, user_id)

        cron_file = config_dir / "CRON.md"
        if not cron_file.exists():
            cron_file.write_text(_build_cron_seed(config, user_id))
            logger.debug("Created %s/config/CRON.md for %s", bot_dir, user_id)

        # Seed PERSONA.md from global persona file
        persona_file = config_dir / "PERSONA.md"
        if not persona_file.exists():
            global_persona = config.skills_dir.parent / "persona.md"
            if global_persona.exists():
                persona_file.write_text(global_persona.read_text())
                logger.debug("Created %s/config/PERSONA.md for %s", bot_dir, user_id)

        # Write example files (always overwrite to stay current)
        examples_dir = bot_dir_path / "examples"
        examples_dir.mkdir(exist_ok=True)
        examples = {
            "README.md": WORKSPACE_README_EXAMPLE,
            "TASKS.md": TASKS_FILE_EXAMPLE,
            "BRIEFINGS.md": BRIEFINGS_EXAMPLE,
            "HEARTBEAT.md": HEARTBEAT_EXAMPLE,
            "CRON.md": CRON_EXAMPLE,
            "WORKFLOW.md": WORKFLOW_EXAMPLE,
        }
        for filename, content in examples.items():
            (examples_dir / filename).write_text(content)
        logger.debug("Updated %s examples for %s", bot_dir, user_id)

        # Auto-share bot dir back to the user (OCS). Skipped entirely when
        # Nextcloud is unconfigured (local install) — the OCS call is a no-op
        # there and would only log a spurious "Cannot share folder" warning.
        #
        # `auto_share_bot_dir` is the second guard, and it is a deployment
        # shape rather than a preference. On bare metal this share is how the
        # user gets the bot workspace at all. On the Docker shape
        # `provision-nc.sh` gives them a `files_external` mount over the very
        # same directory at first provisioning, so the share would put a second
        # copy of it in their file list under a different name.
        if config.nextcloud.url and config.nextcloud.auto_share_bot_dir:
            bot_path = get_user_bot_path(user_id, bot_dir)
            share_folder_with_user(config, bot_path, user_id)

        return True
    else:
        result = ensure_user_directories(config.rclone_remote, user_id, bot_dir)
        if result:
            logger.debug("Ensured user directories for %s via rclone", user_id)
        return result


def user_directories_exist_v2(config: "Config", user_id: str) -> dict[str, bool]:
    """
    Check which user directories exist (mount-aware).

    Returns dict mapping directory name to existence status.
    """
    if config.use_mount:
        base = _get_mount_path(config, get_user_base_path(user_id))
        subdirs = ["inbox", "memories", config.bot_dir_name, "shared"]
        return {subdir: (base / subdir).exists() for subdir in subdirs}
    else:
        return user_directories_exist(config.rclone_remote, user_id, config.bot_dir_name)


def read_user_memory_v2(config: "Config", user_id: str) -> str | None:
    """
    Read the user's memory file (mount-aware).

    Returns the content of the memory file, or None if it doesn't exist or is empty.
    """
    if config.use_mount:
        memory_path = _get_mount_path(config, get_user_memory_path(user_id, config.bot_dir_name))
        if not memory_path.exists():
            return None
        content = memory_path.read_text()
        if not content.strip():
            return None
        return content
    else:
        return read_user_memory(config.rclone_remote, user_id, config.bot_dir_name)


def init_user_memory_v2(config: "Config", user_id: str) -> bool:
    """
    Initialize the user's memory file with a template (mount-aware).

    Returns True on success.
    """
    if config.use_mount:
        memory_path = _get_mount_path(config, get_user_memory_path(user_id, config.bot_dir_name))
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text(MEMORY_TEMPLATE)
        return True
    else:
        return init_user_memory(config.rclone_remote, user_id, config.bot_dir_name)


def ensure_workspace_for_user(config: "Config", user_id: str) -> bool:
    """Seed a user's full workspace (directories + memory template).

    Shared by ``istota setup`` (first-run) and the daemon startup path so both
    guarantee the same layout. Directory creation is idempotent; the USER.md
    memory template is written only when absent (never clobbers existing
    memory on a re-run). Returns True on success.
    """
    ok = ensure_user_directories_v2(config, user_id)
    if get_memory_line_count_v2(config, user_id) is None:
        init_user_memory_v2(config, user_id)
    return ok


def get_memory_line_count_v2(config: "Config", user_id: str) -> int | None:
    """
    Get the line count of a user's memory file (mount-aware).

    Returns None if file doesn't exist.
    """
    content = read_user_memory_v2(config, user_id)
    if content is None:
        return None
    return len(content.splitlines())


def upload_file_to_inbox_v2(
    config: "Config",
    user_id: str,
    local_path: Path,
    remote_filename: str | None = None,
) -> str | None:
    """
    Upload a local file to the user's inbox directory (mount-aware).

    Args:
        config: Application config
        user_id: User ID
        local_path: Local file path to upload
        remote_filename: Optional filename to use on remote (defaults to local filename)

    Returns:
        The remote path on success, None on failure.
    """
    if not local_path.exists():
        return None

    filename = remote_filename or local_path.name
    inbox_path = get_user_inbox_path(user_id)
    remote_path = f"{inbox_path}/{filename}"

    if config.use_mount:
        dst = _get_mount_path(config, remote_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(local_path), str(dst))
        return remote_path
    else:
        return upload_file_to_inbox(config.rclone_remote, user_id, local_path, remote_filename)


# Date pattern for dated memory files (YYYY-MM-DD.md)
_DATED_MEMORY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


def read_dated_memories(
    config: "Config",
    user_id: str,
    max_days: int = 7,
    max_chars: int = 4000,
) -> str | None:
    """
    Read recent dated memory files from a user's memories directory.

    Scans /Users/{user_id}/memories/ for YYYY-MM-DD.md files within max_days,
    concatenates newest-first, and caps at max_chars.

    Returns concatenated content, or None if no dated files found.
    """
    if not config.use_mount:
        return None  # Only supported with mount

    context_dir = _get_mount_path(config, get_user_memories_path(user_id))
    if not context_dir.exists():
        return None

    # Cutoff is computed in the user's timezone so it lines up with the
    # filenames the sleep cycle writes (which are user-local YYYY-MM-DD).
    # Falling back to UTC matches the historical behavior for callers
    # without a configured user timezone.
    # Live DB timezone so the cutoff matches the user-local filenames the
    # sleep cycle writes, even after a web-UI tz change (ISSUE-099).
    tz_name = (
        config.resolve_user_timezone(user_id)
        if hasattr(config, "resolve_user_timezone")
        else "UTC"
    )
    try:
        user_tz = ZoneInfo(tz_name)
    except Exception:
        user_tz = ZoneInfo("UTC")
    cutoff = datetime.now(user_tz) - timedelta(days=max_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    # Find matching files
    dated_files = []
    for path in context_dir.iterdir():
        if path.is_file() and _DATED_MEMORY_PATTERN.match(path.name):
            date_str = path.stem  # e.g. "2026-01-28"
            if date_str >= cutoff_str:
                dated_files.append((date_str, path))

    if not dated_files:
        return None

    # Sort newest-first
    dated_files.sort(key=lambda x: x[0], reverse=True)

    # Concatenate with headers, respecting max_chars
    parts = []
    total = 0
    for date_str, path in dated_files:
        content = path.read_text().strip()
        if not content:
            continue
        entry = f"### {date_str}\n\n{content}\n"
        if total + len(entry) > max_chars:
            # Include partial if we have nothing yet
            if not parts:
                remaining = max_chars - total
                parts.append(entry[:remaining] + "...[truncated]")
            break
        parts.append(entry)
        total += len(entry)

    if not parts:
        return None

    return "\n".join(parts)


# =============================================================================
# Channel memory functions
# =============================================================================

CHANNEL_MEMORY_TEMPLATE = """# Channel Memory

This file contains remembered information about this channel/room.
The bot can append to this file to remember things relevant to all participants.

## Notes

"""


_TOKEN_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_conversation_token(token: str) -> str:
    """Validate that a conversation token is safe for filesystem use."""
    if not token or not _TOKEN_PATTERN.match(token):
        raise ValueError(f"Invalid conversation token: {token!r}")
    return token


def get_channel_base_path(conversation_token: str) -> str:
    """Get the base path for a channel's bot-managed directory."""
    validate_conversation_token(conversation_token)
    return f"{CHANNEL_BASE}/{conversation_token}"


def get_channel_memory_path(conversation_token: str) -> str:
    """Get the path to a channel's memory file."""
    return f"{get_channel_base_path(conversation_token)}/CHANNEL.md"


def get_channel_memories_path(conversation_token: str) -> str:
    """Get the path to a channel's dated memories directory."""
    return f"{get_channel_base_path(conversation_token)}/memories"


def ensure_channel_directories(config: "Config", conversation_token: str) -> bool:
    """
    Create the bot-managed directory structure for a channel (mount-aware).

    Creates /Channels/{token}/memories/

    Returns True if directory was created or already exists.
    """
    if config.use_mount:
        base = _get_mount_path(config, get_channel_base_path(conversation_token))
        memories_dir = base / "memories"
        memories_dir.mkdir(parents=True, exist_ok=True)

        # Migrate old layout: context/memory.md → CHANNEL.md
        old_memory = base / "context" / "memory.md"
        new_memory = base / "CHANNEL.md"
        if old_memory.exists() and not new_memory.exists():
            shutil.copy2(old_memory, new_memory)
            logger.info("Migrated channel memory %s → %s", old_memory, new_memory)

        logger.debug("Ensured channel directories for %s via mount", conversation_token)
        return True
    else:
        path = get_channel_memories_path(conversation_token)
        if not _rclone_mkdir(config.rclone_remote, path):
            if not _rclone_path_exists(config.rclone_remote, path):
                return False
        return True


def read_channel_memory(config: "Config", conversation_token: str) -> str | None:
    """
    Read the channel's memory file (mount-aware).

    Returns the content of the memory file, or None if it doesn't exist or is empty.
    """
    if config.use_mount:
        memory_path = _get_mount_path(config, get_channel_memory_path(conversation_token))
        if not memory_path.exists():
            return None
        # Explicit UTF-8 both ways: the web save hashes the content as UTF-8 to
        # build its revision tag, so a locale-dependent decode here would make
        # the same bytes hash two ways and every save read as a conflict.
        content = memory_path.read_text(encoding="utf-8")
        if not content.strip():
            return None
        return content
    else:
        memory_path = get_channel_memory_path(conversation_token)
        content = _rclone_cat(config.rclone_remote, memory_path)
        if content is None or not content.strip():
            return None
        return content


def write_channel_memory(
    config: "Config", conversation_token: str, content: str,
) -> bool:
    """Replace the channel's memory file with `content` (mount-aware).

    Returns False on a write that failed; raises `ValueError` on a token that
    isn't filesystem-safe.

    On the mount the write is tmp + `os.replace`: a reader — the executor
    loading the file into a prompt, or the sleep cycle re-indexing it — must
    never see a half-written file. The caller owns the read-modify-write window
    around it (`memory_md_lock` plus a revision check); this only guarantees the
    write itself is not observable half-done.

    **The staging name is unique per writer, not `CHANNEL.md.tmp`.** `os.replace`
    is atomic but the staging is not, and a fixed name is shared: the memory
    skill CLI computes the byte-identical path for the same target, and its lock
    anchor is per-user (`ISTOTA_DEFERRED_DIR`), so a web save by one member of a
    shared Talk room and a task write by another are genuinely concurrent under
    different locks. Two interleaved writes into one staging file publish a
    mixture of both, which the revision check cannot catch — it guards against a
    lost update, and the tearing happens after it. `mkstemp` is what makes the
    promise above true rather than merely intended.

    The rclone branch is **not** atomic: `rclone rcat` streams the object, so a
    concurrent reader can observe a partial one. Nothing local exists to stage
    through there, and the caller's lock does not help because a remote is
    shared across hosts.
    """
    if config.use_mount:
        memory_path = _get_mount_path(config, get_channel_memory_path(conversation_token))
        tmp_path = None
        try:
            memory_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=memory_path.parent, prefix=f".{memory_path.name}.", suffix=".tmp",
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            # mkstemp is 0600; the file is about to become CHANNEL.md, which the
            # user reads over Nextcloud like any other file in their channel dir.
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, memory_path)
            return True
        except OSError as e:
            logger.warning("channel memory write failed for %s: %s", conversation_token, e)
            if tmp_path is not None:
                # A stray staging file would otherwise sit in the user's own
                # channel directory forever.
                tmp_path.unlink(missing_ok=True)
            return False
    else:
        return _rclone_rcat(
            config.rclone_remote,
            get_channel_memory_path(conversation_token),
            content,
        )


def init_channel_memory(config: "Config", conversation_token: str) -> bool:
    """
    Initialize the channel's memory file with a template (mount-aware).

    Returns True on success.
    """
    if config.use_mount:
        memory_path = _get_mount_path(config, get_channel_memory_path(conversation_token))
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text(CHANNEL_MEMORY_TEMPLATE)
        return True
    else:
        return _rclone_rcat(
            config.rclone_remote,
            get_channel_memory_path(conversation_token),
            CHANNEL_MEMORY_TEMPLATE,
        )


# =============================================================================
# Nextcloud OCS sharing functions
# =============================================================================


def share_folder_with_user(config: "Config", folder_path: str, user_id: str) -> bool:
    """
    Share a folder with a Nextcloud user via the OCS Sharing API.

    Creates a user share (shareType=0) with full permissions (read+write).
    Idempotent: checks existing shares first.

    Delegates to nextcloud_client.ocs_share_folder.
    """
    from .nextcloud_client import ocs_share_folder
    return ocs_share_folder(config, folder_path, user_id)
