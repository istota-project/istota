"""Claude Code execution wrapper."""

import contextlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading  # noqa: F401  — kept for `mock.patch("istota.executor.threading.Timer")` compat
import time  # noqa: F401  — kept for `mock.patch("istota.executor.time.sleep")` compat
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db
from .config import Config
from .context import (
    build_talk_context,
    format_context_for_prompt,
    format_talk_context_for_prompt,
    select_relevant_context,
    select_relevant_talk_context,
)
from .storage import (
    ensure_channel_directories,
    ensure_user_directories_v2,
    get_user_persona_path,
    get_user_scripts_path,
    read_channel_memory,
    read_dated_memories,
    read_user_memory_v2,
)
from .brain import (
    ContextManagementEvent,
    StreamEvent,
    TextEvent,
    ToolEndEvent,
    ToolProgressEvent,
    ToolUseEvent,
    make_brain,
)
from .events import EventWriter, random_progress_message
from .skills.calendar import get_caldav_client, get_calendars_for_user

logger = logging.getLogger("istota.executor")

# Source types treated as interactive (live user behind the turn): they load
# conversation context, sticky skills, the skills changelog, and personal
# memory. The REPL and web chat are full-stack interactive surfaces like
# talk/email.
_INTERACTIVE_SOURCE_TYPES = ("talk", "email", "repl", "web")


def _resolve_user_tz(
    config: Config,
    user_id: str,
    *,
    conn: "sqlite3.Connection | None" = None,
) -> tuple[ZoneInfo, str]:
    """Return (ZoneInfo, tz_str) for a user, falling back to UTC.

    Delegates the DB-vs-in-memory timezone resolution to
    ``Config.resolve_user_timezone`` (so web-UI edits take effect without a
    scheduler restart — ISSUE-099) and wraps the result in a ``ZoneInfo``,
    falling back to UTC if the resolved name is not a valid zone. Pass
    ``conn`` to reuse an existing framework-DB connection on the hot path.
    """
    tz_str = config.resolve_user_timezone(user_id, conn=conn)
    try:
        return ZoneInfo(tz_str), tz_str
    except Exception:
        return ZoneInfo("UTC"), "UTC"

# API error detection / retry policy moved into brain.claude_code; re-exported
# here for backward compatibility with callers (scheduler.py) and tests that
# import these symbols from istota.executor.
from .brain.claude_code import (  # noqa: E402  (kept after module docstring)
    API_ERROR_PATTERN,
    API_RETRY_DELAY_SECONDS,
    API_RETRY_MAX_ATTEMPTS,
    TRANSIENT_STATUS_CODES,
    is_transient_api_error,
    parse_api_error,
)

# Audio extensions eligible for pre-transcription (matches whisper skill file_types)
_AUDIO_EXTENSIONS = frozenset({"mp3", "wav", "ogg", "flac", "m4a", "opus", "webm", "mp4", "aac", "wma"})

# Image extensions eligible for pre-shrinking before they reach the vision model
_IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp", "heic", "heif"})

# 1568 px matches Anthropic's vision long-edge limit; sending anything larger
# just pays tokens for pixels the model downsamples on its end. (Vision also
# enforces a separate ~1.15 MP area limit, which Claude handles itself.)
_IMAGE_MAX_EDGE = 1568
_IMAGE_JPEG_QUALITY = 85


# Result composition + malformed-output detection moved to session.result in
# Phase 0 of the agent-loop migration. Re-exported here for backward
# compatibility with callers (scheduler.py) and tests that import these
# symbols from istota.executor.
from .session.result import (  # noqa: E402,F401
    _AUTOMATED_SOURCE_TYPES,
    _CM_SEGMENT_MIN_CHARS,
    _CODE_FENCE_PATTERN,
    _TERSE_REFERENCE_RE,
    _TERSE_RESULT_MAX_CHARS,
    _TOOL_SYNTAX_PATTERN,
    _TRAILING_REGION_MIN_CHARS,
    _compose_full_result,
    _is_automated_task,
    _is_terse,
    _last_substantial_region,
    _log_compose_override,
    _text_similarity,
    detect_malformed_result,
)


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


def _preshrink_image_attachments(
    attachments: list[str] | None,
    user_temp_dir: Path,
    task_id: int,
) -> list[str] | None:
    """Downscale oversized image attachments before they reach the vision model.

    Phone photos are typically 12+ MP; that's expensive vision tokens and slow
    inference for content the model auto-downsamples anyway. For each image
    attachment we rewrite a JPEG copy under
    ``user_temp_dir/attachments/task_<id>/`` when either:

    * the longest edge exceeds ``_IMAGE_MAX_EDGE``, or
    * the EXIF orientation isn't 1 (Tesseract OCR doesn't honor EXIF, so
      small sideways scans need a physically rotated copy too).

    Returns the (possibly rewritten) attachments list, or the original input
    when there's nothing to do or PIL isn't available.
    """
    if not attachments:
        return attachments

    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError:
        logger.debug("Pillow not available, skipping image pre-shrink")
        return attachments

    # Optional HEIC/HEIF support — iPhone photos arrive in this format.
    try:
        import pillow_heif  # type: ignore[import-not-found]
        pillow_heif.register_heif_opener()
    except ImportError:
        pass

    out_dir = user_temp_dir / "attachments" / f"task_{task_id}"
    rewritten: list[str] = []
    changed = False
    for idx, att in enumerate(attachments):
        ext = Path(att).suffix.lstrip(".").lower()
        if ext not in _IMAGE_EXTENSIONS:
            rewritten.append(att)
            continue
        src = Path(att)
        if not src.is_file():
            rewritten.append(att)
            continue
        try:
            with Image.open(src) as img:
                orientation = img.getexif().get(0x0112, 1) or 1
                w, h = img.size
                # Orientations 5-8 swap the axes; project to final dimensions.
                if orientation in (5, 6, 7, 8):
                    final_w, final_h = h, w
                else:
                    final_w, final_h = w, h
                needs_shrink = max(final_w, final_h) > _IMAGE_MAX_EDGE
                needs_rotate = orientation != 1
                if not needs_shrink and not needs_rotate:
                    rewritten.append(att)
                    continue
                icc = img.info.get("icc_profile")
                # JPEG-only: ask libjpeg to downsample at decode time so a 50 MP
                # panorama doesn't fully decode into RAM before we thumbnail.
                if needs_shrink and ext in ("jpg", "jpeg"):
                    img.draft("RGB", (_IMAGE_MAX_EDGE, _IMAGE_MAX_EDGE))
                img = ImageOps.exif_transpose(img)
                if needs_shrink:
                    img.thumbnail(
                        (_IMAGE_MAX_EDGE, _IMAGE_MAX_EDGE), Image.Resampling.LANCZOS,
                    )
                if img.mode == "RGBA":
                    # Flatten onto white so transparent screenshots don't end up
                    # with a black background after JPEG conversion.
                    flat = Image.new("RGB", img.size, (255, 255, 255))
                    flat.paste(img, mask=img.split()[3])
                    rgb = flat
                elif img.mode not in ("RGB", "L"):
                    rgb = img.convert("RGB")
                else:
                    rgb = img
                out_dir.mkdir(parents=True, exist_ok=True)
                # Prefix with the attachment index so two paths sharing a stem
                # (photo.jpg + photo.png, or duplicate IMG_1234.jpg from
                # different directories) don't overwrite each other.
                out_path = out_dir / f"{idx:02d}_{src.stem}.jpg"
                save_kwargs: dict = {
                    "quality": _IMAGE_JPEG_QUALITY,
                    "optimize": True,
                }
                if icc:
                    save_kwargs["icc_profile"] = icc
                rgb.save(out_path, "JPEG", **save_kwargs)
                logger.info(
                    "Pre-shrunk image %s: %dx%d -> %dx%d (%d bytes)",
                    src.name, w, h, *rgb.size, out_path.stat().st_size,
                )
                rewritten.append(str(out_path))
                changed = True
        except UnidentifiedImageError:
            logger.debug("Could not decode %s (unrecognized format)", att)
            rewritten.append(att)
        except Exception:
            logger.warning("Pre-shrink failed for %s", att, exc_info=True)
            rewritten.append(att)

    return rewritten if changed else attachments


def get_user_temp_dir(config: Config, user_id: str) -> Path:
    """Get the per-user temp directory path."""
    return config.temp_dir / user_id


def discover_calendars_for_task(
    task, config: Config,
) -> list[tuple[str, str, bool]]:
    """Best-effort CalDAV discovery for the task's user.

    Returns ``[]`` when CalDAV is not configured, the server is
    unreachable, or the user owns no calendars. Used by the LLM,
    skill-task, and command-task code paths so manifest specs gated on
    ``gate_has_discovered_calendars`` resolve consistently across all
    three.
    """
    if not (config.caldav_url and config.caldav_username and config.caldav_password):
        return []
    try:
        # ISSUE-101: DAVClient owns a requests.Session whose urllib3 pool
        # spawns a daemon watchdog thread on first connection. Without
        # close() the thread and the open socket leak per call — over
        # days the scheduler accumulated 6000+ of each.
        with get_caldav_client(
            config.caldav_url, config.caldav_username, config.caldav_password,
        ) as client:
            return get_calendars_for_user(client, task.user_id) or []
    except Exception:
        return []


def _resolve_effort(task, config: Config) -> str:
    """Resolve the effort flag for a task.

    Why: a per-job model override (e.g. cron job pinned to Haiku) shouldn't
    inherit `config.effort` set for the default model — Haiku doesn't accept
    --effort and would fail the subprocess.
    """
    task_model = (task.model or "").strip()
    task_effort = (task.effort or "").strip()
    if task_model and not task_effort:
        return ""
    return task_effort or config.effort


