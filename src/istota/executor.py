"""Claude Code execution wrapper."""

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db
from .config import Config
from .context import format_context_for_prompt, select_relevant_context
from .storage import (
    ensure_channel_directories,
    ensure_user_directories_v2,
    get_user_persona_path,
    get_user_scripts_path,
    read_channel_memory,
    read_user_memory_v2,
)
from .stream_parser import ResultEvent, TextEvent, ToolUseEvent, parse_stream_line
from .skills.calendar import get_caldav_client, get_calendars_for_user

logger = logging.getLogger("istota.executor")

# Pattern to detect Anthropic API errors in output
API_ERROR_PATTERN = re.compile(r"API Error: (\d{3}) (\{.*\})", re.DOTALL)

# Transient HTTP status codes that warrant retry
TRANSIENT_STATUS_CODES = {500, 502, 503, 504, 529}  # 529 = overloaded

# Retry configuration for transient API errors
API_RETRY_MAX_ATTEMPTS = 3
API_RETRY_DELAY_SECONDS = 5

# Audio extensions eligible for pre-transcription (matches whisper skill file_types)
_AUDIO_EXTENSIONS = frozenset({"mp3", "wav", "ogg", "flac", "m4a", "opus", "webm", "mp4", "aac", "wma"})


def _pre_transcribe_attachments(
    attachments: list[str] | None,
    prompt: str,
) -> str:
    """Pre-transcribe audio attachments so skill selection sees real text.

    Returns an enriched prompt with transcribed text, or the original prompt
    if no audio attachments or transcription fails.
    """
    if not attachments:
        return prompt

    audio_paths = []
    for att in attachments:
        ext = Path(att).suffix.lstrip(".").lower()
        if ext in _AUDIO_EXTENSIONS:
            audio_paths.append(att)

    if not audio_paths:
        return prompt

    try:
        from .skills.whisper.transcribe import transcribe_audio
    except ImportError:
        logger.debug("faster-whisper not available, skipping pre-transcription")
        return prompt

    transcribed_parts = []
    for audio_path in audio_paths:
        try:
            result = transcribe_audio(audio_path)
            if result.get("status") == "ok" and result.get("text", "").strip():
                text = result["text"].strip()
                transcribed_parts.append(text)
                logger.debug(
                    "Pre-transcribed %s: %s",
                    Path(audio_path).name,
                    text[:100] + ("..." if len(text) > 100 else ""),
                )
            else:
                error = result.get("error", "unknown error")
                logger.debug("Pre-transcription failed for %s: %s", audio_path, error)
        except Exception:
            logger.debug("Pre-transcription error for %s", audio_path, exc_info=True)

    if not transcribed_parts:
        return prompt

    transcribed_text = " ".join(transcribed_parts)
    filenames = ", ".join(Path(p).name for p in audio_paths)
    return f"Transcribed voice message: {transcribed_text}\n\n(Original audio: {filenames})"


def parse_api_error(text: str) -> dict | None:
    """
    Parse API error string into structured data.

    Returns dict with status_code, message, request_id on match, or None.
    """
    match = API_ERROR_PATTERN.search(text)
    if not match:
        return None
    status_code = int(match.group(1))
    try:
        payload = json.loads(match.group(2))
        return {
            "status_code": status_code,
            "message": payload.get("error", {}).get("message", "Unknown error"),
            "request_id": payload.get("request_id"),
        }
    except json.JSONDecodeError:
        return {"status_code": status_code, "message": "Unknown error", "request_id": None}


def is_transient_api_error(text: str) -> bool:
    """Check if the error text represents a transient API error worth retrying."""
    parsed = parse_api_error(text)
    if not parsed:
        return False
    return parsed["status_code"] in TRANSIENT_STATUS_CODES or parsed["status_code"] == 429


def get_user_temp_dir(config: Config, user_id: str) -> Path:
    """Get the per-user temp directory path."""
    return config.temp_dir / user_id


# Credential-related env var patterns to strip from subprocess environments
_CREDENTIAL_ENV_PATTERNS = frozenset({
    "PASSWORD", "SECRET", "TOKEN", "API_KEY",
    "APP_PASSWORD", "NC_PASS", "PRIVATE_KEY",
})


def build_clean_env(config: Config) -> dict[str, str]:
    """Build base environment for Claude subprocess.

    In permissive mode, inherits full os.environ.
    In restricted mode, returns a minimal env (PATH, HOME, PYTHONUNBUFFERED)
    plus any configured passthrough vars.
    """
    if config.security.mode == "permissive":
        return dict(os.environ)
    # Ensure the active Python venv bin dir is on PATH so skills can run
    # as `python -m istota.skills.*` inside the sandbox. Use sys.prefix
    # (not sys.executable) to get the venv root — sys.executable resolves
    # through symlinks to the system python binary.
    venv_bin = str(Path(sys.prefix).resolve() / "bin")
    base_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    if venv_bin not in base_path.split(os.pathsep):
        base_path = f"{venv_bin}{os.pathsep}{base_path}"
    env = {
        "PATH": base_path,
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONUNBUFFERED": "1",
    }
    for key in config.security.passthrough_env_vars:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    # Pass through Claude CLI auth token if present (set via EnvironmentFile)
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


def build_stripped_env() -> dict[str, str]:
    """Build os.environ minus credential vars. For heartbeat/cron commands."""
    return {
        k: v for k, v in os.environ.items()
        if not any(p in k.upper() for p in _CREDENTIAL_ENV_PATTERNS)
    }


def build_allowed_tools(is_admin: bool, skill_names: list[str]) -> list[str]:
    """Build --allowedTools list for restricted security mode.

    Permits all Bash commands — the security boundary is the clean env
    (credential stripping), not command restriction. The tool surface is
    effectively unbounded: skill CLIs, user scripts, cron commands, curl
    for CalDAV/Nextcloud, rclone, etc.
    """
    return ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]


