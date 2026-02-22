"""TASKS.md file polling and task creation."""

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from . import db
from .config import Config
from .skills.files import list_files, read_text, write_text

logger = logging.getLogger("istota.tasks_file_poller")


# Pattern for TASKS files: TASKS.md (case insensitive)
TASKS_FILE_PATTERN = re.compile(r'^TASKS\.md$', re.IGNORECASE)


@dataclass
class ParsedTask:
    """A parsed task from a TASKS.md file."""
    original_line: str
    normalized_content: str
    content_hash: str
    status: str  # 'pending', 'in_progress', 'completed', 'failed'


# Regex patterns for task lines
TASK_PATTERN = re.compile(
    r'^(\s*)-\s*\[(.)\]\s*(.+)$',
    re.MULTILINE
)

# Status markers
STATUS_PENDING = ' '
STATUS_IN_PROGRESS = '~'
STATUS_COMPLETED = 'x'
STATUS_FAILED = '!'


@dataclass
class DiscoveredTasksFile:
    """A discovered TASKS.md file with owner info."""
    file_path: str
    owner_id: str  # Nextcloud username of the file owner


def discover_tasks_files(config: Config) -> list[DiscoveredTasksFile]:
    """
    Discover TASKS.md files in users' bot-managed directories.

    Scans /Users/{user_id}/istota/config/ for TASKS.md files.
    The owner is known from the path structure.

    Args:
        config: Application config

    Returns:
        List of discovered TASKS files with owner info
    """
    from .storage import get_user_config_path

    discovered = []

    for user_id in config.users:
        user_path = get_user_config_path(user_id, config.bot_dir_name)

        try:
            files = list_files(config, user_path)
        except Exception:
            # User directory may not exist yet
            continue

        for item in files:
            if item["is_dir"]:
                continue

            filename = item["name"]
            if not TASKS_FILE_PATTERN.match(filename):
                continue

            discovered.append(DiscoveredTasksFile(
                file_path=f"{user_path}/{filename}",
                owner_id=user_id,
            ))

    return discovered


def normalize_task_content(content: str) -> str:
    """
    Normalize task content for hashing.

    Strips whitespace, lowercases, removes timestamps from completed tasks,
    removes trailing ellipsis from in-progress tasks.
    """
    # Remove any timestamp prefix (e.g., "2025-01-26 12:34 | ")
    content = re.sub(r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*\|\s*', '', content)
    # Remove any result/error suffix (e.g., " | Result: ..." or " | Error: ...")
    content = re.sub(r'\s*\|\s*(Result|Error):.*$', '', content, flags=re.IGNORECASE)
    # Remove trailing ellipsis (from in-progress tasks)
    content = re.sub(r'\.{2,}$', '', content)
    # Normalize whitespace and lowercase
    return ' '.join(content.lower().split())


def compute_content_hash(content: str) -> str:
    """
    Compute a stable hash for task content.

    Returns first 12 characters of SHA-256 hash.
    """
    normalized = normalize_task_content(content)
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:12]


def parse_tasks_file(content: str) -> list[ParsedTask]:
    """
    Parse a TASKS.md file and extract tasks.

    Returns list of ParsedTask objects for all valid task lines.
    """
    tasks = []

    for match in TASK_PATTERN.finditer(content):
        indent = match.group(1)
        marker = match.group(2)
        task_content = match.group(3).strip()
        original_line = match.group(0)

        # Determine status from marker
        if marker == STATUS_PENDING:
            status = 'pending'
        elif marker == STATUS_IN_PROGRESS:
            status = 'in_progress'
        elif marker.lower() == STATUS_COMPLETED:
            status = 'completed'
        elif marker == STATUS_FAILED:
            status = 'failed'
        else:
            # Unknown marker, skip
            continue

        normalized = normalize_task_content(task_content)
        content_hash = compute_content_hash(task_content)

        tasks.append(ParsedTask(
            original_line=original_line,
            normalized_content=normalized,
            content_hash=content_hash,
            status=status,
        ))

    return tasks


def update_task_in_file(
    file_content: str,
    content_hash: str,
    new_status: str,
    result_summary: str | None = None,
    error_message: str | None = None,
) -> str:
    """
    Update a task's status in the file content.

    Args:
        file_content: The current file content
        content_hash: Hash of the task to update
        new_status: New status ('in_progress', 'completed', 'failed')
        result_summary: Summary for completed tasks
        error_message: Error message for failed tasks

    Returns:
        Updated file content
    """
    lines = file_content.split('\n')
    updated_lines = []

    for line in lines:
        match = TASK_PATTERN.match(line)
        if match:
            indent = match.group(1)
            task_content = match.group(3).strip()
            line_hash = compute_content_hash(task_content)

            if line_hash == content_hash:
                # This is the task to update
                # Extract the original task description (without timestamps/results/ellipsis)
                original_content = re.sub(
                    r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*\|\s*',
                    '',
                    task_content
                )
                original_content = re.sub(
                    r'\s*\|\s*(Result|Error):.*$',
                    '',
                    original_content,
                    flags=re.IGNORECASE
                )
                # Remove trailing ellipsis (from in-progress tasks)
                original_content = re.sub(r'\.{2,}$', '', original_content).strip()

                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

                if new_status == 'in_progress':
                    # Mark as in progress
                    new_line = f"{indent}- [~] {original_content}..."
                elif new_status == 'completed':
                    # Mark as completed with timestamp and result
                    result_part = f" | Result: {result_summary}" if result_summary else ""
                    new_line = f"{indent}- [x] {timestamp} | {original_content}{result_part}"
                elif new_status == 'failed':
                    # Mark as failed with timestamp and error
                    error_part = f" | Error: {error_message}" if error_message else ""
                    new_line = f"{indent}- [!] {timestamp} | {original_content}{error_part}"
                else:
                    new_line = line

                updated_lines.append(new_line)
                continue

        updated_lines.append(line)

    return '\n'.join(updated_lines)