def _persist_task_usage(config: Config, conn, task_id: int, usage) -> None:
    """Record native-brain token/cost telemetry to ``task_logs`` + an INFO log.

    ``usage`` is a ``TaskUsage`` or None. ClaudeCodeBrain leaves it None (the CLI
    doesn't surface per-call usage), so this is a no-op for it. Persisting to
    ``task_logs`` keeps cost observable in production with no schema migration.
    Best-effort — a logging failure never affects task success.
    """
    if usage is None:
        return
    payload = json.dumps(
        {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cost_usd": round(usage.cost_usd, 6),
        }
    )
    logger.info("native_usage task_id=%s %s", task_id, payload)
    try:
        if conn is not None:
            db.log_task(conn, task_id, "info", f"usage {payload}")
        else:
            with db.get_db(config.db_path) as usage_conn:
                db.log_task(usage_conn, task_id, "info", f"usage {payload}")
    except Exception:
        logger.debug("failed to persist usage for task %s", task_id, exc_info=True)


def _native_with_user_key(native_config, config: Config, user_id: str):
    """Overlay the user's per-user native-brain API key onto the native config.

    Looks up the encrypted ``native_brain``/``api_key`` secret for ``user_id``;
    when present it replaces the instance-wide key (`[brain.native] api_key` /
    `ISTOTA_BRAIN_NATIVE_API_KEY`), enabling per-user provider credentials in a
    multi-user deployment. Falls back to the instance key on absence/error so a
    missing secret never blocks the task. Returns a copy — never mutates input.
    """
    import dataclasses

    try:
        from . import secrets_store

        key = secrets_store.get_secret(
            config.db_path, user_id, "native_brain", "api_key"
        )
    except Exception:
        logger.debug(
            "native api key secret lookup failed for user=%s", user_id, exc_info=True
        )
        key = None
    if key:
        return dataclasses.replace(native_config, api_key=key)
    return native_config


def _build_native_completer(native_config, timeout: float):
    """A `prompt -> raw_output | None` one-shot completer over the native provider.

    Used for both Pass-2 skill classification and conversation-context triage,
    so the native brain runs them through its own provider/model instead of
    shelling out to the `claude` CLI it isn't using.

    Returns None if the provider can't be built (e.g. missing key / bad config),
    so the caller skips the brain-aware path rather than mis-routing to the CLI.
    """
    try:
        from istota.llm import make_provider
        from istota.llm.oneshot import make_completer

        provider = make_provider(native_config)
        # Generous output budget: a JSON skill array is short, but reasoning
        # models burn tokens thinking first and would otherwise return empty.
        completer = make_completer(provider, native_config.model, max_tokens=4096)
    except Exception:
        logger.warning(
            "native Pass-2 classifier setup failed; skipping semantic routing",
            exc_info=True,
        )
        return None

    def _classify(prompt: str) -> str | None:
        return completer(prompt, timeout=timeout)

    return _classify


def _build_triage_completer(task: "db.Task", config: Config):
    """Conversation-context triage completer, routed through the task's brain.

    Mirrors the Pass-2 skill-routing decision (per-source-type brain routing):
    - claude_code → None, so context triage uses the `claude` CLI as before.
    - native → a native provider completer. If it can't be built (missing key /
      bad config), returns a completer that always yields None so triage fails
      open (includes all older messages) instead of shelling out to the `claude`
      CLI the native brain isn't using.
    """
    from .brain import resolve_brain_kind

    routed = resolve_brain_kind(task.source_type, config.brain)
    if routed.kind != "native":
        return None

    native = _native_with_user_key(routed.native, config, task.user_id)
    completer = _build_native_completer(native, config.conversation.selection_timeout)
    if completer is None:
        return lambda _prompt: None
    return completer


# Credential-related env var patterns to strip from subprocess environments
_CREDENTIAL_ENV_PATTERNS = frozenset({
    "PASSWORD", "SECRET", "TOKEN", "API_KEY",
    "APP_PASSWORD", "NC_PASS", "PRIVATE_KEY",
})

_bwrap_checked: bool | None = None


