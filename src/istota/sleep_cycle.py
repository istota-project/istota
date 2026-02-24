"""Nightly sleep cycle — extract long-term memories from the day's interactions."""

import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

from . import db
from .config import Config
from .storage import (
    _get_mount_path,
    get_user_memories_path,
    get_user_memory_path,
    get_channel_memories_path,
    read_user_memory_v2,
    read_dated_memories,
    read_channel_memory,
    _DATED_MEMORY_PATTERN,
)

logger = logging.getLogger("istota.sleep_cycle")

# Maximum chars of day data to include in extraction prompt
MAX_DAY_DATA_CHARS = 50000

# Sentinel output from Claude indicating nothing worth saving
NO_NEW_MEMORIES = "NO_NEW_MEMORIES"


def gather_day_data(
    config: Config,
    conn: "db.sqlite3.Connection",
    user_id: str,
    lookback_hours: int,
    after_task_id: int | None,
) -> str:
    """
    Gather the day's interaction data for memory extraction.

    Reads completed task prompts/results from the DB and prompt files
    from the user's temp directory.

    Returns concatenated day data string.
    """
    since = datetime.now(tz=ZoneInfo("UTC")) - timedelta(hours=lookback_hours)
    # DB stores naive UTC timestamps, so strip tzinfo for comparison
    since_str = since.replace(tzinfo=None).isoformat()

    tasks = db.get_completed_tasks_since(conn, user_id, since_str, after_task_id)

    if not tasks:
        return ""

    parts = []
    for task in tasks:
        prompt_excerpt = task.prompt[:2000] if task.prompt else ""
        result_excerpt = (task.result or "")[:3000]
        parts.append(
            f"--- Task {task.id} ({task.source_type}, {task.created_at or 'unknown'}) ---\n"
            f"User: {prompt_excerpt}\n"
            f"Bot: {result_excerpt}\n"
        )

    combined = "\n".join(parts)
    if len(combined) > MAX_DAY_DATA_CHARS:
        combined = combined[:MAX_DAY_DATA_CHARS] + "\n...[truncated]"

    return combined


def build_memory_extraction_prompt(
    user_id: str,
    day_data: str,
    existing_memory: str | None,
    date_str: str,
) -> str:
    """
    Build the prompt that instructs Claude to extract memories from the day's interactions.

    Args:
        user_id: The user ID
        day_data: Concatenated interaction data from the day
        existing_memory: Current contents of memory.md (to avoid duplication)
        date_str: Date string for the memory file (e.g. "2026-01-28")
    """
    existing_section = ""
    if existing_memory:
        existing_section = f"""
## Existing long-term memory (memory.md)

The following information is already stored in the user's permanent memory file.
Do NOT repeat any of this information in your output.

{existing_memory}
"""

    return f"""You are extracting important memories from a day of interactions with user '{user_id}'.

Date: {date_str}
{existing_section}
## Today's interactions

{day_data}

## Instructions

Review the interactions above and extract information worth remembering for future conversations.
Focus on:
- New facts about the user (preferences, projects, people, habits)
- Decisions made or plans discussed
- Corrections the user made
- Important context that would help in future interactions
- Key outcomes of tasks (e.g., "sent email to X about Y", "created report Z")

Do NOT include:
- Information already in the existing memory above
- Trivial exchanges (greetings, acknowledgments)
- Temporary states that are no longer relevant
- Raw data or lengthy outputs

Format your output as concise bullet points with dates and task references, like:
- Decided to switch project Alpha to Python 3.12 (2026-01-28, ref:1234)
- Prefers email summaries over detailed reports (2026-01-28, ref:1235)

If there is genuinely nothing new worth remembering, respond with exactly: {NO_NEW_MEMORIES}

Output ONLY the bullet points (or {NO_NEW_MEMORIES}). No preamble, no explanation."""