def poll_user_tasks_file(config: Config, user_id: str, file_path: str) -> list[int]:
    """
    Poll a user's TASKS.md file and create tasks for new pending items.

    Args:
        config: Application config
        user_id: User ID to poll for
        file_path: Path to the TASKS.md file

    Returns:
        List of created task IDs
    """
    # Read the file (mount-aware)
    try:
        file_content = read_text(config, file_path)
    except Exception as e:
        # File read error - log and skip
        logger.error("Error reading %s for %s: %s", file_path, user_id, e)
        return []

    # Parse tasks from file
    parsed_tasks = parse_tasks_file(file_content)
    created_task_ids = []
    file_updated = False
    updated_content = file_content

    with db.get_db(config.db_path) as conn:
        for parsed_task in parsed_tasks:
            # Only process pending tasks
            if parsed_task.status != 'pending':
                continue

            # Check if already tracked
            if db.is_istota_task_tracked(conn, user_id, parsed_task.content_hash):
                continue

            # Create the main task
            task_id = db.create_task(
                conn,
                prompt=parsed_task.normalized_content,
                user_id=user_id,
                source_type="istota_file",
                priority=5,
            )

            # Track the task file task
            db.track_istota_file_task(
                conn,
                user_id=user_id,
                content_hash=parsed_task.content_hash,
                original_line=parsed_task.original_line,
                normalized_content=parsed_task.normalized_content,
                file_path=file_path,
                task_id=task_id,
            )

            created_task_ids.append(task_id)

            # Update file to mark task as in progress
            updated_content = update_task_in_file(
                updated_content,
                parsed_task.content_hash,
                'in_progress',
            )
            file_updated = True

    # Write updated file back if any changes
    if file_updated:
        try:
            write_text(config, file_path, updated_content)
            logger.debug("Updated %s with %d new task(s)", file_path, len(created_task_ids))
        except Exception as e:
            logger.error("Error updating %s for %s: %s", file_path, user_id, e)

    return created_task_ids


def poll_all_tasks_files(config: Config) -> list[int]:
    """
    Poll all TASKS.md files and create tasks.

    Auto-discovers TASKS.md files in users' bot-managed directories
    and processes them.

    Args:
        config: Application config

    Returns:
        List of all created task IDs
    """
    all_task_ids = []

    # Auto-discover TASKS files
    discovered_files = discover_tasks_files(config)

    for tasks_file in discovered_files:
        task_ids = poll_user_tasks_file(config, tasks_file.owner_id, tasks_file.file_path)
        all_task_ids.extend(task_ids)

    return all_task_ids


def handle_tasks_file_completion(
    config: Config,
    task: db.Task,
    success: bool,
    result: str,
) -> None:
    """
    Handle completion of a TASKS.md file task.

    Updates the TASKS.md file with the result and optionally sends email notification.

    Args:
        config: Application config
        task: The completed task
        success: Whether the task succeeded
        result: Task result or error message
    """
    with db.get_db(config.db_path) as conn:
        istota_task = db.get_istota_file_task_by_task_id(conn, task.id)
        if not istota_task:
            return

        # Truncate result for summary
        result_summary = result[:200] if len(result) > 200 else result
        result_summary = result_summary.replace('\n', ' ').strip()

        # Update task file task status in DB
        if success:
            db.update_istota_file_task_status(
                conn,
                istota_task.id,
                'completed',
                result_summary=result_summary,
            )
        else:
            db.update_istota_file_task_status(
                conn,
                istota_task.id,
                'failed',
                error_message=result_summary,
            )

    # Update the file (using file_path from DB, mount-aware)
    try:
        file_content = read_text(config, istota_task.file_path)

        updated_content = update_task_in_file(
            file_content,
            istota_task.content_hash,
            'completed' if success else 'failed',
            result_summary=result_summary if success else None,
            error_message=result_summary if not success else None,
        )

        write_text(config, istota_task.file_path, updated_content)
        logger.debug("Updated %s with task completion", istota_task.file_path)
    except Exception as e:
        logger.error("Error updating %s after completion for %s: %s", istota_task.file_path, task.user_id, e)

    # Send email notification if user has email configured
    user_config = config.get_user(task.user_id)
    if user_config and user_config.email_addresses and config.email.enabled:
        try:
            from .skills.email import send_email

            to_addr = user_config.email_addresses[0]
            task_preview = istota_task.normalized_content[:50]
            if len(istota_task.normalized_content) > 50:
                task_preview += "..."

            if success:
                subject = f"[{config.bot_name}] Task Completed: {task_preview}"
                body = f"""Task completed successfully.

Original task: {istota_task.original_line}

Result:
{result}
"""
            else:
                subject = f"[{config.bot_name}] Task Failed: {task_preview}"
                body = f"""Task failed.

Original task: {istota_task.original_line}

Error:
{result}
"""

            from .email_poller import get_email_config
            email_config = get_email_config(config)
            send_email(
                to=to_addr,
                subject=subject,
                body=body,
                config=email_config,
                from_addr=config.email.bot_email,
            )
            logger.debug("Sent email notification for task %d to %s", task.id, to_addr)
        except Exception as e:
            logger.error("Error sending email notification for %s: %s", task.user_id, e)