def build_bwrap_cmd(
    cmd: list[str],
    config: Config,
    task: db.Task,
    is_admin: bool,
    user_resources: list[db.UserResource],
    user_temp_dir: Path,
) -> list[str]:
    """Wrap a command in bubblewrap for per-user filesystem isolation.

    Returns the original cmd unchanged if sandbox is not available
    (non-Linux, bwrap not installed).
    """
    import shutil
    import sys

    if sys.platform != "linux":
        logger.debug("Sandbox skipped: not Linux (platform=%s)", sys.platform)
        return cmd

    if shutil.which("bwrap") is None:
        logger.warning("Sandbox skipped: bwrap binary not found in PATH")
        return cmd

    args: list[str] = ["bwrap"]

    def _ro_bind(src: Path, dest: Path | None = None) -> None:
        original = str(src)
        src = src.resolve()
        if not src.exists():
            return
        d = str(dest.resolve()) if dest else original
        args.extend(["--ro-bind", str(src), d])

    def _bind(src: Path, dest: Path | None = None) -> None:
        original = str(src)
        src = src.resolve()
        if not src.exists():
            return
        d = str(dest.resolve()) if dest else original
        args.extend(["--bind", str(src), d])

    def _tmpfs(path: Path) -> None:
        args.extend(["--tmpfs", str(path.resolve())])

    # --- System (RO) ---
    _ro_bind(Path("/usr"))
    # Merged-usr compatibility: /bin, /lib, /sbin, /lib64 are symlinks to /usr/*
    # on Debian 13+. Create symlinks inside sandbox so both paths work.
    for compat in ["/bin", "/lib", "/lib64", "/sbin"]:
        p = Path(compat)
        if p.is_symlink():
            args.extend(["--symlink", str(p.readlink()), compat])
        elif p.exists():
            _ro_bind(p)

    # Selective /etc binds — only what's needed for DNS, TLS, user lookup, timezone
    etc_files = [
        "/etc/ssl", "/etc/ca-certificates", "/etc/resolv.conf",
        "/etc/hosts", "/etc/nsswitch.conf", "/etc/ld.so.cache",
        "/etc/localtime", "/etc/passwd", "/etc/group",
    ]
    for ef in etc_files:
        _ro_bind(Path(ef))

    # --- Namespaces ---
    args.extend(["--unshare-pid", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"])

    # --- Python venv + source tree (RO) ---
    # Resolve istota_home from the source tree (src/istota/ -> parent -> parent)
    istota_src = Path(__file__).resolve().parent.parent  # src/
    istota_home = istota_src.parent  # project root or install root
    venv_path = istota_home / ".venv"
    if not venv_path.exists():
        # Deployed layout: {istota_home}/src/.venv
        venv_path = istota_src / ".venv"
    _ro_bind(venv_path)
    _ro_bind(istota_src)

    # Mask other users' config files
    users_config_dir = istota_src / "config" / "users"
    if users_config_dir.exists():
        _tmpfs(users_config_dir)

    # --- Claude CLI (selective .local binds) ---
    home = Path(os.environ.get("HOME", "/tmp"))
    # bin/ and share/claude/ are RO (binary + versions)
    _ro_bind(home / ".local" / "bin")
    _ro_bind(home / ".local" / "share" / "claude")
    # state/claude/ is RW (lock files created at runtime)
    _bind(home / ".local" / "state" / "claude")

    # --- Claude auth (tmpfs base + RW credentials for OAuth refresh) ---
    claude_dir = home / ".claude"
    if claude_dir.exists():
        _tmpfs(claude_dir)
        creds = claude_dir / ".credentials.json"
        if creds.exists():
            _bind(creds)  # RW: OAuth token refresh writes here
        settings = claude_dir / "settings.json"
        if settings.exists():
            _ro_bind(settings)
        # Persist session JSONL logs and debug output across sandbox exits
        for subdir in ["projects", "debug", "todos"]:
            d = claude_dir / subdir
            if d.exists():
                _bind(d)

    # --- User workspace (RW) ---
    _bind(user_temp_dir.resolve())

    # --- Nextcloud mounts ---
    mount = config.nextcloud_mount_path
    if mount:
        mount = mount.resolve()
        if is_admin:
            # Admin: full mount RW
            _bind(mount)
        else:
            # Non-admin: only their own user dir RW
            user_dir = mount / "Users" / task.user_id
            if user_dir.exists():
                _bind(user_dir)
            # Active channel dir RW (agent writes CHANNEL.md)
            if task.conversation_token:
                channel_dir = mount / "Channels" / task.conversation_token
                if channel_dir.exists():
                    _bind(channel_dir)

    # --- DB access for admin ---
    if is_admin:
        db_path = config.db_path.resolve()
        if db_path.exists():
            if config.security.sandbox_admin_db_write:
                _bind(db_path)
            else:
                _ro_bind(db_path)
            # SQLite WAL/SHM files
            for suffix in ["-wal", "-shm"]:
                wal = db_path.parent / (db_path.name + suffix)
                if wal.exists():
                    if config.security.sandbox_admin_db_write:
                        _bind(wal)
                    else:
                        _ro_bind(wal)

    # --- Huggingface model cache (RO) ---
    hf_cache = home / ".cache" / "huggingface"
    if hf_cache.exists():
        _ro_bind(hf_cache)

    # --- Developer repos (RW) ---
    if is_admin and config.developer.enabled and config.developer.repos_dir:
        repos = Path(config.developer.repos_dir)
        if repos.exists():
            _bind(repos)

    # --- Per-resource mounts ---
    if mount and not is_admin:
        for r in user_resources:
            if not r.resource_path:
                continue
            rpath = (mount / r.resource_path.lstrip("/")).resolve()
            if not rpath.exists():
                continue
            # Skip if already covered by user dir bind
            user_dir = mount / "Users" / task.user_id
            try:
                rpath.relative_to(user_dir.resolve())
                continue  # Already inside user dir
            except ValueError:
                pass
            if r.permissions == "readwrite":
                _bind(rpath)
            else:
                _ro_bind(rpath)

    # --- Static site directory (RW) ---
    if config.site.enabled and config.site.base_path:
        site_dir = Path(config.site.base_path)
        if site_dir.exists():
            _bind(site_dir)

    # --- Lifecycle ---
    args.extend(["--die-with-parent", "--chdir", str(user_temp_dir.resolve())])
    args.append("--")
    args.extend(cmd)
    return args


def _allowlist_pattern_to_case(pattern: str) -> str:
    """Convert an allowlist pattern like 'GET /api/v4/projects/*' to a shell case glob.

    Each literal segment is quoted, wildcards become unquoted * for shell globbing.
    Examples:
        'GET /api/v4/projects/*' → '"GET /api/v4/projects/"*'
        'POST /api/v4/projects/*/merge_requests' → '"POST /api/v4/projects/"*"/merge_requests"'
    """
    parts = pattern.split("*")
    result = "*".join(f'"{p}"' for p in parts if p)
    if pattern.endswith("*"):
        result += "*"
    return result


def _ensure_reply_parent_in_history(
    task: db.Task,
    history: list[db.ConversationMessage],
    config: Config,
    conn: "db.sqlite3.Connection | None" = None,
) -> tuple[list[db.ConversationMessage], db.ConversationMessage | None]:
    """
    Ensure the replied-to message's task is included in conversation history.

    If the user replied to a specific Talk message, look up the task associated
    with that message and prepend it to history if not already present.
    Falls back to injecting reply_to_content as a synthetic message if the
    parent task isn't found in the DB.

    Returns (updated_history, reply_parent_msg) where reply_parent_msg is the
    message that must survive triage (or None if not applicable).
    """
    if not task.reply_to_talk_id or not task.conversation_token:
        return history, None

    history_ids = {msg.id for msg in history}

    def _lookup(c: db.sqlite3.Connection) -> db.Task | None:
        return db.get_reply_parent_task(c, task.conversation_token, task.reply_to_talk_id)

    parent_task = None
    if conn is not None:
        parent_task = _lookup(conn)
    else:
        with db.get_db(config.db_path) as temp_conn:
            parent_task = _lookup(temp_conn)

    if parent_task:
        parent_msg = db.ConversationMessage(
            id=parent_task.id,
            prompt=parent_task.prompt,
            result=parent_task.result or "",
            created_at=parent_task.created_at or "",
            actions_taken=parent_task.actions_taken,
            source_type=parent_task.source_type,
            user_id=parent_task.user_id,
        )
        if parent_task.id not in history_ids:
            logger.info(
                "Force-including reply parent task %d in context for task %d",
                parent_task.id,
                task.id,
            )
            return [parent_msg] + history, parent_msg
        else:
            logger.debug(
                "Reply parent task %d already in history for task %d",
                parent_task.id,
                task.id,
            )
            return history, parent_msg

    if task.reply_to_content:
        # Parent task not in DB — inject reply_to_content as synthetic context
        synthetic_msg = db.ConversationMessage(
            id=-1,  # Sentinel ID, won't collide with real task IDs
            prompt="(replied-to message)",
            result=task.reply_to_content,
            created_at="",
        )
        logger.info(
            "Injecting reply_to_content as synthetic context for task %d (parent talk msg %d not in DB)",
            task.id,
            task.reply_to_talk_id,
        )
        return [synthetic_msg] + history, synthetic_msg

    return history, None


def _apply_bot_name(content: str, config: Config) -> str:
    """Replace {BOT_NAME} placeholder with config.bot_name in loaded content."""
    return content.replace("{BOT_NAME}", config.bot_name).replace("{BOT_DIR}", config.bot_dir_name)


def load_emissaries(config: Config) -> str | None:
    """Load the emissaries constitutional document (global only, not user-overridable)."""
    if not config.emissaries_enabled:
        return None
    config_dir = config.skills_dir.parent
    emissaries_path = config_dir / "emissaries.md"
    if emissaries_path.exists():
        return emissaries_path.read_text().strip()
    return None


def load_persona(config: Config, user_id: str | None = None) -> str | None:
    """Load persona file, checking user workspace first, then global.

    User workspace PERSONA.md (in their Nextcloud config dir) takes precedence
    over the global config/istota.md file.
    """
    # Try user workspace persona first
    if user_id and config.use_mount:
        from .storage import _get_mount_path
        user_persona_path = _get_mount_path(config, get_user_persona_path(user_id, config.bot_dir_name))
        if user_persona_path.exists():
            content = user_persona_path.read_text().strip()
            if content:
                return _apply_bot_name(content, config)

    # Fall back to global persona
    config_dir = config.skills_dir.parent
    persona_path = config_dir / "persona.md"
    if persona_path.exists():
        return _apply_bot_name(persona_path.read_text().strip(), config)
    return None


def load_channel_guidelines(config: Config, source_type: str) -> str | None:
    """Load channel-specific guidelines if they exist, substituting {BOT_NAME} placeholders."""
    config_dir = config.skills_dir.parent
    guidelines_path = config_dir / "guidelines" / f"{source_type}.md"
    if guidelines_path.exists():
        return _apply_bot_name(guidelines_path.read_text().strip(), config)
    return None


def build_prompt(
    task: db.Task,
    user_resources: list[db.UserResource],
    config: Config,
    skills_doc: str | None = None,
    conversation_context: str | None = None,
    user_memory: str | None = None,
    discovered_calendars: list[tuple[str, str, bool]] | None = None,
    user_email_addresses: list[str] | None = None,
    dated_memories: str | None = None,
    channel_memory: str | None = None,
    skills_changelog: str | None = None,
    is_admin: bool = True,
    emissaries: str | None = None,
    source_type: str | None = None,
    output_target: str | None = None,
) -> str:
    """Build the full prompt for Claude Code execution."""
    # Group resources by type
    resources_by_type: dict[str, list[db.UserResource]] = {}
    for r in user_resources:
        resources_by_type.setdefault(r.resource_type, []).append(r)

    resource_sections = []

    # Use discovered calendars if available, otherwise fall back to user_resources
    if discovered_calendars:
        cal_list = "\n".join(
            f"  - {name}: {url} ({'read/write' if writable else 'read-only'})"
            for name, url, writable in discovered_calendars
        )
        resource_sections.append(f"Calendars (shared by {task.user_id}):\n{cal_list}")
    elif "calendar" in resources_by_type:
        calendars = resources_by_type["calendar"]
        cal_list = "\n".join(
            f"  - {r.display_name or r.resource_path}: {r.resource_path} ({r.permissions})"
            for r in calendars
        )
        resource_sections.append(f"Calendars:\n{cal_list}")

    if "folder" in resources_by_type:
        folders = resources_by_type["folder"]
        folder_list = "\n".join(
            f"  - {r.display_name or r.resource_path}: {r.resource_path} ({r.permissions})"
            for r in folders
        )
        resource_sections.append(f"Nextcloud Folders:\n{folder_list}")

    if "todo_file" in resources_by_type:
        todos = resources_by_type["todo_file"]
        todo_list = "\n".join(
            f"  - {r.display_name or r.resource_path}: {r.resource_path} ({r.permissions})"
            for r in todos
        )
        resource_sections.append(f"TODO Files:\n{todo_list}")

    if "email_folder" in resources_by_type:
        email_folders = resources_by_type["email_folder"]
        email_list = "\n".join(
            f"  - {r.display_name or r.resource_path}: {r.resource_path}"
            for r in email_folders
        )
        resource_sections.append(f"Email Folders:\n{email_list}")

    if "notes_file" in resources_by_type:
        notes = resources_by_type["notes_file"]
        notes_list = "\n".join(
            f"  - {r.display_name or r.resource_path}: {r.resource_path} ({r.permissions})"
            for r in notes
        )
        resource_sections.append(f"Notes Files (read for reminders/agenda items):\n{notes_list}")

    if "reminders_file" in resources_by_type and task.source_type != "briefing":
        reminders = resources_by_type["reminders_file"]
        reminders_list = "\n".join(
            f"  - {r.display_name or r.resource_path}: {r.resource_path} ({r.permissions})"
            for r in reminders
        )
        resource_sections.append(f"Reminders Files:\n{reminders_list}")

    if config.site.enabled:
        user_config = config.get_user(task.user_id)
        if user_config and user_config.site_enabled:
            site_url = f"https://{config.site.hostname}/~{task.user_id}"
            site_path = config.nextcloud_mount_path / "Users" / task.user_id / config.bot_dir_name / "html"
            resource_sections.append(
                f"Website:\n  - URL: {site_url}\n  - Path: {site_path} (readwrite)"
            )

    resources_text = "\n\n".join(resource_sections) if resource_sections else "No specific resources configured."

    # Load emissaries (constitutional principles, global only)
    emissaries_section = ""
    if emissaries:
        emissaries_section = f"\n\n{emissaries}\n"

    # Load persona (always included if exists)
    persona = load_persona(config, user_id=task.user_id)
    persona_section = ""
    if persona:
        persona_section = f"\n\n{persona}\n"

    # Load channel-specific guidelines
    channel_guidelines = load_channel_guidelines(config, task.source_type)
    channel_section = ""
    if channel_guidelines:
        channel_section = f"\n\n## Response format ({task.source_type})\n\n{channel_guidelines}\n"

    # Build attachments section if present
    attachments_text = ""
    if task.attachments:
        att_list = "\n".join(f"  - {att}" for att in task.attachments)
        # Check if paths are local (absolute) or remote (Nextcloud)
        if any(att.startswith("/") for att in task.attachments):
            attachments_text = f"\n\nAttached files (local paths):\n{att_list}"
        else:
            attachments_text = f"\n\nAttached files (in Nextcloud, access via rclone):\n{att_list}"

    # Build user memory section
    memory_section = ""
    if user_memory:
        memory_section = f"""
## User memory

The following information has been remembered about this user:

{user_memory}

"""

    # Build channel memory section
    channel_memory_section = ""
    if channel_memory:
        channel_memory_section = f"""
## Channel memory

The following information has been remembered about this channel/room:

{channel_memory}

"""

    # Build dated memories section
    dated_memories_section = ""
    if dated_memories:
        dated_memories_section = f"""
## Recent context (from previous days)

{dated_memories}

"""

    # Build conversation context section
    context_section = ""
    if conversation_context:
        context_section = f"""
## Conversation context

The following are relevant previous messages from this conversation:

{conversation_context}

"""

    # Build file access tools section based on mount mode
    # Non-admin users get a scoped mount path restricted to their own directory
    if config.use_mount:
        if is_admin:
            mount_display = str(config.nextcloud_mount_path)
        else:
            mount_display = str(config.nextcloud_mount_path / "Users" / task.user_id)
        file_tools = f"""- Nextcloud files are mounted at '{mount_display}'
  - List: ls {mount_display}/path/
  - Read: cat {mount_display}/path/file.txt
  - Write: Use standard file operations (Python, bash, etc.)
  - All Nextcloud paths are accessible as local filesystem paths"""
    else:
        file_tools = f"""- rclone for Nextcloud files: remote name is '{config.rclone_remote}'
  - List: rclone ls {config.rclone_remote}:/path/
  - Copy from NC: rclone copy {config.rclone_remote}:/path/file.txt /tmp/
  - Copy to NC: rclone copy /tmp/file.txt {config.rclone_remote}:/path/"""

    # Browser tool line (only when enabled)
    browser_tool = ""
    if config.browser.enabled:
        browser_tool = "\n- Web browser for JS-rendered pages: python -m istota.skills.browse (see browse skill for details)"

    # Compute user's local time
    user_config = config.get_user(task.user_id)
    user_tz_str = user_config.timezone if user_config else "UTC"
    try:
        user_tz = ZoneInfo(user_tz_str)
    except Exception:
        user_tz = ZoneInfo("UTC")
        user_tz_str = "UTC"
    user_now = datetime.now(user_tz)
    user_time_str = user_now.strftime("%A, %B %-d, %Y at %-I:%M %p") + f" ({user_tz_str})"

    # Build admin-sensitive sections
    db_path_line = f"Database path: {config.db_path}" if is_admin else "Database path: (restricted)"

    db_tool_line = ""  # DB writes handled via deferred JSON files

    if is_admin:
        rules_section = f"""## Important rules

1. Only access resources that belong to user '{task.user_id}' as listed above.
2. For sensitive actions, ask for confirmation EXCEPT:
   - Emails to the user's own addresses ({', '.join(user_email_addresses) if user_email_addresses else 'none configured'}) do NOT need confirmation
   - Emails to external addresses DO need confirmation
   - Modifying calendars, deleting files, sharing externally need confirmation
3. To create subtasks, write a JSON file to $ISTOTA_DEFERRED_DIR/task_{task.id}_subtasks.json with format: [{{"prompt": "...", "conversation_token": "...", "priority": 5}}]. They will be queued after this task completes.
4. Do NOT write to the SQLite database directly (e.g. via sqlite3 CLI or Python sqlite3 module). The database is read-only in your environment. All database modifications are handled by the skill CLI commands (e.g. `python -m istota.skills.accounting`, `python -m istota.skills.memory_search`) or via deferred JSON files in $ISTOTA_DEFERRED_DIR.
5. After creating or writing a file, verify it exists on the filesystem (e.g. check with ls or Read). Do not assume a write succeeded.
6. Never edit or create files in your own source directory.
7. Respond directly with your answer — your final output will be sent to the user. While you're working (between tool calls), keep commentary minimal — brief status notes are fine, but save substantive analysis and detailed results for your final response. Intermediate text may be shown to the user as progress updates.
8. Your execution JSONL logs (full conversation traces including subagent output) are stored under ~/.claude/projects/. If a user reports missing or truncated output from a previous task, search these logs for the full assistant message content."""
    else:
        scoped_path = str(config.nextcloud_mount_path / "Users" / task.user_id) if config.use_mount else f"{config.rclone_remote}:/Users/{task.user_id}"
        rules_section = f"""## Important rules

1. You can ONLY access files under {scoped_path}. You do NOT have access to the task database or other users' data.
2. For sensitive actions, ask for confirmation EXCEPT:
   - Emails to the user's own addresses ({', '.join(user_email_addresses) if user_email_addresses else 'none configured'}) do NOT need confirmation
   - Emails to external addresses DO need confirmation
   - Modifying calendars, deleting files, sharing externally need confirmation
3. Do NOT write to the SQLite database directly. All database modifications are handled by the skill CLI commands or the bot's scheduler.
4. After creating or writing a file, verify it exists on the filesystem (e.g. check with ls or Read). Do not assume a write succeeded.
5. Never edit or create files in your own source directory.
6. Respond directly with your answer — your final output will be sent to the user. While you're working (between tool calls), keep commentary minimal — brief status notes are fine, but save substantive analysis and detailed results for your final response. Intermediate text may be shown to the user as progress updates."""

    group_chat_line = ""
    if task.is_group_chat:
        group_chat_line = f"\nThis is a group conversation. You were @mentioned by '{task.user_id}'. Other participants' messages are visible in conversation context below."

    prompt = f"""You are {config.bot_name}, a helpful assistant bot. You are responding to a request from user '{task.user_id}'.

Current time: {user_time_str}
Current task ID: {task.id}
Conversation token: {task.conversation_token or 'none'}{group_chat_line}
Source: {source_type or task.source_type or 'unknown'}
Output target: {output_target or 'text'}
{db_path_line}
{emissaries_section}{persona_section}
## User's accessible resources

{resources_text}
{memory_section}{channel_memory_section}{dated_memories_section}## Available tools

You have access to:
{file_tools}{browser_tool}
- caldav via curl or the caldav Python library for calendar operations{db_tool_line}
- Email sending is handled by the bot internally. When the output target is "email", use the email output tool: `python -m istota.skills.email output --subject "..." --body "..." [--html]`. Use `--body-file` for long content. Do NOT use this tool when the output target is "talk" — just respond with text. See the email skill for details.

{rules_section}
{context_section}
## User's request

{task.prompt}{attachments_text}
{channel_section}"""

    if skills_changelog:
        prompt += f"\n\n## What's New in Skills\n\n{skills_changelog}"

    if skills_doc:
        prompt += f"\n\n{skills_doc}"

    return prompt


def execute_task(
    task: db.Task,
    config: Config,
    user_resources: list[db.UserResource],
    dry_run: bool = False,
    use_context: bool = True,
    conn: "db.sqlite3.Connection | None" = None,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """
    Execute a task using Claude Code.

    Args:
        on_progress: Optional callback for progress updates. Called with
            human-readable descriptions of tool uses and intermediate text.

    Returns (success, result_or_error).
    """
    # Ensure per-user temp directory exists
    user_temp_dir = get_user_temp_dir(config, task.user_id)
    user_temp_dir.mkdir(parents=True, exist_ok=True)

    # Build resources: merge config-defined resources with dynamic DB resources
    user_config = config.get_user(task.user_id)
    all_resources = list(user_resources)  # start with passed resources (e.g. shared_file from DB)
    if user_config:
        for rc in user_config.resources:
            all_resources.append(db.UserResource(
                id=0, user_id=task.user_id,
                resource_type=rc.type, resource_path=rc.path,
                display_name=rc.name or None, permissions=rc.permissions,
            ))
    user_resources = all_resources

    # Pre-transcribe audio attachments so skill selection sees real text
    enriched_prompt = _pre_transcribe_attachments(task.attachments, task.prompt)
    if enriched_prompt != task.prompt:
        logger.info("Pre-transcribed audio for task %s, enriched prompt for skill selection", task.id)
        task.prompt = enriched_prompt

    # Select and load relevant skills
    from .skills_loader import (
        load_skill_index, select_skills, load_skills,
        compute_skills_fingerprint, load_skills_changelog,
    )

    is_admin = config.is_admin(task.user_id)

    _bundled_dir = config.bundled_skills_dir
    skill_index = load_skill_index(config.skills_dir, bundled_dir=_bundled_dir)
    user_resource_types = {r.resource_type for r in user_resources}
    selected_skills = select_skills(
        prompt=task.prompt,
        source_type=task.source_type,
        user_resource_types=user_resource_types,
        skill_index=skill_index,
        is_admin=is_admin,
        attachments=task.attachments,
    )
    skills_doc = load_skills(
        config.skills_dir, selected_skills, config.bot_name, config.bot_dir_name,
        skill_index=skill_index, bundled_dir=_bundled_dir,
    )
    if skills_doc:
        # Resolve per-user scripts directory
        scripts_nc_path = get_user_scripts_path(task.user_id)
        if config.use_mount:
            scripts_dir = str(config.nextcloud_mount_path / scripts_nc_path.lstrip("/"))
        else:
            scripts_dir = f"{config.rclone_remote}:{scripts_nc_path}"
        skills_doc = skills_doc.replace("{scripts_dir}", scripts_dir)
    if selected_skills:
        logger.debug("Selected skills: %s", ", ".join(selected_skills))

    # Skills changelog: detect changes for interactive tasks
    skills_changelog = None
    _is_interactive = task.source_type in ("talk", "email")
    current_fingerprint = compute_skills_fingerprint(config.skills_dir, bundled_dir=_bundled_dir)
    if _is_interactive:
        try:
            def _check_fingerprint(c):
                return db.get_user_skills_fingerprint(c, task.user_id)
            if conn is not None:
                stored_fingerprint = _check_fingerprint(conn)
            else:
                with db.get_db(config.db_path) as fp_conn:
                    stored_fingerprint = _check_fingerprint(fp_conn)
            if stored_fingerprint != current_fingerprint:
                skills_changelog = load_skills_changelog(config.skills_dir, bundled_dir=_bundled_dir)
                if skills_changelog:
                    logger.info(
                        "Skills changed for user %s (%s -> %s), including changelog",
                        task.user_id, stored_fingerprint or "none", current_fingerprint,
                    )
        except Exception:
            pass  # Graceful degradation

    # Get conversation context if enabled
    conversation_context = None
    context_skip_reason = None
    if not use_context:
        context_skip_reason = "use_context=False"
    elif not config.conversation.enabled:
        context_skip_reason = "conversation.enabled=False in config"
    elif task.source_type not in ("talk", "email"):
        context_skip_reason = f"source_type={task.source_type!r} (not talk/email)"
    elif not task.conversation_token:
        context_skip_reason = "no conversation_token"

    if context_skip_reason:
        logger.info("Skipping context lookup: %s", context_skip_reason)
    else:
        # Exclude background task types from conversation context so scheduled
        # jobs, briefings, and heartbeat results don't pollute interactive history
        _exclude_types = ["scheduled", "briefing"]

        # Use provided connection or open a new one
        if conn is not None:
            history = db.get_conversation_history(
                conn,
                task.conversation_token,
                exclude_task_id=task.id,
                limit=config.conversation.lookback_count,
                exclude_source_types=_exclude_types,
            )
        else:
            with db.get_db(config.db_path) as temp_conn:
                history = db.get_conversation_history(
                    temp_conn,
                    task.conversation_token,
                    exclude_task_id=task.id,
                    limit=config.conversation.lookback_count,
                    exclude_source_types=_exclude_types,
                )

        # Always fetch the immediately previous task (unfiltered) so that
        # scheduled/briefing messages aren't orphaned when the user responds
        # to them in the same channel.
        if conn is not None:
            prev_task = db.get_previous_task(
                conn, task.conversation_token, exclude_task_id=task.id
            )
        else:
            with db.get_db(config.db_path) as temp_conn:
                prev_task = db.get_previous_task(
                    temp_conn, task.conversation_token, exclude_task_id=task.id
                )

        if prev_task:
            history_ids = {msg.id for msg in history}
            if prev_task.id not in history_ids:
                history.append(prev_task)
                history.sort(key=lambda m: (m.created_at, m.id))
                logger.info(
                    "Included previous task %d (excluded source_type) in context for task %d",
                    prev_task.id,
                    task.id,
                )

        logger.debug(
            "Context lookup: token=%s, history_count=%d",
            task.conversation_token,
            len(history),
        )

        if history:
            # Force-include reply parent if this task is a reply to a specific message
            reply_parent_msg = None
            if task.reply_to_talk_id and task.conversation_token:
                history, reply_parent_msg = _ensure_reply_parent_in_history(
                    task, history, config, conn if conn is not None else None
                )

            relevant = select_relevant_context(task.prompt, history, config)

            # Ensure reply parent survives triage — force it in if triage dropped it
            if reply_parent_msg:
                relevant_ids = {msg.id for msg in relevant}
                if reply_parent_msg.id not in relevant_ids:
                    relevant = [reply_parent_msg] + relevant
                    logger.info(
                        "Re-added reply parent (task %d) after triage dropped it for task %d",
                        reply_parent_msg.id,
                        task.id,
                    )

            if relevant:
                conversation_context = format_context_for_prompt(
                    relevant, truncation=config.conversation.context_truncation
                )
                logger.info(
                    "Loaded %d context messages (%d chars) for task %d",
                    len(relevant),
                    len(conversation_context),
                    task.id,
                )
            else:
                logger.info("No relevant context selected from %d messages", len(history))
        else:
            # No conversation history — but if this is a reply, still inject the parent content
            if task.reply_to_talk_id and task.reply_to_content:
                conversation_context = f"(In reply to: {task.reply_to_content})"
                logger.info("Using inline reply context for task %d (no history)", task.id)
            else:
                logger.info("No conversation history found for token %s", task.conversation_token)

    # Load user memory (auto-create directories if missing)
    # Skip personal memory for briefings — they should use only their
    # pre-fetched components (markets, calendar, news) to avoid leaking
    # private context into newsletter-style output.
    user_memory = None
    if task.source_type != "briefing":
        try:
            user_memory = read_user_memory_v2(config, task.user_id)
            if user_memory is None:
                # Try to create directories (memory file may just not exist yet)
                ensure_user_directories_v2(config, task.user_id)
        except Exception:
            # Graceful degradation if storage unavailable
            pass

    # Load channel memory if in a conversation
    channel_memory = None
    if task.conversation_token:
        try:
            channel_memory = read_channel_memory(config, task.conversation_token)
            if channel_memory is None:
                ensure_channel_directories(config, task.conversation_token)
        except Exception:
            pass  # Graceful degradation

    # Auto-discover calendars for user
    discovered_calendars = None
    if config.caldav_url and config.caldav_username and config.caldav_password:
        try:
            caldav_client = get_caldav_client(
                config.caldav_url,
                config.caldav_username,
                config.caldav_password,
            )
            discovered_calendars = get_calendars_for_user(caldav_client, task.user_id)
        except Exception:
            # Graceful degradation if CalDAV unavailable
            pass

    # Dated memories are stored for search/reference, not auto-loaded into prompts.
    # They are available at /Users/{user_id}/memories/ for Claude to read if needed.
    dated_memories = None
    user_config = config.get_user(task.user_id)

    # Get user's email addresses for confirmation policy
    user_email_addresses = []
    if user_config:
        user_email_addresses = user_config.email_addresses

    # Load emissaries (constitutional principles)
    emissaries = load_emissaries(config)

    # Compute effective output target (same logic as scheduler.process_one_task)
    effective_output_target = task.output_target
    if not effective_output_target:
        if task.source_type in ("talk", "briefing"):
            effective_output_target = "talk"
        elif task.source_type == "email":
            effective_output_target = "email"
        elif task.source_type == "istota_file":
            effective_output_target = "istota_file"

    # Build prompt
    prompt = build_prompt(
        task, user_resources, config, skills_doc, conversation_context, user_memory,
        discovered_calendars, user_email_addresses, dated_memories, channel_memory,
        skills_changelog, is_admin, emissaries,
        source_type=task.source_type,
        output_target=effective_output_target,
    )

    # Log prompt size breakdown
    context_chars = len(conversation_context) if conversation_context else 0
    memory_chars = len(user_memory or "") + len(dated_memories or "") + len(channel_memory or "")
    skills_chars = len(skills_doc or "")
    prompt_chars = len(prompt)
    logger.info(
        "Prompt for task %d: %d chars total (context: %d, memory: %d, skills: %d, other: %d)",
        task.id, prompt_chars, context_chars, memory_chars, skills_chars,
        prompt_chars - context_chars - memory_chars - skills_chars,
    )

    if dry_run:
        return True, f"[DRY RUN] Would execute with prompt:\n\n{prompt}", None

    # Write prompt to temp file for debugging
    prompt_file = user_temp_dir / f"task_{task.id}_prompt.txt"
    prompt_file.write_text(prompt)

    # Result file path
    result_file = user_temp_dir / f"task_{task.id}_result.txt"

    # Clean up any previous result file
    if result_file.exists():
        result_file.unlink()

    try:
        # Build command
        use_streaming = on_progress is not None
        if config.security.mode == "restricted":
            allowed = build_allowed_tools(is_admin, selected_skills)
            cmd = ["claude", "-p", "--allowedTools"] + allowed
        else:
            cmd = ["claude", "-p", "--dangerously-skip-permissions"]
        if config.model:
            cmd += ["--model", config.model]
        if use_streaming:
            cmd += ["--output-format", "stream-json", "--verbose"]

        env = build_clean_env(config)
        env.update({
            "ISTOTA_TASK_ID": str(task.id),
            "ISTOTA_USER_ID": task.user_id,
            # CalDAV credentials for calendar access
            "CALDAV_URL": config.caldav_url or "",
            "CALDAV_USERNAME": config.caldav_username or "",
            "CALDAV_PASSWORD": config.caldav_password or "",
            # Nextcloud OCS API credentials
            "NC_URL": config.nextcloud.url or "",
            "NC_USER": config.nextcloud.username or "",
            "NC_PASS": config.nextcloud.app_password or "",
            "ISTOTA_CONVERSATION_TOKEN": task.conversation_token or "",
            "ISTOTA_DEFERRED_DIR": str(user_temp_dir),
        })

        # Admin users get full DB and mount access; non-admin users get scoped paths
        if is_admin:
            env["ISTOTA_DB_PATH"] = str(config.db_path)
            env["NEXTCLOUD_MOUNT_PATH"] = str(config.nextcloud_mount_path) if config.nextcloud_mount_path else ""
        else:
            if config.nextcloud_mount_path:
                env["NEXTCLOUD_MOUNT_PATH"] = str(config.nextcloud_mount_path / "Users" / task.user_id)
            else:
                env["NEXTCLOUD_MOUNT_PATH"] = ""

        # Browser container credentials
        if config.browser.enabled:
            env["BROWSER_API_URL"] = config.browser.api_url
            env["BROWSER_VNC_URL"] = config.browser.vnc_url

        # Email credentials for direct sending from Claude Code
        if config.email.enabled:
            env["SMTP_HOST"] = config.email.smtp_host
            env["SMTP_PORT"] = str(config.email.smtp_port)
            env["SMTP_USER"] = config.email.effective_smtp_user
            env["SMTP_PASSWORD"] = config.email.effective_smtp_password
            env["SMTP_FROM"] = config.email.bot_email
            env["IMAP_HOST"] = config.email.imap_host
            env["IMAP_PORT"] = str(config.email.imap_port)
            env["IMAP_USER"] = config.email.imap_user
            env["IMAP_PASSWORD"] = config.email.imap_password

        # Accounting ledger paths (from user resources)
        ledger_resources = [r for r in user_resources if r.resource_type == "ledger"]
        if ledger_resources and config.nextcloud_mount_path:
            # Pass all ledgers as JSON for multi-ledger support
            ledgers = []
            for r in ledger_resources:
                ledgers.append({
                    "name": r.display_name or "default",
                    "path": str(config.nextcloud_mount_path / r.resource_path.lstrip("/")),
                })
            env["LEDGER_PATHS"] = json.dumps(ledgers)
            # Also set LEDGER_PATH to first ledger for simple single-ledger usage
            env["LEDGER_PATH"] = ledgers[0]["path"]

        # Invoicing config path (always in user's bot config folder)
        if config.nextcloud_mount_path and task:
            from .storage import get_user_invoicing_path
            invoicing_path = config.nextcloud_mount_path / get_user_invoicing_path(task.user_id, config.bot_dir_name).lstrip("/")
            if not invoicing_path.exists():
                from .storage import INVOICING_TEMPLATE
                invoicing_path.parent.mkdir(parents=True, exist_ok=True)
                invoicing_path.write_text(
                    INVOICING_TEMPLATE.format(user_id=task.user_id)
                )
            env["INVOICING_CONFIG"] = str(invoicing_path)

        # Accounting config path (from user resource or default bot config location)
        if config.nextcloud_mount_path and task:
            accounting_resources = [r for r in user_resources if r.resource_type == "accounting"]
            if accounting_resources:
                accounting_path = config.nextcloud_mount_path / accounting_resources[0].resource_path.lstrip("/")
            else:
                from .storage import get_user_accounting_path
                accounting_path = config.nextcloud_mount_path / get_user_accounting_path(task.user_id, config.bot_dir_name).lstrip("/")
            if not accounting_path.exists():
                from .storage import ACCOUNTING_TEMPLATE
                accounting_path.parent.mkdir(parents=True, exist_ok=True)
                accounting_path.write_text(ACCOUNTING_TEMPLATE)
            env["ACCOUNTING_CONFIG"] = str(accounting_path)

            # Also pass user ID and DB path for deduplication tracking
            env["ISTOTA_USER_ID"] = task.user_id
            if is_admin:
                env["ISTOTA_DB_PATH"] = str(config.db_path)

        # Karakeep bookmarks (per-user API credentials from resource config)
        if user_config:
            karakeep_resources = [
                rc for rc in user_config.resources
                if rc.type == "karakeep" and rc.base_url and rc.api_key
            ]
            if karakeep_resources:
                # Use the first karakeep resource entry
                env["KARAKEEP_BASE_URL"] = karakeep_resources[0].base_url
                env["KARAKEEP_API_KEY"] = karakeep_resources[0].api_key

        # Developer skill (git + GitLab/GitHub workflows)
        # Tokens are never exposed as env vars directly — instead we write helper
        # scripts that embed the credential, so it stays out of Claude's context.
        if config.developer.enabled and config.developer.repos_dir:
            env["DEVELOPER_REPOS_DIR"] = config.developer.repos_dir
            env["GITLAB_URL"] = config.developer.gitlab_url
            env["GITHUB_URL"] = config.developer.github_url
            if config.developer.gitlab_default_namespace:
                env["GITLAB_DEFAULT_NAMESPACE"] = config.developer.gitlab_default_namespace
            if config.developer.gitlab_reviewer_id:
                env["GITLAB_REVIEWER_ID"] = config.developer.gitlab_reviewer_id
            if config.developer.github_default_owner:
                env["GITHUB_DEFAULT_OWNER"] = config.developer.github_default_owner
            if config.developer.github_reviewer:
                env["GITHUB_REVIEWER"] = config.developer.github_reviewer
            if config.developer.author_credit:
                env["DEVELOPER_AUTHOR_CREDIT"] = config.developer.author_credit

            dev_bin = Path(user_temp_dir) / ".developer"
            dev_bin.mkdir(parents=True, exist_ok=True)
            git_config_index = 0

            if config.developer.gitlab_token:
                env["GITLAB_TOKEN"] = config.developer.gitlab_token

                # Git credential helper — git calls this automatically for HTTPS auth
                # Reads token from GITLAB_TOKEN env var (no secrets on disk)
                git_cred = dev_bin / "git-credential-helper"
                git_cred.write_text(
                    "#!/bin/sh\n"
                    '[ "$1" = "get" ] || exit 0\n'
                    f"echo username={config.developer.gitlab_username}\n"
                    'echo password=$GITLAB_TOKEN\n'
                )
                git_cred.chmod(0o700)
                gitlab_host = config.developer.gitlab_url.rstrip("/")
                env[f"GIT_CONFIG_KEY_{git_config_index}"] = f"credential.{gitlab_host}.helper"
                env[f"GIT_CONFIG_VALUE_{git_config_index}"] = str(git_cred)
                git_config_index += 1

                # GitLab API wrapper — usage: gitlab-api METHOD /api/v4/... [curl args]
                # Enforces an endpoint allowlist and strips query strings for matching.
                # Reads token from GITLAB_TOKEN env var (no secrets on disk)
                api_script = dev_bin / "gitlab-api"
                allowlist_cases = "\n".join(
                    f"  {_allowlist_pattern_to_case(p)}) ;;"
                    for p in config.developer.gitlab_api_allowlist
                )
                api_script.write_text(
                    "#!/bin/sh\n"
                    'METHOD="$1"; shift\n'
                    'ENDPOINT="$1"; shift\n'
                    'CLEAN="${ENDPOINT%%\\?*}"\n'
                    'case "$METHOD $CLEAN" in\n'
                    f"{allowlist_cases}\n"
                    '  *) printf \'{"error":"endpoint not allowed: %s %s"}\\n\' '
                    '"$METHOD" "$CLEAN" >&2; exit 1 ;;\n'
                    "esac\n"
                    f'curl -s --header "PRIVATE-TOKEN: $GITLAB_TOKEN" '
                    f'--request "$METHOD" "{gitlab_host}$ENDPOINT" "$@"\n'
                )
                api_script.chmod(0o700)
                env["GITLAB_API_CMD"] = str(api_script)

            if config.developer.github_token:
                env["GITHUB_TOKEN"] = config.developer.github_token

                # Git credential helper for GitHub
                # Reads token from GITHUB_TOKEN env var (no secrets on disk)
                gh_username = config.developer.github_username or "x-access-token"
                gh_cred = dev_bin / "git-credential-helper-github"
                gh_cred.write_text(
                    "#!/bin/sh\n"
                    '[ "$1" = "get" ] || exit 0\n'
                    f"echo username={gh_username}\n"
                    'echo password=$GITHUB_TOKEN\n'
                )
                gh_cred.chmod(0o700)
                github_host = config.developer.github_url.rstrip("/")
                env[f"GIT_CONFIG_KEY_{git_config_index}"] = f"credential.{github_host}.helper"
                env[f"GIT_CONFIG_VALUE_{git_config_index}"] = str(gh_cred)
                git_config_index += 1

                # GitHub API wrapper — usage: github-api METHOD /endpoint [curl args]
                # Enforces an endpoint allowlist and strips query strings for matching.
                # Reads token from GITHUB_TOKEN env var (no secrets on disk)
                gh_api_script = dev_bin / "github-api"
                gh_allowlist_cases = "\n".join(
                    f"  {_allowlist_pattern_to_case(p)}) ;;"
                    for p in config.developer.github_api_allowlist
                )
                # GitHub Enterprise uses {url}/api/v3, github.com uses api.github.com
                gh_host_stripped = github_host.rstrip("/")
                if "github.com" == gh_host_stripped.split("//")[-1]:
                    gh_api_base = "https://api.github.com"
                else:
                    gh_api_base = f"{gh_host_stripped}/api/v3"
                gh_api_script.write_text(
                    "#!/bin/sh\n"
                    'METHOD="$1"; shift\n'
                    'ENDPOINT="$1"; shift\n'
                    'CLEAN="${ENDPOINT%%\\?*}"\n'
                    'case "$METHOD $CLEAN" in\n'
                    f"{gh_allowlist_cases}\n"
                    '  *) printf \'{"error":"endpoint not allowed: %s %s"}\\n\' '
                    '"$METHOD" "$CLEAN" >&2; exit 1 ;;\n'
                    "esac\n"
                    f'curl -s --header "Authorization: Bearer $GITHUB_TOKEN" '
                    f'--header "Accept: application/vnd.github+json" '
                    f'--request "$METHOD" "{gh_api_base}$ENDPOINT" "$@"\n'
                )
                gh_api_script.chmod(0o700)
                env["GITHUB_API_CMD"] = str(gh_api_script)

            if git_config_index > 0:
                env["GIT_CONFIG_COUNT"] = str(git_config_index)

        # Static website hosting
        if config.site.enabled and task:
            user_config = config.get_user(task.user_id)
            if user_config and user_config.site_enabled:
                site_dir = config.nextcloud_mount_path / "Users" / task.user_id / config.bot_dir_name / "html"
                env["WEBSITE_PATH"] = str(site_dir)
                env["WEBSITE_URL"] = f"https://{config.site.hostname}/~{task.user_id}"

        # Declarative env vars from skill.toml manifests
        from .skills._env import EnvContext, build_skill_env, dispatch_setup_env_hooks
        env_ctx = EnvContext(
            config=config,
            task=task,
            user_resources=user_resources,
            user_config=user_config,
            user_temp_dir=Path(user_temp_dir),
            is_admin=is_admin,
        )
        skill_env = build_skill_env(selected_skills, skill_index, env_ctx)
        # Declarative env vars don't override hardcoded ones
        for k, v in skill_env.items():
            if k not in env:
                env[k] = v
        # setup_env hooks (for complex skills like developer)
        hook_env = dispatch_setup_env_hooks(selected_skills, skill_index, env_ctx)
        for k, v in hook_env.items():
            if k not in env:
                env[k] = v

        # Wrap in bwrap sandbox if enabled
        if config.security.sandbox_enabled:
            cmd = build_bwrap_cmd(cmd, config, task, is_admin, user_resources, Path(user_temp_dir))

        if use_streaming:
            success, result, actions = _execute_streaming(cmd, env, config, task, on_progress, result_file, prompt)
        else:
            success, result, actions = _execute_simple(cmd, env, config, task, result_file, prompt)

        # Update skills fingerprint after successful interactive execution
        if success and _is_interactive:
            try:
                def _update_fp(c):
                    db.set_user_skills_fingerprint(c, task.user_id, current_fingerprint)
                if conn is not None:
                    _update_fp(conn)
                else:
                    with db.get_db(config.db_path) as fp_conn:
                        _update_fp(fp_conn)
            except Exception:
                pass  # Non-critical

        return success, result, actions

    except FileNotFoundError:
        return False, "Claude Code CLI not found. Is it installed and in PATH?", None
    except Exception as e:
        return False, f"Execution error: {e}", None


def _execute_simple(
    cmd: list[str],
    env: dict,
    config: Config,
    task: db.Task,
    result_file: Path,
    prompt: str = "",
) -> tuple[bool, str, str | None]:
    """Execute Claude Code with subprocess.run (no streaming).

    Prompt is passed via stdin to avoid E2BIG errors from large CLI arguments.
    """
    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=config.scheduler.task_timeout_minutes * 60,
        cwd=str(config.temp_dir),
        env=env,
    )

    output = result.stdout.strip()

    if result.returncode == -9:
        return False, "Claude Code was killed (likely out of memory)", None

    if result.returncode == 0 and output:
        return True, output, None
    elif result.returncode == 0 and result_file.exists():
        return True, result_file.read_text().strip(), None
    elif output:
        return False, output, None
    elif result.stderr.strip():
        return False, result.stderr.strip(), None
    else:
        return False, f"Claude Code produced no output (rc={result.returncode})", None


def _execute_streaming_once(
    cmd: list[str],
    env: dict,
    config: Config,
    task: db.Task,
    on_progress: Callable[[str], None] | None,
    result_file: Path,
    prompt: str = "",
) -> tuple[bool, str, str | None]:
    """Execute Claude Code once with Popen + stream-json parsing for progress updates.

    Prompt is passed via stdin to avoid E2BIG errors from large CLI arguments.
    Returns (success, result_text, actions_taken_json).
    """
    show_tool_use = config.scheduler.progress_show_tool_use
    show_text = config.scheduler.progress_show_text

    # Accumulate tool use descriptions for actions_taken
    actions_descriptions: list[str] = []

    # Capture stderr in a thread to avoid deadlock when both pipes are full
    stderr_lines = []

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(config.temp_dir),
        env=env,
    )

    # Write prompt to stdin and close to signal EOF
    try:
        process.stdin.write(prompt)
        process.stdin.close()
    except BrokenPipeError:
        pass  # process may have exited early

    # Store PID in DB for !stop command
    try:
        with db.get_db(config.db_path) as pid_conn:
            db.update_task_pid(pid_conn, task.id, process.pid)
    except Exception:
        pass  # non-critical

    def _read_stderr():
        for line in process.stderr:
            stderr_lines.append(line)

    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stderr_thread.start()

    # Timeout via timer
    timed_out = threading.Event()

    def _kill():
        timed_out.set()
        process.kill()

    timeout_secs = config.scheduler.task_timeout_minutes * 60
    timer = threading.Timer(timeout_secs, _kill)
    timer.start()

    final_result = None
    raw_stdout_lines = []
    cancelled = False

    try:
        for line in process.stdout:
            raw_stdout_lines.append(line)
            event = parse_stream_line(line)
            if event is None:
                continue

            if isinstance(event, ResultEvent):
                final_result = event
            elif isinstance(event, ToolUseEvent):
                actions_descriptions.append(event.description)
            if isinstance(event, ToolUseEvent) and show_tool_use and on_progress:
                try:
                    on_progress(event.description)
                except Exception:
                    pass  # never affect task execution
            elif isinstance(event, TextEvent) and show_text and on_progress:
                try:
                    on_progress(event.text, italicize=False)
                except Exception:
                    pass

            # Check cancellation flag periodically (on each parsed event)
            if isinstance(event, (ToolUseEvent, TextEvent)):
                try:
                    with db.get_db(config.db_path) as cancel_conn:
                        if db.is_task_cancelled(cancel_conn, task.id):
                            logger.info("Task %d cancelled by user, killing subprocess", task.id)
                            process.kill()
                            cancelled = True
                            break
                except Exception:
                    pass  # non-critical

        process.wait()
        stderr_thread.join(timeout=5)
    finally:
        timer.cancel()

    # Build actions JSON from collected descriptions
    actions_json = json.dumps(actions_descriptions) if actions_descriptions else None

    if cancelled:
        return False, "Cancelled by user", None

    if timed_out.is_set():
        return False, f"Task execution timed out after {config.scheduler.task_timeout_minutes} minutes", None

    if process.returncode == -9:
        return False, "Claude Code was killed (likely out of memory)", None

    stderr_output = "".join(stderr_lines).strip()

    # Extract result: prefer ResultEvent, fall back to result file, then stderr
    if final_result is not None:
        if final_result.success:
            return True, final_result.text.strip(), actions_json
        else:
            return False, final_result.text.strip() or stderr_output or "Unknown error", None

    # Fallback: result file
    if result_file.exists():
        output = result_file.read_text()
        if process.returncode == 0:
            return True, output.strip(), actions_json
        return False, output.strip(), None

    # No ResultEvent and no result file — Claude Code likely errored
    logger.warning(
        "No ResultEvent parsed from stream-json for task %d (rc=%s, stderr=%s, stdout_lines=%d)",
        task.id, process.returncode, stderr_output[:200] if stderr_output else "(empty)", len(raw_stdout_lines),
    )

    if stderr_output:
        return False, stderr_output, None
    elif raw_stdout_lines:
        return False, f"Stream parsing failed (rc={process.returncode}, {len(raw_stdout_lines)} lines)", None
    else:
        return False, f"Claude Code produced no output (rc={process.returncode})", None


def _execute_streaming(
    cmd: list[str],
    env: dict,
    config: Config,
    task: db.Task,
    on_progress: Callable[[str], None],
    result_file: Path,
    prompt: str = "",
) -> tuple[bool, str, str | None]:
    """Execute Claude Code with Popen + stream-json parsing, with auto-retry for transient API errors."""
    last_error = ""

    for attempt in range(API_RETRY_MAX_ATTEMPTS):
        success, result, actions = _execute_streaming_once(cmd, env, config, task, on_progress, result_file, prompt)

        if success:
            return True, result, actions

        # Check if this is a transient API error worth retrying
        if not is_transient_api_error(result):
            return False, result, None

        last_error = result
        parsed = parse_api_error(result)
        request_id = parsed.get("request_id", "unknown") if parsed else "unknown"

        if attempt < API_RETRY_MAX_ATTEMPTS - 1:
            logger.warning(
                "Task %d: transient API error (attempt %d/%d, request_id=%s), retrying in %ds...",
                task.id, attempt + 1, API_RETRY_MAX_ATTEMPTS, request_id, API_RETRY_DELAY_SECONDS,
            )
            time.sleep(API_RETRY_DELAY_SECONDS)
        else:
            logger.error(
                "Task %d: transient API error persisted after %d attempts (request_id=%s)",
                task.id, API_RETRY_MAX_ATTEMPTS, request_id,
            )

    return False, last_error, None


def execute_task_interactive(
    prompt: str,
    user_id: str,
    config: Config,
) -> tuple[bool, str]:
    """
    Execute a prompt interactively (for CLI testing).
    Creates a temporary task and executes it.
    """
    with db.get_db(config.db_path) as conn:
        # Create temporary task
        task_id = db.create_task(
            conn,
            prompt=prompt,
            user_id=user_id,
            source_type="cli",
        )
        task = db.get_task(conn, task_id)
        if not task:
            return False, "Failed to create task"

        # Get dynamic resources from DB (shared_file entries from auto-organizer)
        user_resources = db.get_user_resources(conn, user_id)

        # Execute (config resources are merged internally by execute_task)
        success, result, actions = execute_task(task, config, user_resources)

        # Update task status
        if success:
            db.update_task_status(conn, task_id, "completed", result=result, actions_taken=actions)
        else:
            db.update_task_status(conn, task_id, "failed", error=result)

        return success, result