def process_user_sleep_cycle(
    config: Config,
    conn: "db.sqlite3.Connection",
    user_id: str,
) -> bool:
    """
    Run the sleep cycle for one user: gather data, extract memories, write file.

    Returns True if a memory file was written.
    """
    sleep_config = config.sleep_cycle

    # Get last run state
    last_run_at, last_task_id = db.get_sleep_cycle_last_run(conn, user_id)

    # Gather day data
    day_data = gather_day_data(
        config, conn, user_id, sleep_config.lookback_hours, last_task_id
    )

    if not day_data.strip():
        logger.info("Sleep cycle for %s: no new interactions, skipping", user_id)
        db.set_sleep_cycle_last_run(conn, user_id, last_task_id)
        return False

    # Load existing memory to avoid duplication
    existing_memory = read_user_memory_v2(config, user_id)

    # Build extraction prompt
    date_str = datetime.now().strftime("%Y-%m-%d")
    prompt = build_memory_extraction_prompt(
        user_id, day_data, existing_memory, date_str
    )

    # Call Claude CLI (like context.py does)
    try:
        result = subprocess.run(
            ["claude", "-p", "-", "--model", "sonnet"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            logger.error(
                "Sleep cycle extraction failed for %s (rc=%d): %s",
                user_id,
                result.returncode,
                result.stderr[:200] if result.stderr else "",
            )
            return False

        output = result.stdout.strip()

    except subprocess.TimeoutExpired:
        logger.error("Sleep cycle extraction timed out for %s", user_id)
        return False
    except FileNotFoundError:
        logger.error("Claude CLI not found for sleep cycle extraction")
        return False
    except Exception as e:
        logger.error("Sleep cycle extraction error for %s: %s", user_id, e)
        return False

    # Check for sentinel
    if output == NO_NEW_MEMORIES:
        logger.info("Sleep cycle for %s: no new memories to save", user_id)
        # Still update state so we don't reprocess these tasks
        _update_state(config, conn, user_id, last_task_id)
        return False

    # Write dated memory file
    if not config.use_mount:
        logger.warning("Sleep cycle requires mount mode, skipping file write for %s", user_id)
        _update_state(config, conn, user_id, last_task_id)
        return False

    context_dir = _get_mount_path(config, get_user_memories_path(user_id))
    context_dir.mkdir(parents=True, exist_ok=True)

    memory_file = context_dir / f"{date_str}.md"
    memory_file.write_text(output + "\n")
    logger.info("Wrote dated memory file for %s: %s (%d chars)", user_id, memory_file.name, len(output))

    # Index memory file for semantic search (non-critical)
    if config.memory_search.enabled and config.memory_search.auto_index_memory_files:
        try:
            from .memory_search import index_file as _index_file
            _index_file(conn, user_id, str(memory_file), output, "memory_file")
        except Exception as e:
            logger.debug("Memory search indexing failed for %s: %s", memory_file.name, e)

    # Update state
    _update_state(config, conn, user_id, last_task_id)

    # Clean up old memory files
    cleanup_old_memory_files(config, user_id, sleep_config.memory_retention_days)

    # Curate USER.md if enabled
    if sleep_config.curate_user_memory:
        try:
            curate_user_memory(config, user_id)
        except Exception as e:
            logger.error("USER.md curation failed for %s: %s", user_id, e)

    return True


# Sentinel output from Claude indicating no curation changes needed
NO_CHANGES_NEEDED = "NO_CHANGES_NEEDED"


def build_curation_prompt(
    user_id: str,
    current_memory: str | None,
    dated_memories: str,
) -> str:
    """Build the prompt that instructs Claude to curate USER.md from dated memories."""
    current_section = ""
    if current_memory:
        current_section = f"""
## Current USER.md

{current_memory}
"""
    else:
        current_section = """
## Current USER.md

(Empty — no existing memory file)
"""

    return f"""You are curating the persistent memory file (USER.md) for user '{user_id}'.

{current_section}
## Recent dated memories

The following memories were extracted from recent conversations:

{dated_memories}

## Instructions

Update USER.md by:
1. Promoting durable facts from the dated memories (preferences, projects, people, decisions)
2. Removing entries that are outdated or contradicted by newer information
3. Keeping the file concise and well-organized under clear headings
4. Preserving the existing structure and headings where possible

Do NOT include:
- Temporary or time-bound information (e.g., "meeting tomorrow")
- Task references (ref:NNNN) — those belong in dated memories only
- Redundant entries — if info is already in USER.md, don't duplicate it

If USER.md is already up to date and no changes are needed, respond with exactly: {NO_CHANGES_NEEDED}

Otherwise, output the COMPLETE updated USER.md content. No preamble, no explanation — just the file content."""


def curate_user_memory(config: Config, user_id: str) -> bool:
    """Second pass: update USER.md based on accumulated dated memories.

    Returns True if USER.md was updated.
    """
    current_memory = read_user_memory_v2(config, user_id)
    dated = read_dated_memories(config, user_id, max_days=30, max_chars=12000)
    if not dated:
        return False  # Nothing to curate from

    prompt = build_curation_prompt(user_id, current_memory, dated)

    try:
        result = subprocess.run(
            ["claude", "-p", "-", "--model", "sonnet"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            logger.error(
                "USER.md curation failed for %s (rc=%d): %s",
                user_id, result.returncode,
                result.stderr[:200] if result.stderr else "",
            )
            return False

        output = result.stdout.strip()

    except subprocess.TimeoutExpired:
        logger.error("USER.md curation timed out for %s", user_id)
        return False
    except FileNotFoundError:
        logger.error("Claude CLI not found for USER.md curation")
        return False
    except Exception as e:
        logger.error("USER.md curation error for %s: %s", user_id, e)
        return False

    if output == NO_CHANGES_NEEDED:
        logger.info("USER.md curation for %s: no changes needed", user_id)
        return False

    if not config.use_mount:
        logger.warning("USER.md curation requires mount mode, skipping for %s", user_id)
        return False

    memory_path = _get_mount_path(config, get_user_memory_path(user_id, config.bot_dir_name))
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_text(output + "\n")
    logger.info("Updated USER.md for %s (%d chars)", user_id, len(output))
    return True


def _update_state(
    config: Config,
    conn: "db.sqlite3.Connection",
    user_id: str,
    previous_last_task_id: int | None,
) -> None:
    """Update sleep cycle state with the latest completed task ID."""
    # Find the latest completed task ID for this user
    since = (datetime.now(tz=ZoneInfo("UTC")) - timedelta(hours=48)).replace(tzinfo=None).isoformat()
    tasks = db.get_completed_tasks_since(conn, user_id, since, previous_last_task_id)
    if tasks:
        latest_id = tasks[-1].id
    else:
        latest_id = previous_last_task_id
    db.set_sleep_cycle_last_run(conn, user_id, latest_id)


def cleanup_old_memory_files(
    config: Config,
    user_id: str,
    retention_days: int,
) -> int:
    """
    Delete dated memory files older than retention_days.

    Returns number of files deleted. If retention_days <= 0, cleanup is
    skipped (unlimited retention).
    """
    if retention_days <= 0:
        return 0

    if not config.use_mount:
        return 0

    context_dir = _get_mount_path(config, get_user_memories_path(user_id))
    if not context_dir.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=retention_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    deleted = 0
    for path in context_dir.iterdir():
        if path.is_file() and _DATED_MEMORY_PATTERN.match(path.name):
            date_str = path.stem
            if date_str < cutoff_str:
                path.unlink()
                deleted += 1
                logger.debug("Deleted old memory file: %s", path.name)

    if deleted:
        logger.info("Cleaned up %d old memory file(s) for %s", deleted, user_id)

    return deleted


def check_sleep_cycles(conn: "db.sqlite3.Connection", config: Config) -> list[str]:
    """
    Evaluate sleep cycle cron for all users, process when due.

    Returns list of user_ids that were processed.
    """
    if not config.sleep_cycle.enabled:
        return []

    sleep_config = config.sleep_cycle
    processed = []

    for user_id, user_config in config.users.items():
        # Evaluate cron in user's timezone
        try:
            user_tz = ZoneInfo(user_config.timezone)
        except Exception:
            user_tz = ZoneInfo("UTC")

        now = datetime.now(user_tz)

        should_run = False
        last_run_at, _ = db.get_sleep_cycle_last_run(conn, user_id)

        if last_run_at:
            last_run = datetime.fromisoformat(last_run_at)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=ZoneInfo("UTC"))
            cron = croniter(sleep_config.cron, last_run.astimezone(user_tz))
            next_run = cron.get_next(datetime)
            should_run = now >= next_run
        else:
            # Never run — check if we're past first scheduled time today
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            cron = croniter(sleep_config.cron, today_start)
            next_run = cron.get_next(datetime)
            should_run = now >= next_run

        if should_run:
            logger.info("Running sleep cycle for user %s", user_id)
            try:
                wrote = process_user_sleep_cycle(config, conn, user_id)
                if wrote:
                    processed.append(user_id)
            except Exception as e:
                logger.error("Sleep cycle failed for %s: %s", user_id, e)

    return processed


# ============================================================================
# Channel sleep cycle (shared channel memory extraction)
# ============================================================================


def gather_channel_data(
    config: Config,
    conn: "db.sqlite3.Connection",
    conversation_token: str,
    lookback_hours: int,
    after_task_id: int | None,
) -> str:
    """
    Gather channel interaction data for memory extraction.

    Like gather_day_data but filters by conversation_token and includes
    user_id attribution per task.
    """
    since = datetime.now(tz=ZoneInfo("UTC")) - timedelta(hours=lookback_hours)
    since_str = since.replace(tzinfo=None).isoformat()

    tasks = db.get_completed_channel_tasks_since(
        conn, conversation_token, since_str, after_task_id
    )

    if not tasks:
        return ""

    parts = []
    for task in tasks:
        prompt_excerpt = task.prompt[:2000] if task.prompt else ""
        result_excerpt = (task.result or "")[:3000]
        parts.append(
            f"--- Task {task.id} (user: {task.user_id}, {task.source_type}, {task.created_at or 'unknown'}) ---\n"
            f"User: {prompt_excerpt}\n"
            f"Bot: {result_excerpt}\n"
        )

    combined = "\n".join(parts)
    if len(combined) > MAX_DAY_DATA_CHARS:
        combined = combined[:MAX_DAY_DATA_CHARS] + "\n...[truncated]"

    return combined


def build_channel_memory_extraction_prompt(
    conversation_token: str,
    day_data: str,
    existing_memory: str | None,
    date_str: str,
) -> str:
    """
    Build prompt for extracting shared memories from channel conversations.

    Focuses on shared context (decisions, agreements, project status) rather
    than personal information.
    """
    existing_section = ""
    if existing_memory:
        existing_section = f"""
## Existing channel memory (CHANNEL.md)

The following information is already stored in this channel's memory file.
Do NOT repeat any of this information. Respect the existing structure —
produce new items that could be appended under appropriate headings.

{existing_memory}
"""

    return f"""You are extracting shared memories from a day of conversations in channel '{conversation_token}'.

Date: {date_str}
{existing_section}
## Today's channel interactions

{day_data}

## Instructions

Review the interactions above and extract information worth remembering as shared channel context.
Focus on:
- Decisions made or agreements reached by the group
- Project status updates, milestones, or blockers
- Action items assigned to specific people
- Technical decisions or architecture choices
- Important context that would help anyone in the channel
- Links between topics discussed here and other projects

Do NOT include:
- Information already in the existing channel memory above
- Personal/private information about individual users
- Trivial exchanges (greetings, acknowledgments, small talk)
- Temporary states that are no longer relevant
- Raw data or lengthy outputs

Format your output as concise bullet points with dates, attribution, and task references, like:
- Decided to migrate API to GraphQL (alice, 2026-01-28, ref:1234)
- Blocked on infrastructure approval for prod deploy (bob, 2026-01-28, ref:1235)

If there is genuinely nothing new worth remembering, respond with exactly: {NO_NEW_MEMORIES}

Output ONLY the bullet points (or {NO_NEW_MEMORIES}). No preamble, no explanation."""


def process_channel_sleep_cycle(
    config: Config,
    conn: "db.sqlite3.Connection",
    conversation_token: str,
) -> bool:
    """
    Run the channel sleep cycle: gather data, extract memories, write file.

    Returns True if a memory file was written.
    """
    csc = config.channel_sleep_cycle

    # Get last run state
    last_run_at, last_task_id = db.get_channel_sleep_cycle_last_run(
        conn, conversation_token
    )

    # Gather channel data
    day_data = gather_channel_data(
        config, conn, conversation_token, csc.lookback_hours, last_task_id
    )

    if not day_data.strip():
        logger.info(
            "Channel sleep cycle for %s: no new interactions, skipping",
            conversation_token,
        )
        db.set_channel_sleep_cycle_last_run(conn, conversation_token, last_task_id)
        return False

    # Load existing channel memory to avoid duplication
    existing_memory = read_channel_memory(config, conversation_token)

    # Build extraction prompt
    date_str = datetime.now().strftime("%Y-%m-%d")
    prompt = build_channel_memory_extraction_prompt(
        conversation_token, day_data, existing_memory, date_str
    )

    # Call Claude CLI
    try:
        result = subprocess.run(
            ["claude", "-p", "-", "--model", "sonnet"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            logger.error(
                "Channel sleep cycle extraction failed for %s (rc=%d): %s",
                conversation_token,
                result.returncode,
                result.stderr[:200] if result.stderr else "",
            )
            return False

        output = result.stdout.strip()

    except subprocess.TimeoutExpired:
        logger.error(
            "Channel sleep cycle extraction timed out for %s", conversation_token
        )
        return False
    except FileNotFoundError:
        logger.error("Claude CLI not found for channel sleep cycle extraction")
        return False
    except Exception as e:
        logger.error(
            "Channel sleep cycle extraction error for %s: %s",
            conversation_token,
            e,
        )
        return False

    # Check for sentinel
    if output == NO_NEW_MEMORIES:
        logger.info(
            "Channel sleep cycle for %s: no new memories to save",
            conversation_token,
        )
        _update_channel_state(config, conn, conversation_token, last_task_id)
        return False

    # Write dated memory file
    if not config.use_mount:
        logger.warning(
            "Channel sleep cycle requires mount mode, skipping file write for %s",
            conversation_token,
        )
        _update_channel_state(config, conn, conversation_token, last_task_id)
        return False

    memories_dir = _get_mount_path(config, get_channel_memories_path(conversation_token))
    memories_dir.mkdir(parents=True, exist_ok=True)

    memory_file = memories_dir / f"{date_str}.md"
    memory_file.write_text(output + "\n")
    logger.info(
        "Wrote channel memory file for %s: %s (%d chars)",
        conversation_token,
        memory_file.name,
        len(output),
    )

    # Index memory file for semantic search (non-critical)
    channel_user_id = f"channel:{conversation_token}"
    if config.memory_search.enabled and config.memory_search.auto_index_memory_files:
        try:
            from .memory_search import index_file as _index_file

            _index_file(
                conn,
                channel_user_id,
                str(memory_file),
                output,
                "channel_memory",
            )
        except Exception as e:
            logger.debug(
                "Memory search indexing failed for channel %s: %s",
                conversation_token,
                e,
            )

    # Update state
    _update_channel_state(config, conn, conversation_token, last_task_id)

    # Clean up old memory files
    cleanup_old_channel_memory_files(
        config, conversation_token, csc.memory_retention_days
    )

    return True


def _update_channel_state(
    config: Config,
    conn: "db.sqlite3.Connection",
    conversation_token: str,
    previous_last_task_id: int | None,
) -> None:
    """Update channel sleep cycle state with the latest completed task ID."""
    since = (
        (datetime.now(tz=ZoneInfo("UTC")) - timedelta(hours=48))
        .replace(tzinfo=None)
        .isoformat()
    )
    tasks = db.get_completed_channel_tasks_since(
        conn, conversation_token, since, previous_last_task_id
    )
    if tasks:
        latest_id = tasks[-1].id
    else:
        latest_id = previous_last_task_id
    db.set_channel_sleep_cycle_last_run(conn, conversation_token, latest_id)


def cleanup_old_channel_memory_files(
    config: Config,
    conversation_token: str,
    retention_days: int,
) -> int:
    """
    Delete dated channel memory files older than retention_days.

    Returns number of files deleted. If retention_days <= 0, cleanup is
    skipped (unlimited retention).
    """
    if retention_days <= 0:
        return 0

    if not config.use_mount:
        return 0

    memories_dir = _get_mount_path(
        config, get_channel_memories_path(conversation_token)
    )
    if not memories_dir.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=retention_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    deleted = 0
    for path in memories_dir.iterdir():
        if path.is_file() and _DATED_MEMORY_PATTERN.match(path.name):
            date_str = path.stem
            if date_str < cutoff_str:
                path.unlink()
                deleted += 1
                logger.debug("Deleted old channel memory file: %s", path.name)

    if deleted:
        logger.info(
            "Cleaned up %d old channel memory file(s) for %s",
            deleted,
            conversation_token,
        )

    return deleted


def check_channel_sleep_cycles(
    conn: "db.sqlite3.Connection",
    config: Config,
) -> list[str]:
    """
    Evaluate channel sleep cycle cron, auto-discover active channels, process when due.

    Returns list of conversation_tokens that were processed.
    """
    if not config.channel_sleep_cycle.enabled:
        return []

    csc = config.channel_sleep_cycle
    processed = []

    # Auto-discover active channels from recent completed tasks
    since = (
        (datetime.now(tz=ZoneInfo("UTC")) - timedelta(hours=csc.lookback_hours))
        .replace(tzinfo=None)
        .isoformat()
    )
    active_tokens = db.get_active_channel_tokens(conn, since)

    if not active_tokens:
        return []

    # Evaluate cron in UTC (channels span users in different timezones)
    now = datetime.now(ZoneInfo("UTC"))

    for token in active_tokens:
        should_run = False
        last_run_at, _ = db.get_channel_sleep_cycle_last_run(conn, token)

        if last_run_at:
            last_run = datetime.fromisoformat(last_run_at)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=ZoneInfo("UTC"))
            cron = croniter(csc.cron, last_run)
            next_run = cron.get_next(datetime)
            should_run = now >= next_run
        else:
            # Never run — check if we're past first scheduled time today
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            cron = croniter(csc.cron, today_start)
            next_run = cron.get_next(datetime)
            should_run = now >= next_run

        if should_run:
            logger.info("Running channel sleep cycle for %s", token)
            try:
                wrote = process_channel_sleep_cycle(config, conn, token)
                if wrote:
                    processed.append(token)
            except Exception as e:
                logger.error(
                    "Channel sleep cycle failed for %s: %s", token, e
                )

    return processed