def _bwrap_available() -> bool:
    """Check if bwrap can create namespaces (cached after first call).

    Returns False on non-Linux, when bwrap is not installed, or inside
    containers without CAP_SYS_ADMIN / user namespace support.
    """
    global _bwrap_checked
    if _bwrap_checked is not None:
        return _bwrap_checked

    import shutil
    import subprocess
    import sys

    if sys.platform != "linux":
        _bwrap_checked = False
        return False

    if shutil.which("bwrap") is None:
        _bwrap_checked = False
        return False

    try:
        result = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--", "true"],
            capture_output=True, timeout=5,
        )
        _bwrap_checked = result.returncode == 0
        if not _bwrap_checked:
            logger.warning(
                "Sandbox skipped: bwrap namespace creation failed "
                "(container without CAP_SYS_ADMIN?): %s",
                result.stderr.decode(errors="replace").strip(),
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Sandbox skipped: bwrap probe failed: %s", exc)
        _bwrap_checked = False
    return _bwrap_checked


def build_clean_env(config: Config) -> dict[str, str]:
    """Build minimal environment for Claude subprocess.

    Returns a restricted env (PATH, HOME, PYTHONUNBUFFERED) plus any
    configured passthrough vars. Credentials are injected per-task by
    execute_task() and optionally routed through the skill proxy.
    """
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
    # Pass through Claude Code auth token if present.
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


def build_stripped_env() -> dict[str, str]:
    """Build os.environ minus credential vars. For heartbeat/cron commands.

    Phase 1.4 of the unified credential resolution refactor: the master
    Fernet key (``ISTOTA_SECRET_KEY``) is no longer preserved here. Skill
    subprocesses that need per-user encrypted secrets get them
    pre-resolved via the manifest ``env:`` blocks.
    """
    return {
        k: v for k, v in os.environ.items()
        if not any(p in k.upper() for p in _CREDENTIAL_ENV_PATTERNS)
    }


# Defense-in-depth: instance-wide credentials that must never be returned
# by the proxy's credential-lookup endpoint, even if a buggy or hostile
# setup_env hook accidentally injects them into the credential env.
#
# After Phase 1.4 the master Fernet key never enters any subprocess env;
# manifests can only declare per-user secrets, and the trusted-side
# resolver returns plaintext values. This frozenset closes the residual
# hole of a setup_env hook doing
# ``env["ISTOTA_SECRET_KEY"] = os.environ["ISTOTA_SECRET_KEY"]``.
# ``derive_lookup_allowlist`` subtracts this set from its return value so
# ``credential-fetch ISTOTA_SECRET_KEY`` is rejected by the proxy even if
# the var sneaks into ``credential_env``.
_PROXY_LOOKUP_BLOCKED = frozenset({"ISTOTA_SECRET_KEY"})


# --- Network proxy allowlist ---

_DEFAULT_NETWORK_HOSTS = frozenset({
    "api.anthropic.com:443",
    "mcp-proxy.anthropic.com:443",
})

_PYPI_HOSTS = frozenset({
    "pypi.org:443",
    "files.pythonhosted.org:443",
})


def _build_network_allowlist(
    config: Config,
    authorized_skills: list[str],
) -> set[str]:
    """Build per-task network allowlist from config and authorized skills.

    Phase 3: keyed on ``authorized_skills`` (the union of selected skills
    and skills auto-authorized via credential presence) so a user with
    GitLab tokens configured can reach gitlab.com even when ``developer``
    wasn't selected — symmetric with credential authorization.
    """
    hosts: set[str] = set(_DEFAULT_NETWORK_HOSTS)

    if config.security.network.allow_pypi:
        hosts |= _PYPI_HOSTS

    hosts.update(config.security.network.extra_hosts)

    # Developer skill: add git remote hosts from config
    if "developer" in authorized_skills and config.developer.enabled:
        from urllib.parse import urlparse

        for url in [config.developer.gitlab_url, config.developer.github_url]:
            if url:
                parsed = urlparse(url)
                host = parsed.hostname
                port = parsed.port or 443
                if host:
                    hosts.add(f"{host}:{port}")

        # GitHub API lives on a separate host from github.com
        if config.developer.github_url:
            parsed = urlparse(config.developer.github_url)
            if parsed.hostname and "github.com" in parsed.hostname:
                hosts.add("api.github.com:443")

    # Google Workspace skill: Google API hosts
    if "google_workspace" in authorized_skills:
        hosts.update({
            "oauth2.googleapis.com:443",
            "www.googleapis.com:443",
            "sheets.googleapis.com:443",
            "docs.googleapis.com:443",
            "drive.googleapis.com:443",
            "calendar-json.googleapis.com:443",
            "chat.googleapis.com:443",
            "gmail.googleapis.com:443",
            "people.googleapis.com:443",
            "admin.googleapis.com:443",
        })

    return hosts


# --- Manifest-derived credential / authorization helpers (Phase 3) ---


def derive_credential_set(skill_index: dict) -> frozenset[str]:
    """All sensitive env-var names declared by any skill manifest.

    Replaces the hand-maintained ``_PROXY_CREDENTIAL_VARS`` constant.
    Includes vars whose source is ``setup_env`` (the manifest declares
    the var name and ``sensitive: true``; the actual value comes from the
    skill's setup_env hook) so the var is split out of Claude's clean env
    and routed through the proxy.
    """
    return frozenset(
        spec.var
        for meta in skill_index.values()
        for spec in meta.env_specs
        if spec.sensitive and spec.var
    )


def derive_authorized_skills(
    selected_skills: list[str],
    skill_index: dict,
    ctx: object,
) -> list[str]:
    """Skills authorized for credential access this task.

    A skill is authorized if EITHER:
      (a) it was selected (Pass 1 / Pass 2 picked it), OR
      (b) ANY of its sensitive EnvSpecs resolves successfully — the user
          has at least one of its credentials configured.

    Replaces ``_authorized_skills_from_credentials``. The auto-auth signal
    is now manifest-derived: adding a credential to a skill's ``env:``
    block is the only step needed to enroll it; no hand-maintained map.

    Three design choices:

    - ``any``, not ``all``. Multi-provider skills (e.g. ``developer`` —
      GitLab token OR GitHub token) auto-authorize when one provider is
      configured.
    - No ``meta.cli`` gate. The ``developer`` skill is doc-only but
      consumes its tokens via ``credential-fetch`` from helper scripts;
      gating on ``cli=true`` would lock it out (regression of e675ed9).
    - ``fallback_var`` does NOT contribute to authorization. An
      operator-set EnvironmentFile fallback is an instance-wide signal
      and would otherwise auto-authorize every user, defeating the
      per-user privacy posture. Resolution passes
      ``fallbacks_disabled=True``.
    """
    from .skills._env import _resolve_env_spec  # noqa: PLC0415

    authorized: set[str] = set(selected_skills)
    for name, meta in skill_index.items():
        if name in authorized:
            continue
        sensitive_specs = [s for s in meta.env_specs if s.sensitive]
        if not sensitive_specs:
            continue
        if any(
            _resolve_env_spec(s, ctx, fallbacks_disabled=True)
            for s in sensitive_specs
        ):
            authorized.add(name)
    return sorted(authorized)


def derive_skill_credential_map(
    authorized_skills: list[str],
    skill_index: dict,
) -> dict[str, set[str]]:
    """Per-skill: which sensitive env vars its manifest declares.

    Replaces ``_build_skill_credential_map``. Used by the proxy to scope
    credential injection: a skill CLI invocation only sees credentials
    its own manifest declared.
    """
    result: dict[str, set[str]] = {}
    for skill in authorized_skills:
        meta = skill_index.get(skill)
        if not meta:
            continue
        creds = {s.var for s in meta.env_specs if s.sensitive and s.var}
        if creds:
            result[skill] = creds
    return result


def derive_lookup_allowlist(
    authorized_skills: list[str],
    skill_index: dict,
) -> set[str]:
    """Union of credentials any authorized skill may fetch via credential-fetch.

    Replaces ``_allowed_credentials_for_skills``. Subtracts
    ``_PROXY_LOOKUP_BLOCKED`` as a defense-in-depth hard-reject list
    (today: ``ISTOTA_SECRET_KEY``).
    """
    allowed: set[str] = set()
    for creds in derive_skill_credential_map(authorized_skills, skill_index).values():
        allowed |= creds
    return allowed - _PROXY_LOOKUP_BLOCKED


def _split_credential_env(
    env: dict[str, str],
    credential_set: frozenset[str] | set[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Split env into (credential_env, clean_env) using ``credential_set``.

    Phase 3: ``credential_set`` is derived per-task from the loaded skill
    index (``derive_credential_set(skill_index)``) instead of a
    module-level constant. The credential dict is passed to the skill
    proxy; the clean dict goes to Claude's subprocess.
    """
    credential_env: dict[str, str] = {}
    clean_env: dict[str, str] = {}
    for k, v in env.items():
        if k in credential_set:
            credential_env[k] = v
        else:
            clean_env[k] = v
    return credential_env, clean_env


def build_allowed_tools(is_admin: bool, skill_names: list[str]) -> list[str]:
    """Build --allowedTools list for restricted security mode.

    Permits all Bash commands — the security boundary is the clean env
    (credential stripping), not command restriction. The tool surface is
    effectively unbounded: skill CLIs, user scripts, cron commands, curl
    for CalDAV/Nextcloud, rclone, etc.
    """
    return ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]


def _validate_workspace_dir(config: Config, workspace_dir: Path) -> Path:
    """Resolve and bounds-check a REPL workspace directory (blocklist posture).

    An arbitrary RW bind expands the sandbox's writable surface, so reject paths
    that overlap sensitive roots: other users' Nextcloud mounts, the istota
    source tree, the credential/secret dirs, and $HOME dotfile config dirs
    (~/.ssh, ~/.config, ~/.claude, ~/.developer). The bwrap-host
    ``--workspace cwd`` case is the security-relevant one; Mac/Docker have no
    bwrap and degrade to running in cwd directly.

    Raises ValueError when the path is forbidden. Returns the resolved path.
    """
    resolved = Path(workspace_dir).resolve()
    home = Path.home().resolve()

    forbidden: list[Path] = []
    # The istota source tree (don't let a workspace shadow our own code).
    try:
        forbidden.append(Path(__file__).resolve().parents[2])
    except IndexError:
        pass
    # Nextcloud mount root (other users' data live under here).
    if config.nextcloud_mount_path:
        forbidden.append(Path(config.nextcloud_mount_path).resolve())
    # Credential / secret dirs + $HOME dotfile config dirs.
    for rel in (".ssh", ".config", ".claude", ".developer", ".aws", ".gnupg"):
        forbidden.append(home / rel)
    secret_key_path = os.environ.get("ISTOTA_SECRET_KEY_FILE")
    if secret_key_path:
        forbidden.append(Path(secret_key_path).resolve().parent)

    def _overlaps(a: Path, b: Path) -> bool:
        # True if a == b, a is under b, or b is under a.
        return a == b or _is_relative_to(a, b) or _is_relative_to(b, a)

    for bad in forbidden:
        try:
            bad_resolved = bad.resolve()
        except OSError:
            continue
        if _overlaps(resolved, bad_resolved):
            raise ValueError(
                f"workspace {resolved} overlaps a protected path ({bad_resolved})"
            )
    return resolved


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def build_bwrap_cmd(
    cmd: list[str],
    config: Config,
    task: db.Task,
    is_admin: bool,
    user_resources: list[db.UserResource],
    user_temp_dir: Path,
    proxy_sock: Path | None = None,
    net_proxy_sock: Path | None = None,
    extra_ro_binds: list[Path] | None = None,
    selected_skills: "frozenset[str] | set[str] | list[str] | None" = None,
    workspace_dir: Path | None = None,
) -> list[str]:
    """Wrap a command in bubblewrap for per-user filesystem isolation.

    Returns the original cmd unchanged if sandbox is not available
    (non-Linux, bwrap not installed, or namespace creation denied).

    ``workspace_dir`` (REPL ``--workspace cwd``) is bound RW and becomes the
    sandbox ``--chdir`` target instead of ``user_temp_dir``. It is bounds-checked
    against the protected-path blocklist (see ``_validate_workspace_dir``) — an
    arbitrary RW bind would otherwise let a workspace shadow the RO ``.developer``
    protections or reach another user's mount.
    """
    if not _bwrap_available():
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

    # --- Application installs (RO) ---
    # Bind extra RO paths from config (e.g. /srv/app for co-located services)
    for ro_path in config.security.sandbox_ro_paths:
        _ro_bind(Path(ro_path))

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
            _ro_bind(creds)  # RO: prevents token persistence attacks
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

    # --- REPL workspace (RW) — validated, bound, and used as the chdir target.
    workspace_resolved: Path | None = None
    if workspace_dir is not None:
        workspace_resolved = _validate_workspace_dir(config, workspace_dir)
        _bind(workspace_resolved)

    # .developer/ scripts (credential-fetch, git helpers) must be read-only
    # to prevent a compromised subprocess from replacing them to intercept
    # credentials.  A later --ro-bind on a subdir overrides the parent --bind.
    dev_dir = user_temp_dir.resolve() / ".developer"
    if dev_dir.is_dir():
        _ro_bind(dev_dir)

    # --- Skill proxy socket (RO inside sandbox) ---
    if proxy_sock and proxy_sock.exists():
        _ro_bind(proxy_sock)

    # --- Network isolation ---
    if net_proxy_sock:
        args.append("--unshare-net")
        if net_proxy_sock.exists():
            _ro_bind(net_proxy_sock)

    # --- Extra RO binds (e.g. service sockets for same-host APIs) ---
    for path in (extra_ro_binds or []):
        if path.exists():
            _ro_bind(path)

    # --- Devbox: Docker CLI + socket for the devbox skill ---
    # The Docker socket is root-equivalent on the host: anything inside the
    # sandbox that can write to it can launch a privileged container that
    # mounts the host root. The skill CLI validates container names before
    # every call, but that check is bypassable from any other Bash invocation
    # in the same sandbox (curl --unix-socket, python socket.AF_UNIX, …).
    #
    # Mitigation in place: only bind the socket when the devbox skill is
    # actually selected for this task. Selection in turn excludes ingest-
    # shaped skills (email, browse, calendar, transcribe, whisper, feeds,
    # bookmarks) so untrusted-content tasks never see the socket at all.
    #
    # TODO: a Docker-API allowlist proxy (à la the gitlab-api wrapper) would
    # close the gap entirely — restrict to exec/cp/inspect/restart on the
    # user's own container and refuse run/create/network/--privileged. Track
    # in the devbox spec; not in this iteration.
    if config.devbox.enabled and selected_skills and "devbox" in selected_skills:
        docker_cli = Path(config.devbox.docker_cli)
        if docker_cli.exists():
            _ro_bind(docker_cli)
        docker_sock = Path(config.devbox.docker_socket)
        if docker_sock.exists():
            _bind(docker_sock)

    # --- Nextcloud mounts (scoped per-user for both admin and non-admin) ---
    mount = config.nextcloud_mount_path
    if mount:
        mount = mount.resolve()
        user_dir = mount / "Users" / task.user_id
        if user_dir.exists():
            _bind(user_dir)
        # Talk attachments directory (flat, shared across conversations)
        talk_dir = mount / "Talk"
        if talk_dir.exists():
            _ro_bind(talk_dir)
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
            # SQLite WAL/SHM files — use --*-bind-try because these are
            # transient (disappear briefly after WAL checkpoint flushes).
            for suffix in ["-wal", "-shm"]:
                wal_path = str(db_path.parent / (db_path.name + suffix))
                if config.security.sandbox_admin_db_write:
                    args.extend(["--bind-try", wal_path, wal_path])
                else:
                    args.extend(["--ro-bind-try", wal_path, wal_path])

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
    if mount:
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
    chdir_target = workspace_resolved or user_temp_dir.resolve()
    args.extend(["--die-with-parent", "--chdir", str(chdir_target)])
    args.append("--")

    if net_proxy_sock:
        # Wrap the command in a shell that starts the TCP-to-Unix bridge as a
        # background process, then execs the original command with HTTPS_PROXY
        # pointed at the bridge. "$@" preserves the original argv from cmd.
        #
        # The bridge's stdin is redirected from /dev/null so it cannot share
        # (and accidentally consume) the prompt that the brain pipes to the
        # exec'd command's stdin — the read end is otherwise inherited by both.
        #
        # No `sleep` before exec: the bridge only needs to be listening before
        # the command opens a *network* connection, which happens well after
        # the command starts and reads its stdin prompt; the bridge's bind()
        # /listen() completes within a few ms of Python startup. On the rare
        # cold-start race the command's own connection retry recovers.
        from .network_proxy import BRIDGE_PORT
        bridge_path = str(user_temp_dir.resolve() / ".developer" / "net-bridge")
        sock_path = str(net_proxy_sock)
        shell_cmd = (
            f"python3 {bridge_path} {sock_path} {BRIDGE_PORT} </dev/null & "
            f"exec env "
            f"HTTPS_PROXY=http://127.0.0.1:{BRIDGE_PORT} "
            f"HTTP_PROXY=http://127.0.0.1:{BRIDGE_PORT} "
            f'NO_PROXY= "$@"'
        )
        args.extend(["/bin/sh", "-c", shell_cmd, "sh"] + cmd)
    else:
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


def _detect_notification_reply(
    task: db.Task,
    config: Config,
    conn: "db.sqlite3.Connection | None" = None,
) -> db.Task | None:
    """
    Check if this task is a reply to a scheduled/briefing notification.

    Returns the parent task if the user is replying to a scheduled or briefing
    notification, so context can be scoped narrowly. Returns None otherwise.
    """
    if not task.reply_to_talk_id or not task.conversation_token or not conn:
        return None
    parent = db.get_reply_parent_task(conn, task.conversation_token, task.reply_to_talk_id)
    if parent and parent.source_type in ("scheduled", "briefing"):
        return parent
    return None


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


def _apply_recency_window_talk(
    messages: list[db.TalkMessage],
    config: Config,
) -> list[db.TalkMessage]:
    """Trim Talk messages to recency window, keeping a guaranteed minimum.

    Always includes the most recent `context_min_messages`. Beyond that,
    includes older messages only if they fall within `context_recency_hours`
    of the newest message. Disabled when context_recency_hours == 0.

    Messages must be in chronological order (oldest first).
    """
    recency_hours = config.conversation.context_recency_hours
    if recency_hours <= 0 or not messages:
        return messages

    min_count = config.conversation.context_min_messages
    if len(messages) <= min_count:
        return messages

    # Cutoff based on the newest message's timestamp
    newest_ts = messages[-1].timestamp
    cutoff_ts = newest_ts - (recency_hours * 3600)

    # Walk backwards: guaranteed minimum, then include if within window
    guaranteed = messages[-min_count:]
    older = messages[:-min_count]
    within_window = [m for m in older if m.timestamp >= cutoff_ts]

    result = within_window + guaranteed
    if len(result) < len(messages):
        logger.info(
            "Recency window trimmed Talk context from %d to %d messages "
            "(min=%d, window=%.1fh, dropped=%d older)",
            len(messages), len(result), min_count, recency_hours,
            len(messages) - len(result),
        )
    return result


def _apply_recency_window_db(
    history: list[db.ConversationMessage],
    config: Config,
) -> list[db.ConversationMessage]:
    """Trim DB conversation messages to recency window, keeping a guaranteed minimum.

    Same logic as _apply_recency_window_talk but for ConversationMessage
    (uses created_at datetime string instead of unix timestamp).

    Messages must be in chronological order (oldest first).
    """
    recency_hours = config.conversation.context_recency_hours
    if recency_hours <= 0 or not history:
        return history

    min_count = config.conversation.context_min_messages
    if len(history) <= min_count:
        return history

    # Parse the newest message's created_at to get cutoff
    newest = history[-1]
    try:
        newest_dt = datetime.fromisoformat(newest.created_at)
    except (ValueError, TypeError):
        return history  # Can't parse, skip filtering

    cutoff_seconds = recency_hours * 3600
    guaranteed = history[-min_count:]
    older = history[:-min_count]

    within_window = []
    for msg in older:
        try:
            msg_dt = datetime.fromisoformat(msg.created_at)
            if (newest_dt - msg_dt).total_seconds() <= cutoff_seconds:
                within_window.append(msg)
        except (ValueError, TypeError):
            within_window.append(msg)  # Keep if unparseable

    result = within_window + guaranteed
    if len(result) < len(history):
        logger.info(
            "Recency window trimmed DB context from %d to %d messages "
            "(min=%d, window=%.1fh, dropped=%d older)",
            len(history), len(result), min_count, recency_hours,
            len(history) - len(result),
        )
    return result


def _build_talk_api_context(
    task: db.Task,
    config: Config,
    conn: "db.sqlite3.Connection | None",
    user_tz: ZoneInfo | None = None,
) -> tuple[str | None, set[int]]:
    """Build conversation context from the local Talk message cache.

    Reads cached messages (populated by the poller), enriches bot messages with
    task metadata from the DB, and formats for the prompt.

    Returns (formatted_context, task_ids_included). task_ids_included is the
    set of DB task IDs whose results appear in the returned context — callers
    use it to deduplicate against memory recall.
    """
    from .context import _parse_reference_id

    limit = config.conversation.talk_context_limit
    if conn is not None:
        raw_messages = db.get_cached_talk_messages(conn, task.conversation_token, limit=limit)
    else:
        with db.get_db(config.db_path) as temp_conn:
            raw_messages = db.get_cached_talk_messages(temp_conn, task.conversation_token, limit=limit)

    if not raw_messages:
        logger.info("No messages from Talk API for token %s", task.conversation_token)
        # Fall through to reply-to fallback
        if task.reply_to_talk_id and task.reply_to_content:
            return f"(In reply to: {task.reply_to_content})", set()
        return None, set()

    # Collect task IDs from referenceIds for batch metadata lookup
    task_ids = []
    for msg in raw_messages:
        ref_id = msg.get("referenceId") or None
        tid, tag = _parse_reference_id(ref_id)
        if tid is not None and tag == "result":
            task_ids.append(tid)

    # Batch lookup task metadata
    task_metadata: dict[int, dict] = {}
    if task_ids:
        if conn is not None:
            task_metadata = db.get_task_metadata_for_context(conn, task_ids)
        else:
            with db.get_db(config.db_path) as temp_conn:
                task_metadata = db.get_task_metadata_for_context(temp_conn, task_ids)

    # Build filtered TalkMessage list
    talk_messages = build_talk_context(
        raw_messages, config.talk.bot_username, task_metadata,
    )

    if not talk_messages:
        logger.info("No relevant Talk messages after filtering for task %d", task.id)
        if task.reply_to_talk_id and task.reply_to_content:
            return f"(In reply to: {task.reply_to_content})", set()
        return None, set()

    # Cap at lookback_count, then apply recency window
    lookback = config.conversation.lookback_count
    if len(talk_messages) > lookback:
        talk_messages = talk_messages[-lookback:]
    talk_messages = _apply_recency_window_talk(talk_messages, config)

    # Reply parent handling: check if replied-to message is in the fetched history
    reply_parent_talk_msg = None
    if task.reply_to_talk_id:
        for tm in talk_messages:
            if tm.message_id == task.reply_to_talk_id:
                reply_parent_talk_msg = tm
                break
        if reply_parent_talk_msg is None and task.reply_to_content:
            # Synthesize a TalkMessage from reply_to_content as fallback
            reply_parent_talk_msg = db.TalkMessage(
                message_id=task.reply_to_talk_id,
                actor_id="unknown",
                actor_display_name="User",
                is_bot=False,
                content=task.reply_to_content,
                timestamp=0,
                actions_taken=None,
                message_role="user",
                task_id=None,
            )
            talk_messages = [reply_parent_talk_msg] + talk_messages

    # Select relevant messages (triage routed through the task's brain)
    relevant = select_relevant_talk_context(
        task.prompt, talk_messages, config, completer=_build_triage_completer(task, config)
    )

    # Ensure reply parent survives triage
    if reply_parent_talk_msg:
        relevant_ids = {m.message_id for m in relevant}
        if reply_parent_talk_msg.message_id not in relevant_ids:
            relevant = [reply_parent_talk_msg] + relevant
            logger.info(
                "Re-added reply parent (talk msg %d) after triage for task %d",
                reply_parent_talk_msg.message_id, task.id,
            )

    if not relevant:
        logger.info("No relevant Talk context selected from %d messages", len(talk_messages))
        return None, set()

    conversation_context = format_talk_context_for_prompt(
        relevant, truncation=config.conversation.context_truncation,
        user_tz=user_tz,
    )
    logger.info(
        "Loaded %d Talk API context messages (%d chars) for task %d",
        len(relevant), len(conversation_context), task.id,
    )
    included_task_ids = {m.task_id for m in relevant if m.task_id}
    return conversation_context, included_task_ids


def _build_db_context(
    task: db.Task,
    config: Config,
    conn: "db.sqlite3.Connection | None",
    user_tz: ZoneInfo | None = None,
) -> tuple[str | None, set[int]]:
    """Build conversation context from the DB (original approach).

    Used for email tasks and as fallback when Talk API is unavailable.

    Returns (formatted_context, task_ids_included). task_ids_included is the
    set of DB task IDs whose results appear in the returned context — callers
    use it to deduplicate against memory recall.
    """
    # Exclude background task types from conversation context
    _exclude_types = ["scheduled", "briefing"]

    if conn is not None:
        history = db.get_conversation_history(
            conn, task.conversation_token, exclude_task_id=task.id,
            limit=config.conversation.lookback_count,
            exclude_source_types=_exclude_types,
        )
    else:
        with db.get_db(config.db_path) as temp_conn:
            history = db.get_conversation_history(
                temp_conn, task.conversation_token, exclude_task_id=task.id,
                limit=config.conversation.lookback_count,
                exclude_source_types=_exclude_types,
            )

    # Inject recent unfiltered tasks (scheduled/briefing in same channel)
    if conn is not None:
        prev_tasks = db.get_previous_tasks(
            conn, task.conversation_token, exclude_task_id=task.id,
            limit=config.conversation.previous_tasks_count,
        )
    else:
        with db.get_db(config.db_path) as temp_conn:
            prev_tasks = db.get_previous_tasks(
                temp_conn, task.conversation_token, exclude_task_id=task.id,
                limit=config.conversation.previous_tasks_count,
            )

    if prev_tasks:
        history_ids = {msg.id for msg in history}
        injected = 0
        for prev in prev_tasks:
            if prev.id not in history_ids:
                history.append(prev)
                injected += 1
        if injected:
            history.sort(key=lambda m: (m.created_at, m.id))
            logger.info(
                "Included %d previous tasks (excluded source_type) in context for task %d",
                injected, task.id,
            )

    logger.debug("Context lookup: token=%s, history_count=%d", task.conversation_token, len(history))

    # Apply recency window before selection
    history = _apply_recency_window_db(history, config)

    if history:
        reply_parent_msg = None
        if task.reply_to_talk_id and task.conversation_token:
            history, reply_parent_msg = _ensure_reply_parent_in_history(
                task, history, config, conn if conn is not None else None,
            )

        relevant = select_relevant_context(
            task.prompt, history, config, completer=_build_triage_completer(task, config)
        )

        if reply_parent_msg:
            relevant_ids = {msg.id for msg in relevant}
            if reply_parent_msg.id not in relevant_ids:
                relevant = [reply_parent_msg] + relevant
                logger.info(
                    "Re-added reply parent (task %d) after triage dropped it for task %d",
                    reply_parent_msg.id, task.id,
                )

        if relevant:
            conversation_context = format_context_for_prompt(
                relevant, truncation=config.conversation.context_truncation,
                user_tz=user_tz,
            )
            logger.info(
                "Loaded %d context messages (%d chars) for task %d",
                len(relevant), len(conversation_context), task.id,
            )
            included_task_ids = {msg.id for msg in relevant}
            return conversation_context, included_task_ids
        else:
            logger.info("No relevant context selected from %d messages", len(history))
    else:
        if task.reply_to_talk_id and task.reply_to_content:
            logger.info("Using inline reply context for task %d (no history)", task.id)
            return f"(In reply to: {task.reply_to_content})", set()
        else:
            logger.info("No conversation history found for token %s", task.conversation_token)

    return None, set()


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


def _recall_memories(
    config: Config,
    conn: "db.sqlite3.Connection | None",
    task: db.Task,
    skip_memory: bool = False,
    exclude_task_ids: set[int] | None = None,
) -> str | None:
    """BM25 search using task prompt as query. Independent of context triage.

    `exclude_task_ids` is the set of task IDs already included as conversation
    history; recall drops conversation chunks for those tasks so the same
    content doesn't appear twice in the prompt.
    """
    if not config.memory_search.enabled or not config.memory_search.auto_recall:
        return None
    if skip_memory:
        return None

    try:
        from .memory.search import search
    except ImportError:
        return None

    include_ids: list[str] = []
    source_types = ["memory_file", "conversation"]
    if task.conversation_token:
        include_ids.append(f"channel:{task.conversation_token}")
        # Channel namespace also has dated channel_memory and durable
        # channel_memory_durable (from CHANNEL.md). Include both.
        source_types += ["channel_memory", "channel_memory_durable"]

    try:
        if conn is not None:
            results = search(
                conn, task.user_id, task.prompt,
                limit=config.memory_search.auto_recall_limit,
                source_types=source_types,
                include_user_ids=include_ids or None,
                exclude_conversation_task_ids=exclude_task_ids or None,
                recency_half_life_days=config.memory_search.recency_half_life_days,
            )
        else:
            with db.get_db(config.db_path) as temp_conn:
                results = search(
                    temp_conn, task.user_id, task.prompt,
                    limit=config.memory_search.auto_recall_limit,
                    source_types=source_types,
                    include_user_ids=include_ids or None,
                    exclude_conversation_task_ids=exclude_task_ids or None,
                    recency_half_life_days=config.memory_search.recency_half_life_days,
                )
    except Exception:
        logger.debug("Memory recall search failed", exc_info=True)
        return None

    if not results:
        return None

    parts = []
    for r in results:
        snippet = r.content[:300].strip()
        parts.append(f"- [{r.source_type}] {snippet}")
    return "\n".join(parts)


def _apply_memory_cap(
    config: Config,
    user_memory: str | None,
    dated_memories: str | None,
    channel_memory: str | None,
    recalled_memories: str | None,
    knowledge_facts: str | None = None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Truncate memory components if total exceeds max_memory_chars.

    Truncation order: recalled → knowledge facts → dated → (warn about user/channel).
    Returns updated (user_memory, dated_memories, channel_memory, recalled_memories, knowledge_facts).
    """
    cap = config.max_memory_chars
    if cap <= 0:
        return user_memory, dated_memories, channel_memory, recalled_memories, knowledge_facts

    total = (
        len(user_memory or "")
        + len(dated_memories or "")
        + len(channel_memory or "")
        + len(recalled_memories or "")
        + len(knowledge_facts or "")
    )
    if total <= cap:
        return user_memory, dated_memories, channel_memory, recalled_memories, knowledge_facts

    over = total - cap

    # Truncate recalled first
    if recalled_memories and over > 0:
        if over >= len(recalled_memories):
            over -= len(recalled_memories)
            recalled_memories = None
        else:
            recalled_memories = recalled_memories[:len(recalled_memories) - over] + "\n...[truncated]"
            over = 0

    # Then knowledge facts
    if knowledge_facts and over > 0:
        if over >= len(knowledge_facts):
            over -= len(knowledge_facts)
            knowledge_facts = None
        else:
            knowledge_facts = knowledge_facts[:len(knowledge_facts) - over] + "\n...[truncated]"
            over = 0

    # Then dated
    if dated_memories and over > 0:
        if over >= len(dated_memories):
            over -= len(dated_memories)
            dated_memories = None
        else:
            dated_memories = dated_memories[:len(dated_memories) - over] + "\n...[truncated]"
            over = 0

    if over > 0:
        logger.warning(
            "Memory cap (%d) exceeded by %d chars after truncating recalled/dated; "
            "user_memory=%d, channel_memory=%d chars remain",
            cap, over, len(user_memory or ""), len(channel_memory or ""),
        )

    return user_memory, dated_memories, channel_memory, recalled_memories, knowledge_facts


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
    recalled_memories: str | None = None,
    excluded_resource_types: set[str] | None = None,
    skip_persona: bool = False,
    cli_skills_text: str | None = None,
    confirmation_context: str | None = None,
    knowledge_facts: str | None = None,
    conn: "db.sqlite3.Connection | None" = None,
) -> str:
    """Build the full prompt for Claude Code execution.

    Pass ``conn`` to let the per-task timezone lookup reuse an existing
    framework-DB connection instead of opening a throwaway one.
    """
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

    # Notes folder: user-configured or default to {bot_dir}/notes/
    if "notes_folder" in resources_by_type:
        notes_folders = resources_by_type["notes_folder"]
        nf_list = "\n".join(
            f"  - {r.display_name or r.resource_path}: {r.resource_path} ({r.permissions})"
            for r in notes_folders
        )
        resource_sections.append(f"Notes Folders:\n{nf_list}")
    elif config.use_mount:
        default_notes = config.nextcloud_mount_path / "Users" / task.user_id / config.bot_dir_name / "notes"
        resource_sections.append(f"Notes Folder:\n  - {default_notes} (readwrite)")

    if "email_folder" in resources_by_type:
        email_folders = resources_by_type["email_folder"]
        email_list = "\n".join(
            f"  - {r.display_name or r.resource_path}: {r.resource_path}"
            for r in email_folders
        )
        resource_sections.append(f"Email Folders:\n{email_list}")

    _excluded_rt = excluded_resource_types or set()
    if "reminders_file" in resources_by_type and "reminders_file" not in _excluded_rt:
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

    # Load emissaries and persona (skipped for neutral output like briefings)
    emissaries_section = ""
    if emissaries and not skip_persona:
        emissaries_section = f"\n\n{emissaries}\n"

    persona_section = ""
    if not skip_persona:
        persona = load_persona(config, user_id=task.user_id)
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

    # Build knowledge facts section
    knowledge_facts_section = ""
    if knowledge_facts:
        knowledge_facts_section = f"""
## Known facts

Current facts about entities relevant to this user:

{knowledge_facts}

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

    # Build recalled memories section
    recalled_section = ""
    if recalled_memories:
        recalled_section = f"""
## Recalled memories (from search)

The following past context was automatically retrieved based on relevance to the current request:

{recalled_memories}

"""

    # Build conversation context section
    context_section = ""
    if conversation_context:
        context_section = f"""
## Conversation context

The following are relevant previous messages from this conversation:

{conversation_context}

"""

    # Build confirmation context section (for re-executed confirmed tasks)
    confirmation_section = ""
    if confirmation_context:
        confirmation_section = f"""## Confirmed action

The user reviewed and approved your previous response. Your previous output:

{confirmation_context}

Execute the action you proposed. If you drafted an email, send it now via `istota-skill email send`. Do not re-draft or ask for confirmation again.

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
        browser_tool = "\n- Web browser for JS-rendered pages: istota-skill browse (see browse skill for details)"

    # CLI skills list (generated from skill index metadata)
    cli_skills_section = cli_skills_text or ""

    # Compute user's local time
    user_tz, user_tz_str = _resolve_user_tz(config, task.user_id, conn=conn)
    user_now = datetime.now(user_tz)
    user_time_str = user_now.strftime("%A, %B %-d, %Y at %-I:%M %p") + f" ({user_tz_str})"
    user_date_str = user_now.strftime("%Y-%m-%d") + f" ({user_tz_str})"
    # UTC anchor for unambiguous elapsed-time arithmetic (ISSUE-091).
    utc_now_str = user_now.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

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
3. Do NOT write to the SQLite database directly (e.g. via sqlite3 CLI or Python sqlite3 module). The database is read-only in your environment. All database modifications are handled by the skill CLI commands (e.g. `istota-skill memory_search`) or via deferred JSON files in $ISTOTA_DEFERRED_DIR.
4. After creating or writing a file, verify it exists on the filesystem (e.g. check with ls or Read). Do not assume a write succeeded.
5. Never edit or create files in your own source directory.
6. Respond directly with your answer — your final output will be sent to the user. While you're working (between tool calls), keep commentary minimal — brief status notes are fine, but save substantive analysis and detailed results for your final response. Intermediate text may be shown to the user as progress updates.
7. Your execution JSONL logs (full conversation traces including subagent output) are stored under ~/.claude/projects/. If a user reports missing or truncated output from a previous task, search these logs for the full assistant message content.
8. Ignore the `currentDate` value in any auto-memory block — it is rendered in the host's UTC clock and may be off by one day from local time. Use the `Today's date`, `Current time`, and `User timezone` lines at the top of this prompt as the authoritative source for "today".
9. When computing elapsed time between two timestamps ("X ago", "merged N hours ago", etc.), normalize both to ISO 8601 UTC first and subtract the full timestamps. Do not subtract clock-face hours/minutes by hand — that gives the wrong answer when the timestamps straddle a UTC midnight, end-of-month, or DST boundary. The `Current UTC` line above is your reference for "now"."""
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
6. Respond directly with your answer — your final output will be sent to the user. While you're working (between tool calls), keep commentary minimal — brief status notes are fine, but save substantive analysis and detailed results for your final response. Intermediate text may be shown to the user as progress updates.
7. Ignore the `currentDate` value in any auto-memory block — it is rendered in the host's UTC clock and may be off by one day from local time. Use the `Today's date`, `Current time`, and `User timezone` lines at the top of this prompt as the authoritative source for "today".
8. When computing elapsed time between two timestamps ("X ago", "merged N hours ago", etc.), normalize both to ISO 8601 UTC first and subtract the full timestamps. Do not subtract clock-face hours/minutes by hand — that gives the wrong answer when the timestamps straddle a UTC midnight, end-of-month, or DST boundary. The `Current UTC` line above is your reference for "now"."""

    group_chat_line = ""
    if task.is_group_chat:
        group_chat_line = f"\nThis is a group conversation. You were @mentioned by '{task.user_id}'. Other participants' messages are visible in conversation context below."

    # Per-user plus-addressed email line
    per_user_email_line = ""
    if config.email.enabled and config.email.bot_email and "@" in config.email.bot_email:
        local, domain = config.email.bot_email.split("@", 1)
        per_user_email_line = f"\nPer-user email: {local}+{task.user_id}@{domain}"

    prompt = f"""You are {config.bot_name}, a helpful assistant bot. You are responding to a request from user '{task.user_id}'.

Current time: {user_time_str}
Today's date: {user_date_str}
User timezone: {user_tz_str}
Current UTC: {utc_now_str}
Current task ID: {task.id}
Conversation token: {task.conversation_token or 'none'}{group_chat_line}
Source: {source_type or task.source_type or 'unknown'}
Output target: {output_target or 'text'}{per_user_email_line}
{db_path_line}
{emissaries_section}{persona_section}
## User's accessible resources

{resources_text}
{memory_section}{knowledge_facts_section}{channel_memory_section}{dated_memories_section}{recalled_section}## Available tools

You have access to:
{file_tools}{browser_tool}
{cli_skills_section}{db_tool_line}
- Email: two commands exist — `istota-skill email send` sends immediately via SMTP, `istota-skill email output` writes a deferred reply file. Use `send` when the user asks you to email someone (this is the common case). Only use `output` when this task arrived as an incoming email (Source: email) and you are composing the reply. See the email skill for details.

{rules_section}
{context_section}
{confirmation_section}## User's request

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
    event_writer: EventWriter | None = None,
    workspace_dir: "Path | None" = None,
) -> tuple[bool, str, str | None, str | None]:
    """
    Execute a task using the configured brain.

    Returns (success, result_text, actions_taken_json, execution_trace_json).

    Args:
        event_writer: Optional task-event sink. When provided, the executor
            adapts the brain's StreamEvent stream into TaskEvents and persists
            them; consumers (Talk, log channel, push, SSE, admin) read those.
            None for dry runs and CLI paths with no observability surface.

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

    # Pre-shrink oversized image attachments — vision tokens scale with pixels
    # and phone photos are 12+ MP. EXIF rotation is applied in the same pass so
    # the model and any downstream OCR see a correctly-oriented image.
    shrunken = _preshrink_image_attachments(
        task.attachments, get_user_temp_dir(config, task.user_id), task.id,
    )
    if shrunken is not task.attachments:
        task.attachments = shrunken

    # Select and load relevant skills
    from .skills._loader import (
        load_skill_index, select_skills, load_skills,
        compute_skills_fingerprint, load_skills_changelog,
        classify_skills,
    )

    is_admin = config.is_admin(task.user_id)

    _bundled_dir = config.bundled_skills_dir
    skill_index = load_skill_index(config.skills_dir, bundled_dir=_bundled_dir)
    user_resource_types = {r.resource_type for r in user_resources}
    # Combine instance-wide and per-user disabled skills
    user_config = config.get_user(task.user_id)
    _disabled = set(config.disabled_skills)
    if user_config:
        _disabled |= set(user_config.disabled_skills)

    # Build sticky skills from recent conversation + explicit reply parent
    sticky_skills: set[str] | None = None
    if task.conversation_token and task.source_type in _INTERACTIVE_SOURCE_TYPES:
        def _get_sticky(c: "db.sqlite3.Connection") -> set[str]:
            skills = db.get_recent_conversation_skills(
                c, task.conversation_token,
                exclude_task_id=task.id,
                max_age_minutes=30,
                limit=2,
            )
            # Also include skills from explicit reply parent (no time limit)
            if task.reply_to_talk_id:
                parent = db.get_reply_parent_task(c, task.conversation_token, task.reply_to_talk_id)
                if parent and parent.selected_skills:
                    try:
                        skills |= set(json.loads(parent.selected_skills))
                    except (json.JSONDecodeError, TypeError):
                        pass
            return skills
        try:
            if conn is not None:
                sticky_skills = _get_sticky(conn)
            else:
                with db.get_db(config.db_path) as temp_conn:
                    sticky_skills = _get_sticky(temp_conn)
            if sticky_skills:
                logger.debug("Sticky skills from conversation: %s", ", ".join(sorted(sticky_skills)))
        except Exception:
            logger.debug("Failed to get sticky skills for task %d", task.id, exc_info=True)

    selected_skills = select_skills(
        prompt=task.prompt,
        source_type=task.source_type,
        user_resource_types=user_resource_types,
        skill_index=skill_index,
        is_admin=is_admin,
        attachments=task.attachments,
        disabled_skills=_disabled if _disabled else None,
        sticky_skills=sticky_skills or None,
        enabled_experimental_features=frozenset(config.experimental.features),
    )

    # Pass 2: LLM-based semantic routing. Inference goes through the brain that
    # will run the task (honoring per-source-type routing), so the native brain
    # uses its own provider/model instead of shelling out to the `claude` CLI it
    # isn't using. Role aliases like "fast" only make sense in the claude_code
    # namespace; under native we classify with the endpoint's own model.
    if config.skills.semantic_routing:
        from .brain import resolve_brain_kind
        _routed_brain = resolve_brain_kind(task.source_type, config.brain)
        _pass2_classifier = None
        _pass2_model = make_brain(config.brain).resolve_model_name(
            config.skills.semantic_routing_model
        )
        _pass2_skip = False
        if _routed_brain.kind == "native":
            _pass2_native = _native_with_user_key(
                _routed_brain.native, config, task.user_id
            )
            _pass2_classifier = _build_native_completer(
                _pass2_native, config.skills.semantic_routing_timeout
            )
            _pass2_model = _pass2_native.model
            # If the native classifier couldn't be built, skip Pass 2 rather
            # than falling back to `claude --model <native-id>` (wrong CLI).
            _pass2_skip = _pass2_classifier is None

        extra_skills = (
            []
            if _pass2_skip
            else classify_skills(
                prompt=task.prompt,
                skill_index=skill_index,
                already_selected=set(selected_skills),
                disabled_skills=_disabled if _disabled else None,
                is_admin=is_admin,
                model=_pass2_model,
                timeout=config.skills.semantic_routing_timeout,
                user_resource_types=user_resource_types,
                enabled_experimental_features=frozenset(config.experimental.features),
                classifier=_pass2_classifier,
            )
        )
        if extra_skills:
            all_selected = set(selected_skills) | set(extra_skills)
            # Re-apply exclude_skills (Pass 1 already applied, but new skills may trigger new exclusions)
            excluded = set()
            for n in list(all_selected):
                m = skill_index.get(n)
                if m:
                    for ex in m.exclude_skills:
                        if ex in all_selected:
                            excluded.add(ex)
            all_selected -= excluded
            selected_skills = sorted(all_selected)

    # Persist selected skills for conversation stickiness
    if task.id and selected_skills:
        def _save_skills(c: "db.sqlite3.Connection") -> None:
            db.save_task_selected_skills(c, task.id, selected_skills)
        try:
            if conn is not None:
                _save_skills(conn)
            else:
                with db.get_db(config.db_path) as temp_conn:
                    _save_skills(temp_conn)
            logger.debug("Saved %d selected skills for task %d", len(selected_skills), task.id)
        except Exception:
            logger.warning("Failed to save selected_skills for task %d", task.id, exc_info=True)

    skills_doc = load_skills(
        config.skills_dir, selected_skills, config.bot_name, config.bot_dir_name,
        skill_index=skill_index, bundled_dir=_bundled_dir,
    )
    if skills_doc:
        # Resolve per-user scripts directory
        scripts_nc_path = get_user_scripts_path(task.user_id, config.bot_dir_name)
        if config.use_mount:
            scripts_dir = str(config.nextcloud_mount_path / scripts_nc_path.lstrip("/"))
        else:
            scripts_dir = f"{config.rclone_remote}:{scripts_nc_path}"
        skills_doc = skills_doc.replace("{scripts_dir}", scripts_dir)
        skills_doc = skills_doc.replace("{user_id}", task.user_id)
    if selected_skills:
        logger.debug("Selected skills: %s", ", ".join(selected_skills))

    # Compute behavior flags from selected skills
    _selected_metas = [skill_index[n] for n in selected_skills if n in skill_index]
    _skip_memory = any(m.exclude_memory for m in _selected_metas)
    _skip_persona = any(m.exclude_persona for m in _selected_metas)
    _excluded_resource_types = {rt for m in _selected_metas for rt in m.exclude_resources}

    # Skills changelog: detect changes for interactive tasks
    skills_changelog = None
    _is_interactive = task.source_type in _INTERACTIVE_SOURCE_TYPES
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
    context_task_ids: set[int] = set()
    notification_parent = _detect_notification_reply(task, config, conn)
    context_skip_reason = None
    if not use_context:
        context_skip_reason = "use_context=False"
    elif not config.conversation.enabled:
        context_skip_reason = "conversation.enabled=False in config"
    elif task.source_type not in _INTERACTIVE_SOURCE_TYPES:
        context_skip_reason = f"source_type={task.source_type!r} (not interactive)"
    elif not task.conversation_token:
        context_skip_reason = "no conversation_token"

    if context_skip_reason:
        logger.info("Skipping context lookup: %s", context_skip_reason)
    elif notification_parent is not None:
        # Reply to a scheduled/briefing notification — scope context narrowly
        parent_result = notification_parent.result or ""
        if parent_result:
            conversation_context = (
                "[Note: The user is replying to a scheduled notification. "
                "If they are simply acknowledging it, respond very briefly (1 sentence or less). "
                "Do not investigate or bring up unrelated topics.]\n\n"
                f"[Scheduled notification (task {notification_parent.id})]:\n"
                f"{parent_result[:2000]}"
            )
        logger.info(
            "Notification reply detected for task %d (parent task %d, source_type=%s)",
            task.id, notification_parent.id, notification_parent.source_type,
        )
    else:
        # Resolve user TZ once for context formatting (mirrors prompt header).
        _ctx_user_tz, _ = _resolve_user_tz(config, task.user_id, conn=conn)

        # Try Talk API-based context for Talk tasks, fall back to DB on failure
        _used_talk_api = False
        if task.source_type == "talk":
            try:
                conversation_context, context_task_ids = _build_talk_api_context(
                    task, config, conn, user_tz=_ctx_user_tz,
                )
                _used_talk_api = conversation_context is not None
            except Exception as e:
                logger.warning(
                    "Talk API context fetch failed for task %d, falling back to DB: %s",
                    task.id, e,
                )

        # DB-based context fallback (always used for email, fallback for Talk)
        if not _used_talk_api:
            conversation_context, context_task_ids = _build_db_context(
                task, config, conn, user_tz=_ctx_user_tz,
            )

    # Load user memory (auto-create directories if missing)
    # Skills with exclude_memory=true (e.g. briefing) skip personal memory
    # to avoid leaking private context into newsletter-style output.
    user_memory = None
    if not _skip_memory:
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
    discovered_calendars = discover_calendars_for_task(task, config)

    # Auto-load recent dated memories if enabled
    dated_memories = None
    if (config.sleep_cycle.enabled
            and config.sleep_cycle.auto_load_dated_days > 0
            and not _skip_memory):
        try:
            dated_memories = read_dated_memories(
                config, task.user_id,
                max_days=config.sleep_cycle.auto_load_dated_days,
            )
        except Exception:
            pass  # Graceful degradation
    user_config = config.get_user(task.user_id)

    # Auto-recall memories via BM25 search. Exclude task IDs already included
    # as conversation history so the same chunk doesn't appear twice.
    recalled_memories = _recall_memories(
        config, conn, task,
        skip_memory=_skip_memory,
        exclude_task_ids=context_task_ids or None,
    )

    # Load knowledge graph facts (filtered by relevance to prompt)
    knowledge_facts_text = None
    if not _skip_memory:
        try:
            from .memory.knowledge_graph import (
                ensure_table, get_current_facts, select_relevant_facts,
                format_facts_for_prompt,
            )
            max_kf = config.max_knowledge_facts
            if conn is not None:
                ensure_table(conn)
                kg_facts = get_current_facts(conn, task.user_id)
                if kg_facts:
                    kg_facts = select_relevant_facts(
                        kg_facts, task.prompt, task.user_id, max_facts=max_kf,
                    )
                    if kg_facts:
                        knowledge_facts_text = format_facts_for_prompt(kg_facts)
            else:
                with db.get_db(config.db_path) as _kg_conn:
                    ensure_table(_kg_conn)
                    kg_facts = get_current_facts(_kg_conn, task.user_id)
                    if kg_facts:
                        kg_facts = select_relevant_facts(
                            kg_facts, task.prompt, task.user_id, max_facts=max_kf,
                        )
                        if kg_facts:
                            knowledge_facts_text = format_facts_for_prompt(kg_facts)
        except Exception:
            pass  # Graceful degradation

    # Apply memory size cap
    user_memory, dated_memories, channel_memory, recalled_memories, knowledge_facts_text = _apply_memory_cap(
        config, user_memory, dated_memories, channel_memory, recalled_memories, knowledge_facts_text,
    )

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

    # Build CLI skills list from skill index
    from .skills._loader import format_cli_skills
    cli_skills_text = format_cli_skills(skill_index)

    # Build prompt
    # Detect confirmed tasks — pass their previous output as confirmation context
    _confirmation_context = None
    if task.confirmed_at and task.confirmation_prompt:
        _confirmation_context = task.confirmation_prompt

    prompt = build_prompt(
        task, user_resources, config, skills_doc, conversation_context, user_memory,
        discovered_calendars, user_email_addresses, dated_memories, channel_memory,
        skills_changelog, is_admin, emissaries,
        source_type=task.source_type,
        output_target=effective_output_target,
        recalled_memories=recalled_memories,
        excluded_resource_types=_excluded_resource_types or None,
        skip_persona=_skip_persona,
        cli_skills_text=cli_skills_text,
        confirmation_context=_confirmation_context,
        knowledge_facts=knowledge_facts_text,
        conn=conn,
    )

    # Log prompt size breakdown
    context_chars = len(conversation_context) if conversation_context else 0
    memory_chars = len(user_memory or "") + len(dated_memories or "") + len(channel_memory or "") + len(recalled_memories or "")
    skills_chars = len(skills_doc or "")
    prompt_chars = len(prompt)
    logger.info(
        "Prompt for task %d: %d chars total (context: %d, memory: %d, skills: %d, other: %d)",
        task.id, prompt_chars, context_chars, memory_chars, skills_chars,
        prompt_chars - context_chars - memory_chars - skills_chars,
    )

    if dry_run:
        return True, f"[DRY RUN] Would execute with prompt:\n\n{prompt}", None, None

    # Write prompt to temp file for debugging
    prompt_file = user_temp_dir / f"task_{task.id}_prompt.txt"
    prompt_file.write_text(prompt)

    # Result file path
    result_file = user_temp_dir / f"task_{task.id}_result.txt"

    # Clean up any previous result file
    if result_file.exists():
        result_file.unlink()

    try:
        if event_writer is not None:
            # Stamp a generic progress verb so stream surfaces (web chat) show a
            # real "working on it" line instead of a hardcoded placeholder until
            # the first tool/text event arrives. Talk ignores this payload and
            # picks its own verb at ack time; both draw from the same list.
            event_writer.emit("task_started", {"text": random_progress_message()})
        use_streaming = event_writer is not None
        allowed = build_allowed_tools(is_admin, selected_skills)

        env = build_clean_env(config)
        env.update({
            "ISTOTA_TASK_ID": str(task.id),
            "ISTOTA_USER_ID": task.user_id,
            "ISTOTA_BOT_DIR_NAME": config.bot_dir_name,
            "ISTOTA_CONVERSATION_TOKEN": task.conversation_token or "",
            "ISTOTA_DEFERRED_DIR": str(user_temp_dir),
            "ISTOTA_EXPERIMENTAL_FEATURES": ",".join(config.experimental.features),
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

        # Devbox: the agent's persistent dev container. Skill CLI shells
        # into ``devbox-<user_id>`` via the host docker socket.
        if config.devbox.enabled:
            env["ISTOTA_DEVBOX_CONTAINER"] = (
                f"{config.devbox.container_prefix}{task.user_id}"
            )
            env["ISTOTA_DEVBOX_DOCKER_CLI"] = config.devbox.docker_cli
            env["ISTOTA_DEVBOX_DOCKER_SOCKET"] = config.devbox.docker_socket
            env["ISTOTA_DEVBOX_EXEC_TIMEOUT"] = str(
                config.devbox.exec_timeout_seconds
            )
            env["ISTOTA_DEVBOX_MAX_OUTPUT_BYTES"] = str(
                config.devbox.max_output_bytes
            )

        # Static website hosting
        if config.site.enabled and task:
            user_config = config.get_user(task.user_id)
            if user_config and user_config.site_enabled:
                site_dir = config.nextcloud_mount_path / "Users" / task.user_id / config.bot_dir_name / "html"
                env["WEBSITE_PATH"] = str(site_dir)
                env["WEBSITE_URL"] = f"https://{config.site.hostname}/~{task.user_id}"

        # Declarative env vars from skill manifests
        from .skills._env import EnvContext, build_skill_env, dispatch_setup_env_hooks
        env_ctx = EnvContext(
            config=config,
            task=task,
            user_resources=user_resources,
            user_config=user_config,
            user_temp_dir=Path(user_temp_dir),
            is_admin=is_admin,
            discovered_calendars=list(discovered_calendars or []),
        )
        # Phase 3: resolve manifest env vars for ``authorized_skills`` —
        # the union of selected skills and skills auto-authorized via
        # credential presence. ``derive_authorized_skills`` walks each
        # skill's sensitive specs with ``fallbacks_disabled=True`` so
        # operator-set EnvironmentFile fallbacks cannot fan out to per-
        # user auto-authorization. Resolution itself (below) honors
        # fallbacks for the value path.
        authorized_skills = derive_authorized_skills(
            selected_skills, skill_index, env_ctx,
        )
        skill_env = build_skill_env(authorized_skills, skill_index, env_ctx)
        # Declarative env vars don't override hardcoded ones
        for k, v in skill_env.items():
            if k not in env:
                env[k] = v
        # setup_env hooks self-gate; the dispatcher iterates the full
        # skill_index regardless of the argument it's given.
        hook_env = dispatch_setup_env_hooks(authorized_skills, skill_index, env_ctx)
        for k, v in hook_env.items():
            if k not in env:
                env[k] = v

        # Credential isolation via skill proxy: strip secrets from Claude's env
        # and run skill CLIs through a Unix socket proxy that injects them.
        _proxy_ctx = None
        _proxy_sock = None
        if config.security.skill_proxy_enabled:
            from .skill_proxy import SkillProxy
            # Phase 3: credential set is derived from the loaded skill
            # index; no hand-maintained constant. Same for the per-skill
            # credential map and the lookup-endpoint allowlist.
            credential_set = derive_credential_set(skill_index)
            credential_env, env = _split_credential_env(env, credential_set)
            if credential_env:
                # Use /tmp for socket path to stay within AF_UNIX length limit (~104 chars).
                # build_bwrap_cmd() bind-mounts this file into the sandbox.
                # PID is included so concurrent processes (xdist test workers,
                # parallel scheduler instances on the same host) don't race on
                # the same path — task.id alone collides when each process has
                # its own DB.
                _proxy_sock = Path(tempfile.gettempdir()) / f"istota-proxy-{os.getpid()}-{task.id}.sock"
                env["ISTOTA_SKILL_PROXY_SOCK"] = str(_proxy_sock)
                allowed_creds = derive_lookup_allowlist(
                    authorized_skills, skill_index,
                )
                skill_cred_map = derive_skill_credential_map(
                    authorized_skills, skill_index,
                )
                cli_skills = frozenset(
                    name for name, meta in skill_index.items() if meta.cli
                )
                logger.info(
                    "proxy_authorization task_id=%d selected=%d authorized=%d "
                    "selected_skills=%s authorized_skills=%s",
                    task.id, len(selected_skills), len(authorized_skills),
                    ",".join(sorted(selected_skills)),
                    ",".join(authorized_skills),
                )
                _proxy_ctx = SkillProxy(
                    _proxy_sock, credential_env, env,
                    timeout=config.security.skill_proxy_timeout,
                    allowed_credentials=allowed_creds,
                    skill_credential_map=skill_cred_map,
                    allowed_skills=cli_skills,
                    authorized_skills=frozenset(authorized_skills),
                    task_id=task.id,
                )

        # Network isolation via CONNECT proxy: outbound traffic restricted
        # to an allowlist of host:port pairs via --unshare-net + proxy.
        _net_proxy_ctx = None
        _net_proxy_sock = None
        if config.security.network.enabled and config.security.sandbox_enabled:
            from .network_proxy import NetworkProxy, write_bridge_script

            allowed_hosts = _build_network_allowlist(config, authorized_skills)

            # Write bridge script to .developer/ (RO inside sandbox)
            dev_dir = Path(user_temp_dir) / ".developer"
            dev_dir.mkdir(parents=True, exist_ok=True)
            write_bridge_script(dev_dir / "net-bridge")

            _net_proxy_sock = Path(tempfile.gettempdir()) / f"istota-net-{os.getpid()}-{task.id}.sock"
            _net_proxy_ctx = NetworkProxy(
                _net_proxy_sock, allowed_hosts,
            )

        # Collect extra paths to RO bind-mount into the sandbox
        _extra_ro_binds: list[Path] = []

        # Sandbox wrapper closure — captures the per-task bind config so the
        # brain can wrap its raw cmd without knowing anything about bwrap.
        def _sandbox_wrap(raw_cmd: list[str]) -> list[str]:
            if not config.security.sandbox_enabled:
                return raw_cmd
            return build_bwrap_cmd(
                raw_cmd, config, task, is_admin, user_resources,
                Path(user_temp_dir), proxy_sock=_proxy_sock,
                net_proxy_sock=_net_proxy_sock,
                extra_ro_binds=_extra_ro_binds,
                selected_skills=frozenset(selected_skills),
                workspace_dir=workspace_dir,
            )

        # Adapt the brain's (widened) StreamEvent stream to TaskEvents. Called
        # by the brain in place of the old string callback. For loop-based
        # brains (NativeBrain) this fires on a worker thread, not the brain's
        # event loop (Layer 3 invariant) — the body stays plain-synchronous
        # either way. progress_show_tool_use / progress_show_text gate whether
        # tool_* and progress_text events are emitted at all.
        show_tool_use = config.scheduler.progress_show_tool_use
        show_text = config.scheduler.progress_show_text

        def _on_event(event: StreamEvent) -> None:
            if event_writer is None:
                return
            if isinstance(event, ToolUseEvent) and show_tool_use:
                event_writer.emit("tool_start", {
                    "tool_name": event.tool_name,
                    "description": event.description,
                    "tool_call_id": event.tool_call_id,  # "" under ClaudeCodeBrain
                })
            elif isinstance(event, ToolEndEvent) and show_tool_use:
                event_writer.emit("tool_end", {
                    "tool_name": event.tool_name,
                    "tool_call_id": event.tool_call_id,
                    "success": event.success,
                    "duration_ms": event.duration_ms,
                })
            elif isinstance(event, ToolProgressEvent):
                # Web SSE only; Talk/log subscribers ignore this kind.
                event_writer.emit("tool_progress", {
                    "tool_name": event.tool_name,
                    "tool_call_id": event.tool_call_id,
                    "text": event.text,
                })
            elif isinstance(event, TextEvent) and show_text:
                # NativeBrain already suppresses the final turn's text (it
                # becomes the result); ClaudeCodeBrain's ResultEvent is a
                # distinct frame, so neither double-renders.
                event_writer.emit("progress_text", {"text": event.text})
            elif isinstance(event, ContextManagementEvent):
                event_writer.emit("context_management")

        def _on_pid(pid: int) -> None:
            try:
                with db.get_db(config.db_path) as pid_conn:
                    db.update_task_pid(pid_conn, task.id, pid)
            except Exception:
                pass  # non-critical

        def _cancel_check() -> bool:
            try:
                with db.get_db(config.db_path) as cancel_conn:
                    return db.is_task_cancelled(cancel_conn, task.id)
            except Exception:
                return False

        # Custom system prompt path (claude_code-only knob; brain ignores
        # if the file is missing)
        sp_path: Path | None = None
        if config.custom_system_prompt:
            sp_path = config.skills_dir.parent / "system-prompt.md"

        from .brain import BrainRequest, resolve_brain_kind
        # Per-source-type brain routing (gradual rollout): an operator can
        # map this task's source_type to a different brain kind via
        # [brain.source_type_overrides]. No-op for the common case.
        _brain_config = resolve_brain_kind(task.source_type, config.brain)
        if _brain_config.kind != config.brain.kind:
            logger.info(
                "brain routing: task %d source_type=%s -> kind=%s (default %s)",
                task.id, task.source_type, _brain_config.kind, config.brain.kind,
            )
        # Overlay the per-user native-brain API key (encrypted secrets) so a
        # multi-user deployment can give each user their own provider credential.
        if _brain_config.kind == "native":
            import dataclasses as _dc
            _brain_config = _dc.replace(
                _brain_config,
                native=_native_with_user_key(
                    _brain_config.native, config, task.user_id
                ),
            )
        brain = make_brain(_brain_config)
        # Resolve aliases (role, provider) to a canonical model ID. Talk-poller
        # tasks already arrive resolved via the !model prefix path; cron jobs,
        # briefings, email, and operator istota_model defaults can still carry
        # an alias string here, which the brain CLI doesn't accept directly.
        # `resolve_model_name` is a no-op for canonical IDs and unknown strings.
        req = BrainRequest(
            prompt=prompt,
            allowed_tools=allowed,
            # Non-sandbox path (Mac/dev/Docker): the REPL points the brain's
            # working directory at the launch dir directly. No blocklist here —
            # without bwrap the process already runs with the user's own FS
            # access, so the bind-shadowing threat the blocklist guards doesn't
            # apply (it fires in build_bwrap_cmd, the sandboxed path). Keyed off
            # *effective* sandboxing: when sandbox_enabled is set but bwrap is
            # absent (Mac/dev), build_bwrap_cmd returns the cmd unwrapped with no
            # --chdir, so this cwd is what actually takes effect for --workspace.
            cwd=(
                Path(workspace_dir).resolve()
                if workspace_dir is not None
                and not (config.security.sandbox_enabled and _bwrap_available())
                else Path(config.temp_dir)
            ),
            env=env,
            timeout_seconds=config.scheduler.task_timeout_minutes * 60,
            model=brain.resolve_model_name((task.model or "").strip() or config.model),
            effort=_resolve_effort(task, config),
            custom_system_prompt_path=sp_path,
            streaming=use_streaming,
            on_progress=_on_event if use_streaming else None,
            cancel_check=_cancel_check,
            on_pid=_on_pid,
            sandbox_wrap=_sandbox_wrap,
            result_file=result_file,
        )

        with contextlib.ExitStack() as stack:
            if _proxy_ctx is not None:
                stack.enter_context(_proxy_ctx)
            if _net_proxy_ctx is not None:
                stack.enter_context(_net_proxy_ctx)
            brain_result = brain.execute(req)

        success = brain_result.success
        result = brain_result.result_text
        actions = brain_result.actions_taken
        trace = brain_result.execution_trace

        # Persist native-brain token/cost telemetry (no-op for claude_code).
        _persist_task_usage(config, conn, task.id, brain_result.usage)

        # CM-aware / terse-result composition: reconcile result_text with
        # the trace so substantial intermediate text isn't lost when the
        # final ResultEvent is terse. Same logic both brains will need.
        if success and trace:
            try:
                trace_list = json.loads(trace)
                result = _compose_full_result(result, trace_list, task=task)
            except (json.JSONDecodeError, TypeError):
                pass

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

        return success, result, actions, trace

    except Exception as e:
        return False, f"Execution error: {e}", None, None


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
        success, result, actions, trace = execute_task(task, config, user_resources)

        # Update task status
        if success:
            db.update_task_status(conn, task_id, "completed", result=result, actions_taken=actions, execution_trace=trace)
        else:
            db.update_task_status(conn, task_id, "failed", error=result)

        return success, result
