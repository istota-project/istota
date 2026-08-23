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
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    import sqlite3

from . import db
from . import email_support
from . import task_cgroup
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
    TextDeltaEvent,
    TextEvent,
    ThinkingDeltaEvent,
    ThinkingEvent,
    ToolEndEvent,
    ToolProgressEvent,
    ToolUseEvent,
    CANONICAL_ROLES,
    is_portable_alias,
    make_brain,
)
from .brain._roles import PORTABLE_KEY
from .brain._fallback import (
    COOLDOWN_STOP_REASONS,
    TRIGGER_STOP_REASONS,
    effective_fallback_kind,
    get_availability_breaker,
)
from .events import EventWriter, random_progress_message
from .skills.calendar import get_caldav_client, get_calendars_for_user
from .skills.whisper.out_of_process import transcribe_audio_out_of_process

logger = logging.getLogger("istota.executor")

# Source types treated as interactive (live user behind the turn): they load
# conversation context, sticky skills, the skills changelog, and personal
# memory. The REPL and web chat are full-stack interactive surfaces like
# talk/email.
_INTERACTIVE_SOURCE_TYPES = ("talk", "email", "repl", "web")


# Distinct (user_id, tz_str) pairs already warned about, so a persistently
# invalid timezone config warns once per process rather than on every task.
_INVALID_TZ_WARNED: set[tuple[str, str]] = set()


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

    An invalid zone name (e.g. the abbreviation ``PDT`` instead of the IANA
    name ``America/Los_Angeles``) logs one WARNING per distinct
    ``(user_id, name)`` so a misconfigured timezone is self-diagnosing rather
    than silently rendering every clock in UTC.
    """
    tz_str = config.resolve_user_timezone(user_id, conn=conn)
    try:
        return ZoneInfo(tz_str), tz_str
    except Exception:
        key = (user_id, tz_str)
        if key not in _INVALID_TZ_WARNED:
            _INVALID_TZ_WARNED.add(key)
            logger.warning(
                "Invalid timezone %r for user %s — falling back to UTC. Use an "
                "IANA name like 'America/Los_Angeles' (abbreviations such as "
                "'PDT'/'PST' are not valid).",
                tz_str, user_id,
            )
        return ZoneInfo("UTC"), "UTC"

# API error detection / retry policy moved into brain.claude_code; re-exported
# here for backward compatibility with callers (scheduler.py) and tests that
# import these symbols from istota.executor. Unused *here* by construction —
# that is what a re-export is — so F401 is silenced rather than obeyed.
from .brain.claude_code import (  # noqa: E402,F401  (kept after module docstring)
    API_ERROR_PATTERN,
    API_RETRY_DELAY_SECONDS,
    API_RETRY_MAX_ATTEMPTS,
    TRANSIENT_STATUS_CODES,
    is_signal_termination,
    is_transient_api_error,
    is_usage_limit_error,
    parse_api_error,
)

# Audio extensions eligible for pre-transcription (matches whisper skill file_types)
_AUDIO_EXTENSIONS = frozenset({"mp3", "wav", "ogg", "flac", "m4a", "opus", "webm", "mp4", "aac", "wma"})

# Wall clock for pre-transcribing *all* of one send's audio, not each file.
# `_pre_transcribe_attachments` runs on a worker thread before the brain is
# called, so `scheduler.task_timeout_minutes` does not cover it and this is the
# only bound there is.
_PRE_TRANSCRIBE_TOTAL_TIMEOUT_SECONDS = 900.0

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
    _NO_FINAL_ANSWER_NOTICE,
    _TERSE_REFERENCE_RE,
    _TERSE_RESULT_MAX_CHARS,
    _TOOL_SYNTAX_PATTERN,
    _TRAILING_REGION_MIN_CHARS,
    _compose_full_result,
    _ensure_final_answer,
    _is_automated_task,
    _is_back_reference,
    _is_terse,
    _last_substantial_region,
    _log_compose_override,
    _text_similarity,
    detect_malformed_result,
    is_no_final_answer,
)


def _pre_transcribe_attachments(
    attachments: list[str] | None,
    prompt: str,
) -> str:
    """Pre-transcribe audio attachments so skill selection sees real text.

    Returns an enriched prompt with transcribed text, or the original prompt
    if no audio attachments or transcription fails.

    The transcript is *appended* to whatever the sender typed rather than
    replacing it: a voice memo can arrive alongside a written message ("have a
    listen and summarize this"), and dropping that half loses the instruction
    the audio was sent under. A send with no typed text (the composer's
    record-and-send) carries only the transcript.

    Each file is transcribed in its own child process. This used to call
    `transcribe_audio` directly, which imported faster-whisper into the daemon
    and left roughly 450 MB per transcription on glibc's free lists that
    nothing ever returned — five voice messages walked the scheduler's RSS from
    820 MB to 2894 MB in four steps, with no sign of stopping (ISSUE-273). See
    `skills/whisper/out_of_process.py` for the measurements.

    The whole loop shares one wall-clock budget rather than giving each file
    its own. This runs on a worker thread *before* the brain call, so nothing
    else bounds it — and a per-file timeout would mean a send carrying five
    audio files could hold the worker for five times the limit, which is the
    stall the timeout exists to prevent rather than a smaller version of it.
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

    deadline = time.monotonic() + _PRE_TRANSCRIBE_TOTAL_TIMEOUT_SECONDS
    transcribed_parts = []
    for audio_path in audio_paths:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Keep what earlier files produced; the prompt is still better with
            # a partial transcript than with none.
            logger.warning(
                "Pre-transcription budget exhausted, skipping %s and any files after it",
                Path(audio_path).name,
            )
            break
        try:
            result = transcribe_audio_out_of_process(audio_path, timeout=remaining)
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
    block = (
        f"Transcribed voice message: {transcribed_text}\n\n"
        f"(Original audio: {filenames})"
    )
    return block if not prompt.strip() else f"{prompt}\n\n{block}"


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


def _resolve_advisor(task, config: Config) -> str:
    """Resolve the ``advisor_model`` for a task, unresolved (alias/raw form).

    A per-task model pin — whatever set it: ``!model``, ``!room model``, a
    ``[[jobs]] model``, an API caller — drops the configured advisor. The CLI's
    advisor gate has two independent checks, and only one is fatal: a *main*
    model that doesn't support the advisor tool at all exits non-zero with no
    result (pin-dependent — this is what a stale pin risks); a capability
    mismatch between two otherwise-advisor-capable models only warns and the
    task still completes. Dropping on any pin sidesteps the fatal case without
    Istota needing to track which models support the advisor tool at all —
    that's the CLI's own catalog. Only the unpinned default path gets the
    configured advisor.
    """
    task_model = (task.model or "").strip()
    if task_model:
        return ""
    return (config.advisor_model or "").strip()


def persist_brain_usage(
    config: Config,
    conn,
    *,
    usage,
    origin: str,
    user_id: str,
    brain_kind: str = "",
    task_id: int | None = None,
    source_type: str = "",
    is_fallback: bool = False,
    model: str = "",
    effort: str = "",
    stop_reason: str = "",
    success: bool = False,
) -> None:
    """Record one brain attempt's token/cost usage. Best-effort throughout.

    ``usage`` is a ``BrainUsage`` or None (``TmuxClaudeBrain`` leaves it None —
    it reconstructs events from a transcript and has no result frame, so a row
    would be a synthetic zero dragging every average).

    ``model`` is the model the attempt actually ran, and it wins over
    ``usage.model``. The two differ where it matters: ``usage.model`` is the
    CLI's cost-weighted dominant model, and it is empty outright for a native
    row, which reports one total with no per-model split. Without this every
    native row would land with no model and Stage 5's per-model grouping would
    bucket the whole native fleet as unknown.

    ``origin`` names the caller: ``task`` for the executor's own path, or the
    daemon call site for the model invocations that have no task at all
    (``sleep_cycle``, ``shared_blocks``, ``health_ocr``, …). Those pass
    ``task_id=None``; without the column they would be invisible in both
    directions — absent from the usage table and absent from any unmeasured-task
    count, because they were never tasks.

    The ``logger.info`` breadcrumb is kept deliberately: it is what leaves a
    figure greppable in the journal when the DB write is the thing that failed.

    Never raises. Telemetry must not turn a completed task into a failed one,
    and the writer's SAVEPOINT means the swallowed case is always a *complete*
    failure rather than a parent with a partial per-model split.
    """
    if usage is None:
        return
    logger.info(
        "brain_usage origin=%s task_id=%s brain=%s model=%s billed_input=%d "
        "cache_read=%d cache_write=%d output=%d cost=%s basis=%s",
        origin, task_id, brain_kind, model or usage.model,
        usage.billed_input_tokens, usage.cache_read_tokens,
        usage.cache_write_tokens, usage.output_tokens,
        round(usage.cost_usd, 6), usage.cost_basis,
    )
    try:
        if conn is not None:
            _insert_usage_row(
                conn, usage=usage, origin=origin, user_id=user_id,
                brain_kind=brain_kind, task_id=task_id, source_type=source_type,
                is_fallback=is_fallback, model=model, effort=effort,
                stop_reason=stop_reason, success=success,
            )
        else:
            with db.get_db(config.db_path) as usage_conn:
                _insert_usage_row(
                    usage_conn, usage=usage, origin=origin, user_id=user_id,
                    brain_kind=brain_kind, task_id=task_id,
                    source_type=source_type, is_fallback=is_fallback,
                    model=model, effort=effort, stop_reason=stop_reason,
                    success=success,
                )
    except Exception:
        logger.warning(
            "failed to persist usage (origin=%s task=%s) — spend not recorded",
            origin, task_id, exc_info=True,
        )


def _insert_usage_row(conn, **kwargs) -> None:
    db.insert_task_usage(conn, **kwargs)


def _persist_task_usage(
    config: Config,
    conn,
    task_id: int,
    usage,
    *,
    user_id: str = "",
    source_type: str = "",
    brain_kind: str = "",
    is_fallback: bool = False,
    model: str = "",
    effort: str = "",
    stop_reason: str = "",
    success: bool = False,
) -> None:
    """The task-shaped wrapper over `persist_brain_usage`."""
    persist_brain_usage(
        config, conn, usage=usage, origin="task", user_id=user_id,
        brain_kind=brain_kind, task_id=task_id, source_type=source_type,
        is_fallback=is_fallback, model=model, effort=effort,
        stop_reason=stop_reason, success=success,
    )


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


# --- Brain fallback (availability failover) --------------------------------
# Generalizes the old tmux→claude_code in-attempt fallback so an operator can
# configure any brain as a fallback for any primary, triggered when the primary
# is unavailable (usage limit / missing binary / tmux launch failure). Stays at
# the executor level: brains have no Config (needed for the operator alert), and
# the same-attempt/no-increment rerun already lives here.


def config_alias_portable_names(config) -> set[str]:
    """The portable alias names for the cross-brain fallback check.

    The canonical tiers (``fast``/``general``/``smart``) plus any custom alias an
    operator flagged ``portable = true`` in ``[models.aliases]``. A shortcut
    (``opus``) or canonical id is deliberately absent — it pins one provider and
    can't cross the boundary. Derived from the config's raw alias mapping so it's
    independent of global load-order state.
    """
    def _truthy(raw):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"true", "1", "yes", "on"}
        return False

    names = set(CANONICAL_ROLES)
    aliases = getattr(getattr(config, "models", None), "aliases", None) or {}
    for name, value in aliases.items():
        if isinstance(value, Mapping):
            for key, raw in value.items():
                if str(key).lower() == PORTABLE_KEY and _truthy(raw):
                    names.add(str(name).strip().lower())
    return names


def _resolve_fallback_model_effort(task, config, fallback_brain, effort):
    """Resolve (model, effort, dropped_pin) for a fallback brain run.

    A portable intent (tier fast/general/smart + operator ``portable = true``
    custom aliases) is re-resolved in the fallback brain's namespace — the intent
    crosses the provider boundary. A non-portable pin (provider shortcut /
    canonical ID) can't cross, so the fallback uses its own default and the
    requested name is returned as ``dropped_pin`` (for the visible note + the
    INFO log). An empty requested model → the fallback's own default, no note.
    """
    raw = (task.model or "").strip() or config.model
    if not raw:
        return ("", effort, None)
    if is_portable_alias(raw, config_alias_portable_names(config)):
        # Re-resolve the intent in the fallback brain's own namespace, carrying
        # its effort too — a customized ``smart`` falling back claude_code→native
        # must land on a valid openai_compat slug + effort, not the anthropic
        # value. Fall back to the id-only path defensively if the pair is empty.
        pair = fallback_brain.resolve_alias(raw)
        if pair and pair[0]:
            return (pair[0], pair[1] or effort, None)
        return (fallback_brain.resolve_model_name(raw), effort, None)
    logger.info(
        "fallback_model: non-portable %r dropped; using fallback brain default", raw
    )
    return ("", effort, raw)


# Prefixed onto a fallback failure text when the fallback was *also* unavailable,
# so the delivery layer can say "both brains are down" instead of echoing a raw
# provider error at the user (ISSUE-212). A marker rather than a formatted
# sentence because the executor's return contract is a plain string — the
# scheduler owns the user-facing wording, and the underlying cause stays in the
# text for the logs and the friendly formatter.
FALLBACK_EXHAUSTED_MARKER = "[brain-fallback-exhausted]"

# The fallback's own stop_reasons that mean "I was unavailable too", as opposed
# to a task-level outcome (timeout / oom / cancelled) where "both unavailable"
# would be the wrong thing to tell the user.
# `not_found` is deliberately absent: it means the fallback brain's binary isn't
# installed. That is an operator misconfiguration, and telling the user to "try
# again shortly" would be false — it will never resolve on its own. It flows
# through the ordinary failure path so the real cause stays visible.
_FALLBACK_UNAVAILABLE_REASONS = frozenset(
    {"usage_limit", "fallback", "transient_api_error"}
)


def _run_fallback(config, brain_config, fallback_kind, task, req, *, on_start=None):
    """Construct the fallback brain and run the same attempt through it.

    Returns ``(BrainResult | None, dropped_pin, effort_used)``. A ``None``
    result means the fallback brain couldn't be constructed (misconfig) — the
    caller keeps the primary's result and flows through the normal path. Never
    raises: an unexpected exception in the fallback brain becomes a failed
    BrainResult.

    ``effort_used`` is returned because the fallback re-resolves model *and*
    effort in its own namespace, so the request's original effort does not
    describe the attempt that ran — recording it on the usage row would name a
    setting the fallback never used.

    ``on_start(model, dropped_pin)`` fires once the request is resolved and
    immediately before the fallback brain runs (ISSUE-278). It exists so the
    user-facing notice lands *in* the silence rather than after it: the fallback
    run is the long part, and a notice emitted on the way out would arrive at
    the end of the same wait it is there to explain. It is called only on the
    path where a fallback actually runs — a construction failure returns above
    it, since no substitution took place to report.
    """
    import dataclasses as _dc

    from .brain import BrainResult

    try:
        fb_config = _dc.replace(brain_config, kind=fallback_kind)
        if fallback_kind == "native":
            fb_config = _dc.replace(
                fb_config,
                native=_native_with_user_key(fb_config.native, config, task.user_id),
            )
        fb_brain = make_brain(fb_config)
    except Exception as e:  # noqa: BLE001 — misconfigured nested block
        logger.warning("brain fallback: could not construct %s: %s", fallback_kind, e)
        return None, None, ""

    fb_model, fb_effort, dropped_pin = _resolve_fallback_model_effort(
        task, config, fb_brain, req.effort
    )
    # An advisor pairing can only be right for the model it was resolved
    # against. anthropic->native drops it (mirrors the non-portable-pin drop
    # above — NativeBrain has no wire for it anyway); a dropped_pin also
    # drops it — a non-portable config.model pin means the fallback runs on
    # its own default model instead, and the advisor was never evaluated
    # against that. anthropic->anthropic with the pin intact keeps it, since
    # the same pairing carries over to the fallback too.
    fb_advisor = (
        req.advisor
        if fb_brain.model_namespace == "anthropic" and dropped_pin is None
        else ""
    )
    fb_req = _dc.replace(req, model=fb_model, effort=fb_effort, advisor=fb_advisor)
    if on_start is not None:
        try:
            on_start(fb_model, dropped_pin)
        except Exception:
            # The notice is cosmetic; the reroute is not. A surface that throws
            # must never cost the user the answer.
            logger.debug("brain fallback notice failed", exc_info=True)
    try:
        return _mark_if_exhausted(fb_brain.execute(fb_req)), dropped_pin, fb_effort
    except Exception as e:  # noqa: BLE001 — brains shouldn't raise, but be safe
        logger.exception("brain fallback: fallback brain %s raised", fallback_kind)
        return (
            BrainResult(
                success=False,
                result_text=f"Fallback execution error: {e}",
                stop_reason="error",
            ),
            dropped_pin,
            fb_effort,
        )


def _mark_if_exhausted(fb_result):
    """Tag a fallback result that failed for *availability* reasons.

    Both brains being unavailable is the one case the user must be told about
    plainly — the alternative is delivering whatever raw provider error the
    fallback produced. A task-level failure (timeout / oom / cancelled) is left
    alone: it isn't an availability problem and the normal wording applies.
    """
    import dataclasses as _dc

    if fb_result.success or fb_result.stop_reason not in _FALLBACK_UNAVAILABLE_REASONS:
        return fb_result
    logger.error(
        "brain fallback: fallback brain also unavailable (reason=%s)",
        fb_result.stop_reason,
    )
    return _dc.replace(
        fb_result,
        result_text=f"{FALLBACK_EXHAUSTED_MARKER} {fb_result.result_text}".strip(),
    )


def _fire_fallback_alert(config, task, primary_kind, fallback_kind, reason):
    """One operator alert when the availability breaker opens for a primary."""
    try:
        from . import notifications

        notifications.send_notification(
            config,
            task.user_id,
            f"⚠️ {primary_kind} brain unavailable ({reason}) — falling back to "
            f"{fallback_kind} for {config.brain.fallback_cooldown_seconds}s. "
            f"The primary will be probed again after the cooldown.",
            purpose="alert",
        )
    except Exception:
        logger.debug("brain fallback alert failed", exc_info=True)


def _append_model_note(result_text, dropped_pin, primary_kind, actual_model):
    """Append a single italic note when a non-portable pin was dropped on a
    successful fallback, so the user isn't silently given a different model.

    Pure string→string (no I/O); part of ``result_text`` so it delivers
    uniformly across every surface and persists with the result.
    """
    model_str = actual_model or "a different model"
    # Italicize only the prose runs — the emoji and the `code` spans stay
    # outside emphasis so they render upright (a single wrapping `_…_` would
    # inherit italics onto the emoji and the model IDs). Asterisk emphasis
    # (not underscore) because `primary_kind`/model IDs can contain `_`
    # (e.g. `claude_code`), which would confuse underscore delimiters.
    note = f"⚠️ *Ran on* `{model_str}` *(*`{dropped_pin}` *unavailable).*"
    return f"{result_text}\n\n{note}"


# Plain-language readings of the stop_reasons that open a fallback, for the
# user-facing notice. `cooldown` is not a brain stop_reason — it's the executor's
# own name for the breaker-open path, where no primary call was made at all.
_FALLBACK_REASON_PHRASES = {
    "usage_limit": "its usage limit was reached",
    "not_found": "its CLI is not installed",
    "transient_api_error": "the provider returned an error",
    "fallback": "it could not start",
}


def fallback_notice_text(primary_kind, reason, fallback_kind, model, dropped_pin) -> str:
    """The sentence every stream surface shows when a fallback is taken.

    Composed here, not per surface: the web transcript and the REPL render
    ``payload["text"]`` verbatim, so the wording lives in one place and two
    surfaces can't drift apart. Pure string→string, no I/O.

    ``model`` is what the fallback was *asked* for, which is empty whenever it
    runs on its own default — the case ``dropped_pin`` describes. The notice
    names the pin then rather than inventing a model name, because the model
    that actually ran is not known until the run returns (the terminal ``done``
    event carries it).
    """
    if reason == "cooldown":
        lead = f"`{primary_kind}` is cooling down after a recent failure."
    else:
        phrase = _FALLBACK_REASON_PHRASES.get(reason, reason)
        lead = f"`{primary_kind}` is unavailable — {phrase}."
    if dropped_pin:
        return (
            f"{lead} Continuing on `{fallback_kind}`, which cannot use the "
            f"pinned `{dropped_pin}`, so its default model runs instead."
        )
    if model:
        return f"{lead} Continuing on `{fallback_kind}` with `{model}`."
    return f"{lead} Continuing on `{fallback_kind}`."


def _build_native_completer(native_config, timeout: float, *, on_usage=None):
    """A `prompt -> raw_output | None` one-shot completer over the native provider.

    Conversation-context triage on a native deployment, so the native brain runs
    it through its own provider/model instead of shelling out to the `claude`
    CLI it isn't using.

    Returns None if the provider can't be built (e.g. missing key / bad config),
    so the caller skips the brain-aware path rather than mis-routing to the CLI.

    ``on_usage`` (ISSUE-272) receives what the turn spent, in the same shared
    vocabulary the CLI path reports. Without it this path returned only
    ``AssistantMessage.text`` and dropped the ``usage`` sitting on the same
    object — so a native deployment's triage was as unmeasured as a
    claude_code one, for a different reason. Reported on a failed turn too:
    a turn that reached the provider and then errored still spent tokens.
    """
    try:
        from istota.llm import make_provider
        from istota.llm.oneshot import make_message_completer

        provider = make_provider(native_config)
        # Generous output budget: a JSON id array is short, but reasoning
        # models burn tokens thinking first and would otherwise return empty.
        completer = make_message_completer(
            provider, native_config.model, max_tokens=4096
        )
    except Exception:
        logger.warning(
            "native triage completer setup failed; skipping brain-aware triage",
            exc_info=True,
        )
        return None

    def _classify(prompt: str) -> str | None:
        message = completer(prompt, timeout=timeout)
        if message is None:
            return None
        if on_usage is not None:
            _report_native_usage(on_usage, message, native_config.model)
        # An `error` turn carries an error message where the answer would be;
        # returning it would feed prose to a JSON parser. None is the fail-open
        # signal the callers already handle.
        if message.stop_reason == "error":
            return None
        return message.text

    return _classify


def _report_native_usage(on_usage, message, requested_model: str) -> None:
    """Convert one native turn's usage to the shared vocabulary and report it.

    Never raises: telemetry must not turn a working triage into a fail-open one.

    ``cost_reported`` follows the same conservative reading the native brain
    uses — True only when the provider returned a cost of its own. The catalog
    prices an unknown model at zero, so without the distinction a
    direct-Anthropic or local deployment would write a fabricated `0.0` labelled
    as real spend.

    **A turn that measured nothing writes no row.** Every ``StreamError`` site in
    ``llm/openai_compat.py`` builds a fresh ``AssistantMessage`` with a default
    ``Usage()``, so a failed native turn carries zeros rather than what it spent.
    Reporting those would write ``has_totals=1`` rows of pure zero — and every
    token aggregate filters on ``has_totals``, so during a provider outage they
    would arrive in bulk and drag this origin's per-call averages toward zero
    while inflating its measured-call count. Zeros here mean "not measured", not
    "free". This mirrors the CLI half, where `_parse_simple_json_output` returns
    no usage for an unparseable attempt and `_report_triage_usage` returns
    early; without the check the two halves disagree, invisibly, behind one sink.

    A provider-reported cost is kept even at zero tokens: that is the provider
    saying the turn was free, which is a measurement.
    """
    try:
        from istota import usage as usage_types
        from istota.llm.catalog import get_model_info
        from istota.session.usage import TaskUsage

        if message.usage.total_tokens == 0 and message.usage.cost_usd is None:
            return

        model = message.model or requested_model
        accumulated = TaskUsage()
        accumulated.add(message.usage, get_model_info(model))
        on_usage(
            usage_types.from_task_usage(
                accumulated, cost_reported=message.usage.cost_usd is not None
            ),
            model=model,
            brain_kind="native",
            stop_reason=message.stop_reason,
            success=message.stop_reason != "error",
        )
    except Exception:
        logger.warning("native triage usage sink failed", exc_info=True)


def _native_web_fetch_enabled(task: "db.Task", config: Config) -> bool:
    """True when this task routes to the native brain with WebFetch enabled.

    Used to fold `untrusted_input` into the eager skill set — the native
    WebFetch tool ingests untrusted web content but, as a core tool, doesn't
    trigger the companion-skill machinery that surfaces that guidance for
    ingest *skills*.
    """
    from .brain import resolve_brain_kind

    try:
        routed = resolve_brain_kind(task.source_type, config.brain)
    except Exception:  # noqa: BLE001 — never let routing lookup fail selection
        return False
    if routed.kind != "native":
        return False
    wf = getattr(routed.native, "web_fetch", None)
    return bool(wf and wf.enabled)


def _build_triage_completer(task: "db.Task", config: Config):
    """Conversation-context triage completer, routed through the task's brain.

    Per-source-type brain routing decides the transport:
    - claude_code (and tmux) → None, so context triage uses the `claude` CLI.
    - native → a native provider completer. If it can't be built (missing key /
      bad config), returns a completer that always yields None so triage fails
      open (includes all older messages) instead of shelling out to the `claude`
      CLI the native brain isn't using.

    The completer carries its own usage sink, because it is the object that
    performs the inference on this path (ISSUE-272). The CLI path's sink is
    passed separately — see ``_build_triage_usage_sink``.
    """
    from .brain import resolve_brain_kind

    routed = resolve_brain_kind(task.source_type, config.brain)
    if routed.kind != "native":
        return None

    native = _native_with_user_key(routed.native, config, task.user_id)
    completer = _build_native_completer(
        native,
        config.conversation.selection_timeout,
        on_usage=_build_triage_usage_sink(task, config),
    )
    if completer is None:
        return lambda _prompt: None
    return completer


def _build_triage_usage_sink(task: "db.Task", config: Config):
    """Record one conversation-context triage inference as a `task_usage` row.

    ``origin="context_triage"``, and **no ``task_id``** — the same shape the
    other task-less origins use. A triage inference is not one of the task's own
    attempts, and a row carrying the id would take an ``attempt_seq`` in that
    task's sequence, which is meant to count brain attempts. ``user_id`` and
    ``source_type`` are available here (unlike the ownerless sleep-cycle pass),
    so the row is still attributable.

    Opens its own short connection (``conn=None``): prompt assembly holds no
    write transaction, so there is no caller connection to reuse.
    """
    def _sink(usage, *, model="", brain_kind="", stop_reason="", success=False):
        persist_brain_usage(
            config, None, usage=usage, origin="context_triage",
            user_id=task.user_id or "", source_type=task.source_type or "",
            brain_kind=brain_kind, model=model,
            stop_reason=stop_reason, success=success,
        )

    return _sink


# Credential-related env var patterns to strip from subprocess environments
_CREDENTIAL_ENV_PATTERNS = frozenset({
    "PASSWORD", "SECRET", "TOKEN", "API_KEY",
    "APP_PASSWORD", "NC_PASS", "PRIVATE_KEY",
})

#: Shell startup files, stripped by exact name rather than by substring.
#:
#: Cron ``command:`` rows and heartbeat shell commands run under bash now
#: (``shell_exec.shell_argv``) where they used to run under ``/bin/sh``. Bash
#: sources ``$BASH_ENV`` for a *non-interactive* shell and dash sources nothing,
#: so a value inherited from the daemon's own environment would newly execute
#: before every one of those commands. Not reachable by a task or by the model —
#: it needs control of the daemon's environment — but it is a capability the
#: previous interpreter did not have, so it goes.
#:
#: ``ENV`` is deliberately **not** here. POSIX shells read it only for
#: *interactive* shells, and bash invoked as ``bash -c`` is not in POSIX mode
#: and reads ``BASH_ENV`` instead — so stripping it would buy nothing and would
#: break an operator whose command reads ``$ENV`` as a deployment name.
#:
#: Exact match, because these go through a substring test above: ``ENV`` as a
#: substring would strip most of the environment.
_SHELL_STARTUP_ENV_VARS = frozenset({"BASH_ENV"})

_bwrap_checked: bool | None = None

#: Whether this host's bwrap needs ``--unshare-user`` spelled out.
#:
#: Set by ``_bwrap_available`` and read through ``_bwrap_requires_unshare_user``.
#: False until the probe has run and found otherwise, so a host where the plain
#: probe already succeeds is left exactly as it was.
_bwrap_needs_unshare_user: bool = False

#: The mount operations the availability probe performs, which are the ones
#: ``build_bwrap_cmd`` performs unconditionally on every sandbox it builds.
#:
#: **A probe that answers for less than the command it gates is not a probe.**
#: This used to be `--ro-bind / /` alone, which asks only whether the kernel
#: will hand out a mount namespace — and a container can answer yes to that and
#: still refuse `mount("proc")` inside it, because Docker's masked `/proc`
#: entries and read-only `/proc/sys` make the container's procfs not "fully
#: visible" and the kernel then blocks a fresh procfs in a nested user
#: namespace. Measured on the shipped image: with `seccomp:unconfined` alone,
#: `bwrap --unshare-user --ro-bind / / -- true` exits 0 and the same command
#: with these mounts exits 1 at "Can't mount proc on /newroot/proc". A daemon
#: that trusted the narrow probe there would report a working sandbox, set
#: ``ISTOTA_SANDBOXED``, and then fail every task — which is worse than the
#: silent fallback it replaced.
#:
#: `tests/test_sandbox_db_isolation.py` holds this against the real argv, so a
#: mount added to `build_bwrap_cmd`'s unconditional set and not to this one
#: fails in the default suite rather than on somebody's host.
_BWRAP_PROBE_MOUNTS = [
    "--unshare-pid", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
]

#: The whole probe argv: something to be a root, plus the mounts above.
#:
#: The root bind is the probe's own scaffolding and is deliberately *not* in
#: the list above. `build_bwrap_cmd` binds selectively — `/usr`, a handful of
#: `/etc` entries, the task's own workspace — and never `--ro-bind / /`, so a
#: guard that required the real argv to contain the probe's root would be
#: asserting the sandbox is broader than it is.
_BWRAP_PROBE_ARGS = ["--ro-bind", "/", "/", *_BWRAP_PROBE_MOUNTS]


def _release_task_cgroup(task_id: int, path: Path) -> None:
    """Give a task's cgroup back, naming an OOM kill on the way out (A6).

    ``memory.events`` has to be read *before* ``rmdir``, because the counters
    go with the directory. Without this the feature's first visible effect on a
    real host is that builds and test suites which used to pass start failing,
    reported as an opaque killed child with nothing anywhere mentioning a cap —
    a task told it was killed, and an operator with no way to find out why.

    Both limits that can end a task are named, not just memory. A task that
    hits ``pids.max`` gets ``fork: Resource temporarily unavailable`` out of
    whatever it was running, which mentions no cgroup and reads like a broken
    toolchain; ``pids.events``'s ``max`` counter is the only place that says
    otherwise. Neither counter moved before placement worked (ISSUE-285) — the
    cgroup held one sleeping process, so nothing in it ever reached a limit.

    Never raises: this runs from an ``ExitStack`` callback on the task's exit
    path, where an exception would replace the task's real result with this
    one's.
    """
    try:
        events = task_cgroup.read_events(path)
        if events.get("oom_kill"):
            logger.warning(
                "task %d: %d process(es) OOM-killed inside the task's own cgroup "
                "(scheduler.task_memory_max_mb) — the task exceeded its memory "
                "cap; the rest of the host was unaffected",
                task_id, events["oom_kill"],
            )
        pids_events = task_cgroup.read_events(path, "pids.events")
        if pids_events.get("max"):
            logger.warning(
                "task %d: %d fork(s) refused by the task's own cgroup "
                "(scheduler.task_pids_max) — the task hit its process limit and "
                "will have reported it as a fork failure",
                task_id, pids_events["max"],
            )
        task_cgroup.destroy(path)
    except Exception:  # noqa: BLE001
        logger.debug("task %d: cgroup cleanup failed", task_id, exc_info=True)


def _bwrap_available() -> bool:
    """Check if bwrap can create namespaces (cached after first call).

    Returns False on non-Linux, when bwrap is not installed, or where the
    kernel refuses the namespaces bwrap needs.

    **Probed twice, and the second probe is the one that matters as root.**
    bwrap only forces ``CLONE_NEWUSER`` on itself when it is neither setuid nor
    running as uid 0 — so an unprivileged daemon gets a user namespace whether
    or not anybody asked, and a daemon running as *root without CAP_SYS_ADMIN*
    does not. The plain probe then fails at ``unshare(CLONE_NEWNS)`` with
    "Creating new namespace failed: Operation not permitted", the whole sandbox
    is disabled for the process, and every task runs unconfined behind one
    warning line. That is what every task in a Docker deployment was doing:
    measured inside the shipped image, ``bwrap --ro-bind / / -- true`` exits 1
    and ``bwrap --unshare-user --ro-bind / / -- true`` exits 0.

    So a failed plain probe is retried with ``--unshare-user`` spelled out, and
    the answer is remembered — `build_bwrap_cmd` and `_bwrap_supports` both
    have to pass the same flag, or they would build an argv the probe never
    tested. Order matters: the plain probe runs first, so a host where the
    sandbox already worked is answered by exactly the command it was answered
    by before and nothing about it changes.

    Both probes carry `_BWRAP_PROBE_ARGS`, which performs the unconditional
    mount set `build_bwrap_cmd` emits rather than the bare root bind this
    used to ask about. See `_BWRAP_PROBE_MOUNTS` for why the narrower
    question has a wrong answer on a container.
    """
    global _bwrap_checked, _bwrap_needs_unshare_user
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

    def _probe(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, timeout=5)

    try:
        plain = _probe(["bwrap", *_BWRAP_PROBE_ARGS, "--", "true"])
        if plain.returncode == 0:
            _bwrap_checked = True
            return True

        explicit = _probe(
            ["bwrap", "--unshare-user", *_BWRAP_PROBE_ARGS, "--", "true"]
        )
        if explicit.returncode == 0:
            _bwrap_needs_unshare_user = True
            _bwrap_checked = True
            logger.info(
                "bwrap needs --unshare-user spelled out on this host (it "
                "declines to unshare the user namespace on its own as uid 0); "
                "adding it. The plain probe said: %s",
                plain.stderr.decode(errors="replace").strip(),
            )
            return True

        _bwrap_checked = False
        logger.warning(
            "Sandbox skipped: bwrap namespace creation failed both without and "
            "with --unshare-user (kernel without unprivileged user namespaces, "
            "or a container blocking the syscall): %s / %s",
            plain.stderr.decode(errors="replace").strip(),
            explicit.stderr.decode(errors="replace").strip(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Sandbox skipped: bwrap probe failed: %s", exc)
        _bwrap_checked = False
    return _bwrap_checked


def _bwrap_requires_unshare_user() -> bool:
    """Whether every bwrap argv on this host has to carry ``--unshare-user``.

    Calls `_bwrap_available` rather than reading the global directly, because
    the global is only meaningful once the probe has run and callers reach this
    from several places that may each be first.
    """
    if not _bwrap_available():
        return False
    return _bwrap_needs_unshare_user


_bwrap_flag_support: dict[str, bool] = {}
_bwrap_probe_lock = threading.Lock()


def effective_sandboxing(config: Config) -> bool:
    """Whether the filesystem sandbox is actually in place for this deployment.

    ``sandbox_enabled`` is what the operator asked for; this is what they got.
    Four shapes run the model with the daemon's own filesystem access: the
    standalone install, which ships ``sandbox_enabled = false``; and — despite
    the flag being set — a container where the bwrap probe fails (no
    CAP_SYS_ADMIN, and that one is multi-user), a Linux host with no bwrap
    installed, and any non-Linux host.

    Named because four call sites need the same answer and three of them used
    to spell it out inline, two under comments calling it "*effective*
    sandboxing" without there being anything of that name to point at. One of
    those sites decides how the prompt describes the database boundary to the
    model, so a definition that drifted between them would have the daemon
    telling the model the databases are masked on a deployment where they are
    not — and the code there is explicit that a false boundary claim is worse
    than no claim at all.

    Consults the bwrap capability probe, which shells out once per process and
    caches. That is why prompt assembly reaches `subprocess` at all (ISSUE-308).

    Not every site that mentions both halves wants this predicate. The
    scheduler's startup warning (`scheduler.py`) reads ``sandbox_enabled and
    not _bwrap_available()`` — "asked for it, didn't get it", which is
    ``sandbox_enabled and not effective_sandboxing(config)`` and not the plain
    negation. Collapsing it to ``not effective_sandboxing(config)`` would fire
    an unsupported-configuration warning on every standalone install, which
    ships the flag off deliberately.
    """
    return bool(config.security.sandbox_enabled and _bwrap_available())


def _bwrap_supports(flag: str, probe_args: list[str]) -> bool:
    """Whether this bwrap accepts *flag*, probed once per process.

    Probed rather than assumed: passing an unsupported flag makes bwrap exit
    non-zero *before* it runs anything, which would fail every task on an older
    host. ``probe_args`` is the whole argv the flag needs, companion flags
    included — bwrap rejects `--disable-userns` without `--unshare-user`, and a
    probe missing one reports "unsupported" on a host that supports it fine.

    A probe that could not run is logged loudly and a rejection quietly: the
    first is an unexplained loss of hardening, the second is an old bwrap saying
    what it is. Either way the answer is cached, so a failure to probe silently
    turns the flag off for the process — the reason the result is not trusted
    for anything but hardening. Locked because scheduler workers build their
    first sandbox concurrently, and an unlocked probe is N subprocesses and N
    log lines for one question.

    ``--unshare-user`` is prepended where `_bwrap_available` found the host
    needs it, for the same reason that function retries with it: without the
    flag *every* probe here fails at namespace creation rather than at the flag
    under test, so a supported flag reports unsupported. Fixing
    `_bwrap_available` alone would have left that true — the sandbox would have
    started and ``--remount-ro`` would have reported unsupported, leaving the
    database masks writable, which is the one thing the read-only mask exists
    to prevent.

    Through `_bwrap_requires_unshare_user` rather than the global, so the two
    places that have to agree about this flag agree through one accessor.
    """
    with _bwrap_probe_lock:
        cached = _bwrap_flag_support.get(flag)
        if cached is not None:
            return cached
        if not _bwrap_available():
            _bwrap_flag_support[flag] = False
            return False

        if _bwrap_requires_unshare_user() and "--unshare-user" not in probe_args:
            probe_args = ["--unshare-user", *probe_args]

        supported = False
        try:
            result = subprocess.run(
                ["bwrap", *probe_args, "--", "true"],
                capture_output=True, timeout=5,
            )
            supported = result.returncode == 0
            if not supported:
                logger.info(
                    "bwrap rejected %s: %s", flag,
                    result.stderr.decode(errors="replace").strip(),
                )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(
                "bwrap probe for %s could not run (%s); treating it as "
                "unsupported for the rest of this process", flag, exc,
            )
        _bwrap_flag_support[flag] = supported
        return supported


def _bwrap_supports_disable_userns() -> bool:
    """Whether this bwrap accepts ``--disable-userns`` (added in 0.8).

    Matters because the deployment enables ``kernel.unprivileged_userns_clone``
    — bwrap needs it — which also lets the sandboxed process `unshare -Urm`
    into a nested namespace where it holds CAP_SYS_ADMIN and can `umount` one
    of our masks, revealing whatever was bound underneath. With nothing bound
    under the database directories that reveals nothing, but the masks exist
    precisely to survive a future broad ``sandbox_ro_paths`` entry, and that is
    the case where lifting them would matter. ``--disable-userns`` blocks the
    nested namespace outright.

    Probed rather than assumed: passing an unsupported flag makes bwrap exit
    non-zero, which would fail every task on an older host.

    The probe carries ``--unshare-user`` because bwrap refuses the pair without
    it ("--disable-userns requires --unshare-user", exit 1) — so a probe without
    it answers "unsupported" on every host, which is what it did from the flag's
    introduction until this was found. `build_bwrap_cmd` emits the two together
    for the same reason. Unprivileged bwrap unshares the user namespace anyway,
    so on the supported deployment the companion flag changes nothing on its own.
    """
    already_probed = "--disable-userns" in _bwrap_flag_support
    supported = _bwrap_supports(
        "--disable-userns",
        ["--unshare-user", "--ro-bind", "/", "/", "--disable-userns"],
    )
    if not supported and not already_probed and _bwrap_available():
        logger.info(
            "bwrap does not support --disable-userns; sandbox masks can be "
            "lifted from a nested user namespace. Keep sandbox_ro_paths narrow."
        )
    return supported


def _bwrap_supports_remount_ro() -> bool:
    """Whether this bwrap accepts ``--remount-ro`` (added in 0.2).

    The database masks are read-only so that a probe against a path that is no
    longer in the namespace fails at open time instead of quietly creating a
    zero-byte file on the mask's tmpfs — which then answers `no such table` and
    reads as a corrupt database rather than as a boundary, and litters the
    directory for the rest of the task.

    Old enough that every supported host has it, but probed all the same: the
    cost of being wrong is every task failing, against a cosmetic gain.
    """
    already_probed = "--remount-ro" in _bwrap_flag_support
    supported = _bwrap_supports(
        "--remount-ro",
        ["--ro-bind", "/", "/", "--tmpfs", "/tmp", "--remount-ro", "/tmp"],
    )
    if not supported and not already_probed and _bwrap_available():
        logger.info(
            "bwrap does not support --remount-ro; the database masks stay "
            "writable and a stray file written there will look like a database."
        )
    return supported


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
    # USER/LOGNAME are process-identity basics, not secrets. The macOS login
    # Keychain lookup the `claude` CLI uses to find its OAuth credential needs
    # them — without them a stripped-env `claude -p` reports "Not logged in"
    # even though the interactive CLI is authenticated (the standalone/local
    # install's default brain). Harmless on Linux, where the credential is a
    # file under HOME.
    for identity_key in ("USER", "LOGNAME"):
        identity_val = os.environ.get(identity_key)
        if identity_val is not None:
            env[identity_key] = identity_val
    for key in config.security.passthrough_env_vars:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    # Pass through Claude Code auth token if present.
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    # Propagate the admins-file path (a path, not a secret) so subprocess
    # config loads — the feeds/money skill facades call load_config() —
    # resolve the namespace-correct admins file instead of the hardcoded
    # /etc/istota default. Unset on non-namespace deploys; harmless to omit.
    admins_file = os.environ.get("ISTOTA_ADMINS_FILE")
    if admins_file:
        env["ISTOTA_ADMINS_FILE"] = admins_file
    # Propagate the config file actually loaded so subprocess load_config()
    # calls — the on-demand `skills` loader re-applies disabled/admin/
    # experimental guards from a fresh load_config(), and the feeds/money
    # facades load config too — resolve the SAME file the daemon used rather
    # than falling back to the default search order. Without this a daemon
    # started with `-c /custom/path` would re-apply guards from a different
    # config than the one that built the catalogue. Mirrors the scheduler's
    # command/skill-task env builders.
    if config.config_path is not None:
        env["ISTOTA_CONFIG_PATH"] = str(config.config_path)
    return env


def build_model_cli_env(config: Config) -> dict[str, str]:
    """Build the env for a daemon-side `claude` spawn that is not a task.

    The context-triage completion and the healthcheck execution test both
    shell out to `claude` outside `BrainRequest`, so neither gets the
    per-task env `execute_task()` assembles. They still need exactly what
    `build_clean_env` allowlists plus the CLI's own credential: the OAuth
    token comes through `build_clean_env`, and `ANTHROPIC_API_KEY` is
    inherited here so an API-key deployment authenticates too. Everything
    else in the daemon environment — the master Fernet key, the Nextcloud
    app password, every configured service token — stays out.
    """
    env = build_clean_env(config)
    if not env.get("ANTHROPIC_API_KEY"):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
    return env


def build_stripped_env() -> dict[str, str]:
    """Build os.environ minus credential vars. For heartbeat/cron commands.

    Phase 1.4 of the unified credential resolution refactor: the master
    Fernet key (``ISTOTA_SECRET_KEY``) is no longer preserved here. Skill
    subprocesses that need per-user encrypted secrets get them
    pre-resolved via the manifest ``env:`` blocks.

    ``PRECOMMIT_SCANS_REQUIRED`` is added rather than filtered (ISSUE-291).
    The repository's pre-commit hook refuses a commit whose secret scan could
    not run, but only where it can tell nobody is watching, and the markers it
    reads for that — ``ISTOTA_SANDBOXED``, ``DEVELOPER_REPOS_DIR`` — are built
    per task by ``build_claude_env``. This env is the daemon's own, so a cron
    ``command`` job or a heartbeat shell command carries neither and would be
    read as a human at a terminal. It is as unattended as any of them, so it
    says so.
    """
    env = {
        k: v for k, v in os.environ.items()
        if not any(p in k.upper() for p in _CREDENTIAL_ENV_PATTERNS)
        and k.upper() not in _SHELL_STARTUP_ENV_VARS
    }
    env["PRECOMMIT_SCANS_REQUIRED"] = "1"
    return env


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


# Reserved key a setup_env hook may return to prepend entries to the *model's*
# PATH. os.pathsep-separated. Consumed and dropped by execute_task; never
# merged into ``env`` and never handed to the skill proxy — see the
# application site for why that distinction is load-bearing.
HOOK_PATH_PREPEND_KEY = "ISTOTA_PATH_PREPEND"


# --- Network proxy allowlist ---

_DEFAULT_NETWORK_HOSTS = frozenset({
    "api.anthropic.com:443",
    "mcp-proxy.anthropic.com:443",
})

_PYPI_HOSTS = frozenset({
    "pypi.org:443",
    "files.pythonhosted.org:443",
})

# Package registries reached by an install inside a developer worktree
# (ISSUE-304). Gated on the developer skill rather than a config flag of their
# own: the registries arrive with the skill, which is already opt-in through
# ``developer.enabled``.
#
# **Authorized, not selected**, and the difference is worth stating because the
# two read alike. ``authorized_skills`` is the union of the selected skills and
# the ones ``derive_authorized_skills`` auto-authorizes on credential presence,
# and ``developer`` auto-authorizes as soon as *either* forge token resolves.
# So on a deployment where the user has configured a GitLab or GitHub token,
# these hosts are on the allowlist for every one of that user's tasks — a Talk
# reply, a cron job, a briefing — and not only for tasks that chose the skill.
# That is the same gate the forge hosts have always ridden, deliberately (the
# symmetry `derive_authorized_skills` exists for), so this adds reach to an
# existing door rather than opening a new one. It is a door onto a registry
# anyone may publish to, which is the argument this file uses below to refuse
# ``*.blob.core.windows.net`` — the difference is that a package registry is
# what an install *is*, and ``allow_pypi`` already concedes the same property
# deployment-wide and on by default.
#
# Every hostname here was measured through a logging CONNECT proxy that
# permitted everything and recorded each target, the same method that
# established the GitHub Actions log host above. A guessed name fails silently
# at the boundary and reads as a broken install, because the proxy matches
# ``host:port`` exactly and supports no wildcards.
#
# npm: a complete `npm ci` of this repo's own web/package-lock.json — 213
# packages — made 15 CONNECTs, all to this one host. Metadata and tarballs
# share it.
#
# cargo: `cargo fetch` on serde and its transitive dependencies contacted the
# sparse index and the download host. `crates.io` itself is the API — publish,
# search, yank — and was never contacted, so it is not here.
#
# PyPI is absent deliberately: it is global (``allow_pypi``) rather than
# developer-gated, because ad-hoc Python runs in every task and not only in
# development ones.
_REGISTRY_HOSTS = frozenset({
    "registry.npmjs.org:443",
    "index.crates.io:443",
    "static.crates.io:443",
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

        # Package registries. Independent of the forge URLs below — a
        # deployment with neither configured still installs dependencies.
        hosts |= _REGISTRY_HOSTS

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
                # `gh run view --log-failed` — the CI feedback loop — fetches
                # job logs from a second host. Measured against gh 2.98 through
                # a logging CONNECT proxy: one stable hostname, the same across
                # independent runs, so an exact entry is enough and the proxy
                # needs no wildcard support.
                #
                # `gh run download` is deliberately NOT covered. Artifacts come
                # from productionresultssa<N>.blob.core.windows.net, where the
                # shard varies (4 and 7 observed for one repository), and the
                # only entry that would cover it is *.blob.core.windows.net —
                # all of Azure Blob Storage, a general-purpose exfiltration
                # channel. Logs are what the feedback loop needs; artifacts
                # are not worth that.
                hosts.add("results-receiver.actions.githubusercontent.com:443")
            elif parsed.hostname:
                # GitHub Enterprise Server: the API is a path on the same host
                # (<host>/api/v3), so no separate entry — but the web host
                # itself was already added above and is what gh talks to.
                pass

    # Nextcloud skill: the instance host. Only reachable when the skill proxy
    # is off — with it on, the skill CLI runs server-side in the daemon's netns
    # and never meets this allowlist.
    if "nextcloud" in authorized_skills and config.nextcloud.url:
        from urllib.parse import urlparse

        parsed = urlparse(config.nextcloud.url)
        if parsed.hostname:
            hosts.add(f"{parsed.hostname}:{parsed.port or 443}")

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


# Non-secret env vars the executor itself withholds from Claude, on top of the
# manifest-declared ``proxy_only`` ones. ISTOTA_DB_PATH is in no skill.md — it
# is set imperatively for every task — so it has no manifest to carry the flag.
_EXECUTOR_PROXY_ONLY_VARS = frozenset({"ISTOTA_DB_PATH"})


def derive_proxy_only_set(skill_index: dict) -> frozenset[str]:
    """Env-var names to route through the proxy without treating as credentials.

    The third bucket alongside credentials and the clean env: values the
    host-side skill CLI needs but the model must not hold. Today that is the
    database paths — ``ISTOTA_DB_PATH`` plus the ``proxy_only`` vars the health
    and location manifests declare for their per-user module DBs.

    Unlike ``derive_credential_set`` this is not per-skill-scoped. These aren't
    secrets, so there is nothing to leak between skills, and scoping them would
    mean re-deriving each skill's DB path in the proxy for no gain.
    """
    return frozenset(
        spec.var
        for meta in skill_index.values()
        for spec in meta.env_specs
        if spec.proxy_only and spec.var
    ) | _EXECUTOR_PROXY_ONLY_VARS


def derive_authorized_skills(
    selected_skills: list[str],
    skill_index: dict,
    ctx: object,
    hook_env: dict[str, str] | None = None,
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

    ``hook_env`` is the merged output of ``dispatch_setup_env_hooks``. It
    is the auto-auth signal for a ``source="setup_env"`` credential, which
    ``_resolve_env_spec`` deliberately resolves to ``None`` — the manifest
    declares only the var name and the hook owns the value. Without it
    such a skill can never auto-authorize, so its credential is stripped
    from Claude's env (it is sensitive) and then never injected back by
    the proxy (it is in no authorized skill's credential map), leaving the
    CLI to run unauthenticated. ``google_workspace`` is the live case: it
    has no eager selector, so it is only ever reached via the on-demand
    menu, and selection is the only other route to authorization. Unlike
    an EnvironmentFile fallback, a hook value is per-user (here, derived
    from that user's stored OAuth token), so it is a sound auto-auth
    signal.
    """
    from .skills._env import _resolve_env_spec  # noqa: PLC0415

    authorized: set[str] = set(selected_skills)
    for name, meta in skill_index.items():
        if name in authorized:
            continue
        sensitive_specs = [s for s in meta.env_specs if s.sensitive]
        if not sensitive_specs:
            continue
        for spec in sensitive_specs:
            if spec.source == "setup_env":
                # The hook self-gates; a produced value means the user has
                # this credential configured.
                resolved = (hook_env or {}).get(spec.var)
            else:
                resolved = _resolve_env_spec(spec, ctx, fallbacks_disabled=True)
            if resolved:
                authorized.add(name)
                break
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
    """Build the per-task tool list.

    The CLI brains (ClaudeCodeBrain / TmuxClaudeBrain) no longer pass this as an
    ``--allowedTools`` allowlist — they run with ``--dangerously-skip-permissions``
    and the model gets its full default toolset. The security boundary is the
    bwrap sandbox + network proxy + clean env (credential stripping), not an
    interactive permission prompt; Bash is permitted anyway, which is effectively
    unrestricted inside the sandbox. ``Agent`` + ``Workflow`` (the harness's
    multi-agent fan-out) are denied separately via ``--disallowedTools`` so
    Istota orchestrates through its own skills.

    The returned list still matters in two places: NativeBrain filters its
    in-process tool set by these names, and a non-empty list is the signal that
    distinguishes a tool-bearing task from a text-only one (empty => sleep cycle
    / OCR / explainer, which get no tools and no skip-permissions).

    WebSearch / WebFetch are included; WebSearch runs server-side (Anthropic's
    backend) and only returns result titles + URLs, so page reading is steered to
    the `browse` skill in the prompt's Tools section.
    """
    return ["Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch", "WebFetch"]


def _validate_workspace_dir(config: Config, workspace_dir: Path) -> Path:
    """Resolve and bounds-check a REPL workspace directory (blocklist posture).

    An arbitrary RW bind expands the sandbox's writable surface, so reject paths
    that overlap sensitive roots: the database directories, other users'
    Nextcloud mounts, the istota source tree, the credential/secret dirs, and
    $HOME dotfile config dirs (~/.ssh, ~/.config, ~/.claude, ~/.developer). The
    bwrap-host ``--workspace cwd`` case is the security-relevant one; Mac/Docker
    have no bwrap and degrade to running in cwd directly.

    The database entries are not what stops the *bwrap* path — the masks run
    last there, so a tmpfs at or under the workspace shadows the bind whatever
    it was. They are load-bearing for ``native_fs_roots``, which threads this
    same validated workspace into the native brain's in-process file tools and
    has no masks at all. In the default layout the source-tree entry covers the
    databases incidentally; a relocated ``db_path`` or ``module_data_dir`` is
    the case that needs naming.

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
    # The framework DB directory and the per-user module-DB root. Skipped when
    # db_path is relative (the `data/istota.db` default): it would resolve
    # against the *current* cwd, so `istota repl --workspace ~/proj` launched
    # from inside ~/proj would be refused for overlapping a `<cwd>/data` that
    # need not even exist.
    if config.db_path and Path(config.db_path).is_absolute():
        forbidden.append(Path(config.db_path).parent.resolve())
    try:
        forbidden.append(config.module_db_root())
    except ValueError:
        # Misconfigured module_data_dir (under the mount). The mount root is
        # already forbidden above, so the path is covered either way.
        pass
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


_cache_dir_refusals: set[str] = set()

# Cache subdirectory names, per tool. uv and npm each treat their cache
# directory as theirs alone and prune it, so they do not share one root.
SANDBOX_CACHE_UV = "uv"
SANDBOX_CACHE_NPM = "npm"


def _sandbox_bind_targets(config: Config) -> list[Path]:
    """Paths ``build_bwrap_cmd`` mounts, that a cache must not be mounted above.

    bwrap applies argv in order, so a later ``--bind`` whose destination is an
    *ancestor* of an earlier mount covers it — the same mechanism the
    ``.developer`` read-only re-bind and the database masks rely on, used the
    wrong way round. The cache bind is emitted late, so without this list a
    supported config value silently revokes boundaries the sandbox is built on:
    ``sandbox_cache_dir = $HOME/.cache`` overmounts the read-only huggingface
    bind, ``= config.temp_dir`` hands every user's deferred-op directory to
    every task and makes the credential-fetch helpers under ``.developer``
    writable again, and ``= $HOME/.local`` gives the model write access to the
    ``claude`` binary the daemon spawns host-side.

    ``_validate_workspace_dir`` does not cover this and should not be made to:
    the REPL workspace it was written for is bound *before* all of those, so
    ordering protects it and its blocklist never had to name them. This list is
    the same idea as ``_mask_protected`` — what a late mount operation must not
    swallow — for the one other late mount operation.

    Equal-or-ancestor is the rule, not overlap: a cache *inside* one of these is
    the documented shape (``developer.repos_dir`` is where it is supposed to go)
    and covers nothing.
    """
    home = Path(os.environ.get("HOME", "/tmp"))
    targets: list[Path] = [
        Path("/"), Path("/usr"), Path("/etc"), Path("/tmp"),
        # Every user's task workspace, and with it `.developer`.
        config.temp_dir,
        # The Claude CLI binary, its state, and its credentials.
        home / ".local",
        home / ".claude",
        # The read-only model cache.
        home / ".cache" / "huggingface",
    ]
    if config.developer.repos_dir:
        targets.append(Path(config.developer.repos_dir))
    if config.nextcloud_mount_path:
        targets.append(Path(config.nextcloud_mount_path))
    for ro_path in config.security.sandbox_ro_paths:
        targets.append(Path(ro_path))
    sp_path = custom_system_prompt_path(config)
    if sp_path is not None:
        targets.append(sp_path)
    return targets


def resolve_sandbox_cache_dir(config: Config, user_id: str) -> Path | None:
    """This user's ``security.sandbox_cache_dir`` subdirectory, or None.

    One predicate for two decisions — the RW bind in ``build_bwrap_cmd`` and the
    ``UV_CACHE_DIR`` / ``XDG_CACHE_HOME`` group in ``execute_task``. They must
    not disagree: naming a cache the sandbox did not bind points uv at a path
    that exists inside the namespace only on bwrap's root tmpfs, which is the
    RAM-backed cache ISSUE-305 is about, at a new name.

    **Per user, not per deployment.** The configured value is a root; each user
    gets ``{root}/{user_id}``, created here. A single shared directory would be
    the first RW surface a non-admin task and an admin task hold in common, and
    it persists across tasks by construction — and uv's unpacked-wheel cache is
    trusted on read, never re-verified against a hash, so a planted archive is
    executed by the next ``uv sync`` that hardlinks out of it. Per-user costs
    nothing the placement argument was about: hardlink sharing is between one
    user's worktrees, which stay inside one subdirectory.

    Returned **as written**, not resolved, though every check below runs against
    the resolved path. ``_bind`` uses the string it was handed as the sandbox
    destination, and the developer-repos bind passes ``repos_dir`` unresolved —
    so resolving here would put a symlinked ``repos_dir`` and a cache under it
    at two different names inside the namespace, hence on two mounts, and
    ``link(2)`` returns EXDEV between them. That is the exact cost the
    recommendation to put the cache under ``repos_dir`` exists to avoid, failing
    silently.

    Never raises. Every rejection falls open to the pre-ISSUE-305 behaviour,
    because both callers run on the task path — for NativeBrain, per Bash call —
    and the alternative to failing open is a config typo that fails every task.
    """
    raw = config.security.sandbox_cache_dir
    if not raw:
        return None

    def _refuse(message: str) -> None:
        # Called from `build_bwrap_cmd` and from `execute_task`, on every task,
        # for what is a fact about the config file. Warn once per process per
        # distinct problem instead of twice per task forever.
        if message not in _cache_dir_refusals:
            _cache_dir_refusals.add(message)
            logger.warning("%s", message)

    try:
        root = Path(raw)
        if not root.is_absolute():
            _refuse(
                f"sandbox_cache_dir {raw!r} is not an absolute path; not binding it. "
                "A relative path would resolve against the daemon's working directory."
            )
            return None

        resolved_root = root.resolve()
        if not (resolved_root.is_dir() and os.access(resolved_root, os.W_OK | os.X_OK)):
            _refuse(
                f"sandbox_cache_dir {resolved_root} is not a directory the daemon can "
                "write; not binding it. Package caches stay on the sandbox's root "
                "tmpfs, in RAM."
            )
            return None

        # Not above anything the sandbox already mounts — see _sandbox_bind_targets.
        for target in _sandbox_bind_targets(config):
            try:
                resolved_target = target.resolve()
            except OSError:
                continue
            if resolved_target == resolved_root or _is_relative_to(resolved_target, resolved_root):
                _refuse(
                    f"sandbox_cache_dir {resolved_root} is at or above {resolved_target}, "
                    "which the sandbox mounts; not binding it, because the cache bind "
                    "would cover that mount. Put it inside a directory instead of above "
                    "one — somewhere under developer.repos_dir is the intended home."
                )
                return None

        # The database directories, checked here rather than left to
        # `_validate_workspace_dir`: that function skips a relative `db_path`
        # (the shipped default) because a REPL workspace would resolve it
        # against the wrong cwd, and the daemon has no such problem. The masks
        # are the last mount operations and are read-only, so a cache under one
        # is a dead end uv cannot write. The cache loses that argument, never
        # the mask.
        db_dirs: list[Path] = []
        if config.db_path:
            db_dirs.append(Path(config.db_path).parent.resolve())
        try:
            db_dirs.append(config.module_db_root())
        except ValueError:
            pass
        for db_dir in db_dirs:
            if resolved_root == db_dir or _is_relative_to(resolved_root, db_dir):
                _refuse(
                    f"sandbox_cache_dir {resolved_root} is under the database directory "
                    f"{db_dir}, which the sandbox masks read-only; not binding it."
                )
                return None

        # The remaining protected roots — the source tree, the mount, the
        # credential and dotfile directories — via the blocklist the REPL
        # workspace already uses. Same posture: an operator-named RW bind.
        _validate_workspace_dir(config, resolved_root)

        cache_dir = root / user_id
        cache_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(cache_dir, 0o700)
        return cache_dir
    except Exception as exc:  # never raise: both callers are on the task path
        _refuse(f"sandbox_cache_dir {raw} rejected ({exc}); not binding it.")
        return None


def custom_system_prompt_path(config: Config) -> Path | None:
    """Absolute path of the operator's ``config/system-prompt.md``, or None.

    Absolute via ``abspath`` (not ``resolve``) so a relative ``skills_dir``
    still yields a path a child process with its own ``--chdir`` can open,
    without rewriting a symlinked deployment root to a name nothing else uses.
    """
    if not config.custom_system_prompt:
        return None
    path = config.skills_dir.parent / "system-prompt.md"
    if path.is_absolute():
        return path
    return Path(os.path.abspath(path))


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

    # Selective /etc binds — only what's needed for DNS, TLS, user lookup,
    # timezone, and for the binaries in the /usr bind above to resolve.
    #
    # /etc/alternatives is the last of those and the least obvious: Debian ships
    # awk, cc, vi, editor, pager, which and nc as /usr/bin symlinks into it, so
    # binding /usr alone carries the links in and leaves every one of them
    # dangling. The command then fails with "No such file or directory" for a
    # binary ls shows sitting right there, inside the sandbox only. It holds
    # nothing but symlinks back into /usr, which is already bound.
    etc_files = [
        "/etc/ssl", "/etc/ca-certificates", "/etc/resolv.conf",
        "/etc/hosts", "/etc/nsswitch.conf", "/etc/ld.so.cache",
        "/etc/localtime", "/etc/passwd", "/etc/group",
        "/etc/alternatives",
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

    # --- Custom system prompt (RO, this one file) ---
    # The config directory is not in the sandbox and should not be: it holds
    # config.toml. Everything else in it — emissaries, persona, guidelines,
    # skill bodies — reaches the model as content the daemon read and put in
    # the prompt. `system-prompt.md` is the exception, because the CLI opens
    # the path itself, inside the namespace. Binding the file rather than its
    # directory keeps config.toml out; bwrap creates the parent as a mount
    # point and nothing else in it is visible.
    #
    # This dependency was met until now only by `sandbox_ro_paths =
    # ["/srv/app"]`, the same default that exposed the databases; narrowing it
    # to [] made every task on a custom_system_prompt install exit with
    # "System prompt file not found".
    sp_path = custom_system_prompt_path(config)
    if sp_path is not None:
        _ro_bind(sp_path)

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

    # --- Devbox: Docker CLI + Docker-API allowlist proxy ---
    # The raw Docker socket is root-equivalent on the host: anything inside the
    # sandbox that can write to it can launch a privileged container that mounts
    # the host root. So we never bind the raw socket. Instead we bind the
    # per-user Docker-API allowlist proxy (src/istota/docker_proxy.py) at the
    # conventional in-sandbox path /var/run/docker.sock — the docker client
    # connects there by default, so the devbox CLI is unchanged. The proxy
    # forwards only exec/cp/inspect/restart on the user's own container and
    # refuses create/run/build/privileged/host-mount, so it is safe to bind
    # unconditionally (no selection-time gate): even an untrusted-content task
    # that reaches the socket directly (curl --unix-socket) can't escalate.
    if config.devbox.enabled and config.devbox.api_proxy_enabled:
        docker_cli = Path(config.devbox.docker_cli)
        if docker_cli.exists():
            _ro_bind(docker_cli)
        proxy_docker_sock = Path(config.devbox.api_proxy_socket_dir) / f"{task.user_id}.sock"
        # Bind the proxy socket at the *literal* conventional dest path so the
        # docker client finds it by default. dest is kept unresolved (mirrors
        # the old raw-socket bind, where /var/run was never otherwise mapped);
        # bwrap creates the intermediate mount point.
        resolved_proxy = proxy_docker_sock.resolve()
        if resolved_proxy.exists():
            args.extend(["--bind", str(resolved_proxy), config.devbox.docker_socket])

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

    # --- Huggingface model cache (RO) ---
    hf_cache = home / ".cache" / "huggingface"
    if hf_cache.exists():
        _ro_bind(hf_cache)

    # --- Package-manager cache (RW) ---
    # Not gated on admin or on the developer skill: any task that runs a
    # package manager writes a cache, and without this the write lands on
    # bwrap's root tmpfs. `execute_task` points UV_CACHE_DIR and XDG_CACHE_HOME
    # at whatever this returns, so the bind and the environment cannot disagree.
    #
    # `{configured root}/{user_id}`, not the root itself — see
    # `resolve_sandbox_cache_dir` for why a shared cache is a cross-user code
    # path. This is emitted late, after the `.developer` read-only re-bind and
    # the huggingface bind, so a destination *above* either would cover it;
    # `_sandbox_bind_targets` is what refuses that. Still before the masks,
    # which stay last.
    cache_dir = resolve_sandbox_cache_dir(config, task.user_id)
    if cache_dir is not None:
        _bind(cache_dir)

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

    # --- Database masks (must be the LAST mount operations) ---
    # No SQLite file the daemon owns is readable from inside the sandbox, for
    # admins or anyone else. Reads and writes go through skill CLIs, which the
    # proxy runs host-side scoped by ISTOTA_USER_ID.
    #
    # These are masks rather than "just don't bind it" because not binding was
    # never sufficient and the gap was invisible: `module_data_dir` defaults
    # under `{db_path.parent}`, the reference deployment puts that under
    # `istota_home`, and `sandbox_ro_paths` defaulted to the `/srv/app` that
    # contains it — so one RO bind that mentions no database exposed the
    # framework DB, its live -wal/-shm, every user's health/money/location/
    # feeds DB, the local DB backups and the browser profile. An empty tmpfs
    # over the directories shadows whatever earlier binds put there, because
    # bwrap applies operations in argv order and these are last. Keep them last.
    # `--remount-ro` on each mask is part of the same operation — see `_mask_dir`
    # for why an empty *writable* tmpfs makes the dead end look like a corrupt
    # database — and is the one thing that may follow a mask, since it can only
    # take permissions away.
    #
    # It is a mask, not a revocation: with `kernel.unprivileged_userns_clone`
    # on (bwrap needs it) a process can `unshare -Urm` and umount a tmpfs to
    # reveal what was underneath, which is why `--disable-userns` is passed
    # where bwrap supports it and why `sandbox_ro_paths` should stay narrow.
    # With nothing bound underneath — the shipped default — there is nothing to
    # reveal either way.
    #
    # Paths the sandbox must keep. A mask at or above any of these would
    # shadow something the task needs (its own workspace, the source tree it
    # runs from), turning a security measure into an outage; the standalone
    # layout puts db_path beside the workspace, so this is reachable by
    # configuration rather than only by mistake.
    _mask_protected: list[Path] = [user_temp_dir.resolve(), istota_src, venv_path]
    if workspace_resolved is not None:
        _mask_protected.append(workspace_resolved)
    if mount:
        _mask_protected.append(mount)
    _masked: list[Path] = []

    def _mask_dir(target: Path) -> None:
        """Cover ``target`` with an empty, read-only tmpfs, at every name it
        answers to.

        Both the resolved path and the path as written: `_ro_bind` uses the
        *unresolved* string as its sandbox destination, so under a symlinked
        deployment root (`/srv` -> `/realstore`) a bind lands at `/srv/app`
        while a resolved-only mask lands at `/realstore/app/...` — a path not
        in the namespace at all, leaving the databases readable at the name the
        model would actually use.

        Read-only because a writable mask makes the dead end lie. `sqlite3
        {db_dir}/istota.db "select …"` on a writable tmpfs *creates* the file
        and then reports `no such table` — which reads as a missing schema or a
        corrupt database, sends the model hunting, and leaves a zero-byte
        `istota.db` sitting in the directory for the rest of the task. On a
        read-only mask the same command fails at open, which is the truth: the
        file is not in this namespace. It also means nothing a task writes
        under a database directory can survive to be mistaken for a database.

        Read-only makes a mask *under* an existing mask fatal rather than
        merely redundant: bwrap has to `mkdir` the second mountpoint on the
        first mask's tmpfs, gets EROFS, and exits before running anything — so
        a second mask nested in the first would fail every task rather than
        weakening one directory. Already-covered candidates are therefore
        skipped here, where every mask can see the others, rather than by each
        caller checking one path against one other.
        """
        candidates: list[Path] = []
        for candidate in (target, target.resolve()):
            if candidate not in candidates:
                candidates.append(candidate)
        for candidate in candidates:
            shadowed = [
                p for p in _mask_protected
                if p == candidate or p.is_relative_to(candidate)
            ]
            if shadowed:
                logger.error(
                    "Not masking %s: it contains paths the sandbox needs (%s). "
                    "Move db_path/module_data_dir out from above the workspace "
                    "and the source tree — the databases are exposed until you do.",
                    candidate, ", ".join(str(p) for p in shadowed),
                )
                continue
            if any(candidate.is_relative_to(m) for m in _masked):
                continue
            args.extend(["--tmpfs", str(candidate)])
            if _bwrap_supports_remount_ro():
                # After the tmpfs, never before: --remount-ro acts on whatever
                # is mounted at that path at the time bwrap reaches it, and
                # before the tmpfs that is the host directory.
                args.extend(["--remount-ro", str(candidate)])
            _masked.append(candidate)

    if config.db_path:
        db_dir = Path(config.db_path).parent
        _mask_dir(db_dir)
        try:
            module_root = config.module_db_root()
        except ValueError:
            # module_data_dir is under the Nextcloud mount — a misconfiguration
            # module resolution fails loudly on. Refusing to build the sandbox
            # would turn it into "every task fails", so mask what we can and
            # let the module path own the error. The mount root is bound only
            # per-user, so the misplaced root isn't broadly reachable anyway.
            logger.warning(
                "module_data_dir is under the Nextcloud mount; skipping its "
                "sandbox mask (module resolution will raise on use)",
            )
        else:
            # No "is it already under db_dir?" test here: `_mask_dir` skips a
            # candidate any earlier mask already covers, and it does it against
            # every name each mask answers to. The check that used to live here
            # compared one resolved path against one other, and it also skipped
            # the module root when the db_dir mask had been *refused* — leaving
            # it unmasked for want of a cover that was never mounted.
            _mask_dir(module_root)

    if _bwrap_supports_disable_userns():
        # Both, or neither: bwrap exits 1 on `--disable-userns` without
        # `--unshare-user` ("--disable-userns requires --unshare-user"), which
        # is why this flag never once reached a real sandbox — the probe had
        # the same gap and answered "unsupported" on every host. Unprivileged
        # bwrap unshares the user namespace regardless, so on the supported
        # deployment the companion flag only makes the request explicit.
        args.extend(["--unshare-user", "--disable-userns"])
    elif _bwrap_requires_unshare_user():
        # Not hardening, unlike the branch above: on this host it is what makes
        # bwrap run at all. `_bwrap_available`'s plain probe failed and its
        # `--unshare-user` probe succeeded, which happens as uid 0 with a
        # non-setuid bwrap — bwrap only forces the user namespace on itself
        # when it is neither. The real argv has to carry the flag the probe was
        # answered with, or the daemon would report a working sandbox and then
        # build one that cannot start.
        args.append("--unshare-user")

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


def native_fs_confinement_active(config: Config) -> bool:
    """Whether NativeBrain's in-process file tools should be path-confined.

    Keyed off *effective* sandboxing, exactly like the executor's cwd choice:
    on a real multi-user deployment (Linux + bwrap) the claude_code path
    confines the filesystem via bwrap, so the native file tools — which run
    in-process, outside any bwrap — must confine themselves to the same roots
    (NB-1). Where bwrap is unavailable (Mac / dev), claude_code runs unconfined
    too, so native stays unconfined for parity rather than surprising the
    developer with a boundary the CLI path doesn't have.
    """
    return effective_sandboxing(config)


def native_fs_roots(
    config: Config,
    task: db.Task,
    is_admin: bool,
    user_resources: list[db.UserResource],
    user_temp_dir: Path,
    workspace_dir: Path | None = None,
) -> tuple[list[Path], list[Path], list[Path]]:
    """File-access roots for a native-brain task.

    Returns ``(read_roots, write_roots, write_denied_roots)``.

    Mirrors ``build_bwrap_cmd``'s user-data binds (not the system/venv binds,
    which are irrelevant to the file tools) so the native file tools reach
    exactly what the claude_code path's bwrap would allow — no more, no less.
    Writable roots are the RW binds; read roots additionally include the RO
    binds (Talk attachments, read-only resources). No database root of any
    kind — build_bwrap_cmd masks those, and these tools have no masks.

    The third element carries the RO carve-outs bwrap gets by re-binding a
    subdirectory read-only *after* its parent's RW bind. Containment alone
    cannot express those, so they are returned separately and threaded onto
    ``ToolEnv.write_denied_roots``. Today that is ``.developer`` — the
    credential-fetch helper and the git credential helpers — which the
    claude_code path has protected since the RO re-bind was added and which
    this function silently left writable until it grew this return value.

    Carve-outs here deny *writes* only. bwrap's other nested override, the
    tmpfs masks over ``db_path.parent`` and ``module_db_root()``, is a total
    mask this cannot express — what holds that property on the native side is
    that neither path is under a returned root, which in turn rests on
    ``Config.module_db_root`` refusing a module dir under the Nextcloud mount
    and on ``_validate_workspace_dir`` refusing a workspace that would bind one
    back in. Those two guards, not this function, are what to check if a
    deployment ever puts a database under ``temp_dir`` or ``repos_dir``.
    """
    write: list[Path] = []
    read_only: list[Path] = []
    write_denied: list[Path] = []

    def _add(target: list[Path], p: Path | None) -> None:
        if p is None:
            return
        rp = p.resolve()
        if rp.exists() and rp not in target:
            target.append(rp)

    # User workspace (RW) — always present (mkdir'd by the caller).
    _add(write, user_temp_dir)

    # .developer/ (RO carve-out inside the workspace above). Mirrors the
    # _ro_bind in build_bwrap_cmd: the scripts in here hold the credential
    # helpers, so a writable copy is a credential-interception path.
    #
    # Appended directly rather than through _add, which skips a path that does
    # not exist yet. build_bwrap_cmd re-checks `dev_dir.is_dir()` on every Bash
    # invocation, while this list is built once per task — so an existence gate
    # here would leave a window where a .developer created mid-run is read-only
    # for Bash and writable for the file tools. A deny root that never comes
    # into existence costs one failed comparison.
    write_denied.append(user_temp_dir.resolve() / ".developer")

    # REPL workspace (RW), validated against the protected-path blocklist.
    if workspace_dir is not None:
        try:
            _add(write, _validate_workspace_dir(config, workspace_dir))
        except ValueError:
            logger.warning("native_fs_roots: workspace %s rejected by blocklist", workspace_dir)

    mount = config.nextcloud_mount_path
    user_dir = None
    if mount:
        mount = mount.resolve()
        user_dir = mount / "Users" / task.user_id
        _add(write, user_dir)
        _add(read_only, mount / "Talk")  # attachments, RO
        if task.conversation_token:
            _add(write, mount / "Channels" / task.conversation_token)

    # No database root of any kind. The file tools are the native brain's
    # stand-in for the bwrap binds, and bwrap no longer exposes the framework
    # DB or the module DBs to anyone — see the mask block in build_bwrap_cmd.

    # Package-manager cache (RW) — mirrors the bwrap bind, which is gated on
    # neither admin nor skill selection. `_add` skips a path that does not
    # exist, and `resolve_sandbox_cache_dir` creates it, so the two agree.
    _add(write, resolve_sandbox_cache_dir(config, task.user_id))

    # Developer repos (RW, admin only).
    if is_admin and config.developer.enabled and config.developer.repos_dir:
        _add(write, Path(config.developer.repos_dir))

    # Per-resource mounts (RW/RO) not already covered by the user dir.
    if mount:
        for r in user_resources:
            if not r.resource_path:
                continue
            rpath = (mount / r.resource_path.lstrip("/")).resolve()
            if not rpath.exists():
                continue
            if user_dir is not None:
                try:
                    rpath.relative_to(user_dir.resolve())
                    continue  # already inside the user dir
                except ValueError:
                    pass
            _add(write if r.permissions == "readwrite" else read_only, rpath)

    read_roots = list(dict.fromkeys(write + read_only))
    return read_roots, write, write_denied


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


def _user_email_address_map(config: Config) -> dict[str, list[str]]:
    """Every configured user's own email addresses, keyed by user id.

    Drives the ISSUE-226 sender attribution in the history readers. A user with
    no configured addresses maps to `[]`, which reads as "nothing is theirs" —
    every email turn of theirs is then attributed to its envelope sender. That
    is the safe direction, but it also means their *own* mail reads as
    third-party, so a missing `email_addresses` is worth a log line rather than
    silent degradation.
    """
    address_map: dict[str, list[str]] = {}
    for user_id, user_config in config.users.items():
        addresses = list(user_config.email_addresses or [])
        if not addresses:
            logger.debug(
                "User %s has no configured email_addresses; their own email "
                "turns will be attributed to the sending address",
                user_id,
            )
        address_map[user_id] = addresses
    return address_map


def _ensure_reply_parent_in_history(
    task: db.Task,
    history: list[db.ConversationMessage],
    config: Config,
    conn: "db.sqlite3.Connection | None" = None,
) -> tuple[list[db.ConversationMessage], db.ConversationMessage | None]:
    """
    Ensure the replied-to message's task is included in conversation history.

    If the user replied to a specific message, look up the task associated with
    it and prepend that whole turn to history if not already present — which is
    what saves a long parent from being reduced to the 1000-char snapshot.
    Falls back to injecting reply_to_content as a synthetic message if the
    parent task isn't found in the DB.

    The parent is addressed in one of two namespaces, resolved by two distinct
    lookups: `reply_to_message_id` is a canonical `messages.id` (web) and
    `reply_to_talk_id` is a Talk message id. They are never interchangeable —
    in a Talk-bound room both are small integers, so crossing them resolves to
    an unrelated turn with no signal that it went wrong. The canonical id is
    tried first: a task carrying both is one whose canonical citation was
    derived from the Talk one (Stage 6), where the canonical lookup is the
    more precise of the two.

    Returns (updated_history, reply_parent_msg) where reply_parent_msg is the
    message that must survive triage (or None if not applicable).
    """
    if not task.conversation_token:
        return history, None
    if not task.reply_to_message_id and not task.reply_to_talk_id:
        return history, None

    history_ids = {msg.id for msg in history}

    # `get_reply_parent_task` also matches on `talk_response_id`, which an email
    # task carries once its confirmation prompt was posted — so this path can
    # surface an email turn and needs the same sender attribution as the bulk
    # readers (ISSUE-226).
    address_map = _user_email_address_map(config)

    def _lookup(c: db.sqlite3.Connection) -> tuple[db.Task | None, str | None]:
        parent = None
        if task.reply_to_message_id:
            parent = db.get_reply_parent_task_by_message_id(
                c, task.conversation_token, task.reply_to_message_id,
            )
        if parent is None and task.reply_to_talk_id:
            parent = db.get_reply_parent_task(
                c, task.conversation_token, task.reply_to_talk_id,
            )
        if parent is None:
            return None, None
        return parent, db.email_sender_for_task(c, parent.id)

    if conn is not None:
        parent_task, parent_sender = _lookup(conn)
    else:
        with db.get_db(config.db_path) as temp_conn:
            parent_task, parent_sender = _lookup(temp_conn)

    if parent_task:
        parent_msg = db.ConversationMessage(
            id=parent_task.id,
            prompt=parent_task.prompt,
            result=parent_task.result or "",
            created_at=parent_task.created_at or "",
            actions_taken=parent_task.actions_taken,
            source_type=parent_task.source_type,
            user_id=parent_task.user_id,
            external_sender=db.external_email_sender(
                parent_sender, address_map.get(parent_task.user_id or "", []),
            ),
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

    # An unresolvable parent — a `role='system'` row, a turn retention deleted,
    # a turn that failed or is still running — falls through to the snapshot
    # alone, which `build_prompt` already quotes into the request section
    # unconditionally. The synthetic `(replied-to message)` context row that
    # used to be injected here is gone with the `(In reply to: …)` fallbacks it
    # was a sibling of: both put the same 1000 characters in the prompt a
    # second time, once as context and once as the frame.
    if task.reply_to_content:
        logger.info(
            "Reply parent not resolvable for task %d "
            "(canonical=%s talk=%s); the request-section quote stands alone",
            task.id,
            task.reply_to_message_id,
            task.reply_to_talk_id,
        )

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
        # No reply-to fallback here any more: `build_prompt` renders the
        # citation into the request section unconditionally, so emitting it as
        # the whole conversation context would quote the same snapshot twice.
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
        # No synthetic stand-in when the parent isn't in the fetched window.
        # `build_prompt` quotes the snapshot into the request section
        # unconditionally, so synthesizing a context message from the same
        # string puts it in the prompt twice — and this is the *common* Talk
        # case, since a reply to anything more than a few turns back falls
        # outside the window. The last of the five fallbacks that did this;
        # the other four are gone for the same reason.

    # Select relevant messages (triage routed through the task's brain)
    relevant = select_relevant_talk_context(
        task.prompt, talk_messages, config,
        completer=_build_triage_completer(task, config),
        on_usage=_build_triage_usage_sink(task, config),
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
    # Exclude background / non-conversational task types from conversation
    # context. subtask/heartbeat are the forward guard (defense-in-depth on top
    # of the nonconversational_transcript_cleanup_v1 migration) against any
    # future path that stores a non-conversational user row — the model must
    # never read a cron/subtask post back as prior user conversation.
    _exclude_types = ["scheduled", "briefing", "subtask", "heartbeat"]

    # Who each turn is attributed to (ISSUE-226). Every user, not just the task's
    # own: a shared room's history carries co-members' turns, and checking theirs
    # against this user's addresses would mark them external for no reason.
    own_email_addresses = _user_email_address_map(config)

    if conn is not None:
        history = db.get_conversation_history(
            conn, task.conversation_token, exclude_task_id=task.id,
            limit=config.conversation.lookback_count,
            exclude_source_types=_exclude_types,
            user_email_addresses=own_email_addresses,
        )
    else:
        with db.get_db(config.db_path) as temp_conn:
            history = db.get_conversation_history(
                temp_conn, task.conversation_token, exclude_task_id=task.id,
                limit=config.conversation.lookback_count,
                exclude_source_types=_exclude_types,
                user_email_addresses=own_email_addresses,
            )

    # Inject recent scheduled/briefing tasks in the same channel — these are
    # deliberately re-surfaced (cron/briefing output the user may reference)
    # even though get_conversation_history excludes them. But subtask/heartbeat
    # must stay excluded here too: a subtask's synthetic orchestration prompt is
    # not a user utterance, and re-injecting it would read back as prior user
    # conversation (the LLM-context isolation invariant — canonical-room-
    # transcript spec). So hard-exclude them from this re-surfacing path as well.
    _prev_exclude = ["subtask", "heartbeat"]
    if conn is not None:
        prev_tasks = db.get_previous_tasks(
            conn, task.conversation_token, exclude_task_id=task.id,
            limit=config.conversation.previous_tasks_count,
            exclude_source_types=_prev_exclude,
            user_email_addresses=own_email_addresses,
        )
    else:
        with db.get_db(config.db_path) as temp_conn:
            prev_tasks = db.get_previous_tasks(
                temp_conn, task.conversation_token, exclude_task_id=task.id,
                limit=config.conversation.previous_tasks_count,
                exclude_source_types=_prev_exclude,
                user_email_addresses=own_email_addresses,
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
            task.prompt, history, config,
            completer=_build_triage_completer(task, config),
            on_usage=_build_triage_usage_sink(task, config),
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
        # The citation is no longer stood up as the whole context here — the
        # request section carries it either way. See `_build_talk_api_context`.
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


def load_channel_guidelines(
    config: Config, source_type: str, user_id: str | None = None,
) -> str | None:
    """Load channel-specific guidelines, substituting the doc placeholders.

    ``{user_id}`` joins ``{BOT_NAME}``/``{BOT_DIR}`` here so a guideline can
    name a concrete workspace path — web.md's file-handover link needs one, and
    a literal ``{user_id}`` reaching the model is worse than no example. Skill
    bodies already substitute it; this brings guidelines in line with the set
    AGENTS.md documents.
    """
    config_dir = config.skills_dir.parent
    guidelines_path = config_dir / "guidelines" / f"{source_type}.md"
    if guidelines_path.exists():
        text = _apply_bot_name(guidelines_path.read_text().strip(), config)
        if user_id:
            text = text.replace("{user_id}", user_id)
        return text
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


def _recall_playbooks(
    config: Config,
    conn: "db.sqlite3.Connection | None",
    task: db.Task,
    skip_memory: bool = False,
) -> str | None:
    """Recall learned playbooks relevant to the task prompt (Part B).

    Mirrors `_recall_memories` but queries only `source_type="playbook"`,
    user-scoped, top-`playbooks.recall_limit`. Gated on `playbooks.enabled`,
    skipped for automated tasks (briefings/scheduled, like all personal memory)
    and when a selected skill set `skip_memory`.
    """
    if not config.playbooks.enabled:
        return None
    if skip_memory or _is_automated_task(task):
        return None

    try:
        from .memory.search import search
    except ImportError:
        return None

    try:
        if conn is not None:
            results = search(
                conn, task.user_id, task.prompt,
                limit=config.playbooks.recall_limit,
                source_types=["playbook"],
            )
        else:
            with db.get_db(config.db_path) as temp_conn:
                results = search(
                    temp_conn, task.user_id, task.prompt,
                    limit=config.playbooks.recall_limit,
                    source_types=["playbook"],
                )
    except Exception:
        logger.debug("Playbook recall search failed", exc_info=True)
        return None

    if not results:
        return None

    # Stamp use-recency onto each recalled playbook file so the sleep cycle's
    # retention prune keys on last-use, not last-write (ISSUE-174 Concern 3).
    now = time.time()
    for r in results:
        source_id = getattr(r, "source_id", None)
        if not source_id:
            continue
        try:
            os.utime(source_id, (now, now))
        except OSError as e:
            # A no-op utime (e.g. an rclone FUSE mount that rejects utimens)
            # silently reverts Concern 3 to write-based aging — log it so the
            # degradation is visible rather than invisible.
            logger.debug("playbook mtime stamp failed for %s: %s", source_id, e)
            continue

    parts = []
    for r in results:
        snippet = r.content.strip()
        parts.append(f"- {snippet}")
    return "\n\n".join(parts)


def _apply_memory_cap(
    config: Config,
    user_memory: str | None,
    dated_memories: str | None,
    channel_memory: str | None,
    recalled_memories: str | None,
    knowledge_facts: str | None = None,
    playbooks: str | None = None,
) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None]:
    """Truncate memory components if total exceeds max_memory_chars.

    Truncation order: recalled → knowledge facts → dated → playbooks →
    (warn about user/channel). Playbooks are truncated late because an
    actionable procedure is higher-value than recalled snippets, dated context,
    or KG triples (cap-ladder open question resolved in favour of protecting
    playbooks). Returns the updated components.
    """
    cap = config.max_memory_chars
    if cap <= 0:
        return user_memory, dated_memories, channel_memory, recalled_memories, knowledge_facts, playbooks

    total = (
        len(user_memory or "")
        + len(dated_memories or "")
        + len(channel_memory or "")
        + len(recalled_memories or "")
        + len(knowledge_facts or "")
        + len(playbooks or "")
    )
    if total <= cap:
        return user_memory, dated_memories, channel_memory, recalled_memories, knowledge_facts, playbooks

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

    # Then playbooks (most protected of the recall-tier sources)
    if playbooks and over > 0:
        if over >= len(playbooks):
            over -= len(playbooks)
            playbooks = None
        else:
            playbooks = playbooks[:len(playbooks) - over] + "\n...[truncated]"
            over = 0

    if over > 0:
        logger.warning(
            "Memory cap (%d) exceeded by %d chars after truncating recalled/dated/playbooks; "
            "user_memory=%d, channel_memory=%d chars remain",
            cap, over, len(user_memory or ""), len(channel_memory or ""),
        )

    return user_memory, dated_memories, channel_memory, recalled_memories, knowledge_facts, playbooks


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
    playbooks: str | None = None,
    skip_persona: bool = False,
    cli_skills_text: str | None = None,
    skills_index: str | None = None,
    confirmation_context: str | None = None,
    knowledge_facts: str | None = None,
    conn: "db.sqlite3.Connection | None" = None,
) -> str:
    """Build the full prompt for Claude Code execution.

    Pass ``conn`` to let the per-task timezone lookup reuse an existing
    framework-DB connection instead of opening a throwaway one.
    """
    # Stage 3a (Resources sunset): resources are no longer a per-task prompt
    # surface. The enumerated Nextcloud Folders / TODO Files / Notes /
    # Reminders / Calendar-fallback sections are replaced by a single static
    # workspace-layout line; the model finds files by convention + the tools
    # it already has (Glob/Read over the bound workspace). The folder
    # bind-mount loop (build_sandbox_command / native_fs_roots) still mounts
    # out-of-workspace paths into the sandbox; CalDAV discovery still drives
    # the Calendar section; the web root stays config-driven.
    resource_sections = []

    if config.use_mount:
        resource_sections.append(
            f"Your workspace is at Users/{task.user_id}/, containing "
            f"shared/, inbox/, memories/, and your bot dir "
            f"({config.bot_dir_name}/). Notes live in {config.bot_dir_name}/notes/."
        )
    else:
        resource_sections.append(
            f"Your workspace is at Users/{task.user_id}/."
        )

    # Calendars stay discovery-driven (CalDAV); the resource-typed fallback
    # is gone.
    if discovered_calendars:
        cal_list = "\n".join(
            f"  - {name}: {url} ({'read/write' if writable else 'read-only'})"
            for name, url, writable in discovered_calendars
        )
        resource_sections.append(f"Calendars (shared by {task.user_id}):\n{cal_list}")

    resources_text = "\n\n".join(resource_sections)

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
    channel_guidelines = load_channel_guidelines(config, task.source_type, task.user_id)
    channel_section = ""
    if channel_guidelines:
        channel_section = f"\n\n## Response format ({task.source_type})\n\n{channel_guidelines}\n"

    # Build attachments section if present
    attachments_text = ""
    if task.attachments:
        att_list = "\n".join(f"  - {att}" for att in task.attachments)
        # Check if paths are local (absolute) or workspace-relative
        if any(att.startswith("/") for att in task.attachments):
            attachments_text = f"\n\nAttached files (local paths):\n{att_list}"
        else:
            where = "in Nextcloud, access via rclone" if config.storage_is_nextcloud else "workspace-relative"
            attachments_text = f"\n\nAttached files ({where}):\n{att_list}"

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

    # Build learned-playbooks section (Part B). Procedures distilled from past
    # successful tasks — guidance, not gospel; verify before acting.
    playbooks_section = ""
    if playbooks:
        playbooks_section = f"""
## Learned Playbooks

Previously-successful approaches to similar tasks, distilled from past work.
Treat these as guidance — verify each step still applies before acting:

{playbooks}

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

    # Build file access tools section based on the storage backend. Three modes:
    # local folder, Nextcloud via mount, Nextcloud via rclone. Non-admin users
    # get a scoped path restricted to their own directory (server shape only).
    if config.storage_backend == "local":
        ws_root = config.workspace_root(task.user_id) or config.nextcloud_mount_path
        file_tools = f"""- Your files live in your workspace at '{ws_root}'. Use standard file tools (Read, Write, Edit, ls, cat).
  - The workspace is the area you manage for the user (memory, notes, inbox, shared files). It is a normal local folder.
  - This install runs locally without a sandbox, so you also have ordinary access to the rest of the machine's filesystem (the user's home, Downloads, etc.). The workspace is your managed area, not the limit of what you can read — stay within what the user asked for."""
    elif config.use_mount:
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

    # Web tools line. WebSearch + WebFetch are always allowed. WebSearch only
    # returns result titles + URLs, so reading a page needs a fetch tool — steer
    # that to the browse skill when the browser service is up (it renders JS and
    # reaches arbitrary sites); WebFetch is the lightweight fallback.
    if config.browser.enabled:
        web_tools = (
            "\n- Web search: WebSearch — finds result titles and URLs; it does not fetch page content."
            "\n- Reading web pages: prefer the browse skill (istota-skill browse) — it renders JavaScript and follows links. Use WebFetch only as a lightweight fallback for simple static pages."
        )
    else:
        web_tools = (
            "\n- Web search: WebSearch — finds result titles and URLs; it does not fetch page content."
            "\n- Reading web pages: WebFetch fetches a URL and extracts content against your prompt."
        )

    # CLI skills list (generated from skill index metadata)
    cli_skills_section = cli_skills_text or ""

    # Menu index (eligible skills the model can load on demand via skills show).
    # Appended after the CLI-tools list; empty when the menu is empty.
    if skills_index:
        cli_skills_section = (
            (cli_skills_section + "\n" + skills_index)
            if cli_skills_section else skills_index
        )

    # Compute user's local time
    user_tz, user_tz_str = _resolve_user_tz(config, task.user_id, conn=conn)
    user_now = datetime.now(user_tz)
    user_time_str = user_now.strftime("%A, %B %-d, %Y at %-I:%M %p") + f" ({user_tz_str})"
    user_date_str = user_now.strftime("%Y-%m-%d") + f" ({user_tz_str})"
    # UTC anchor for unambiguous elapsed-time arithmetic (ISSUE-091).
    utc_now_str = user_now.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    # No database path, for anyone. It used to be stated for admins because
    # operator tooling refers to it, hedged with "skill CLIs only" because an
    # unqualified "Database path: …" three lines above the rules reads as an
    # affordance (that hedge was ISSUE-237's fix). The file is masked out of
    # the sandbox, so a path would name something that isn't there — worse than
    # useless, since a failed open reads as a broken command rather than as a
    # boundary. What replaces it is the rule below.
    db_path_line = "Database: reachable only through skill CLIs (no file access)"

    # Whether the masks are actually in place — see `effective_sandboxing` for
    # the shapes where they are not. Telling the model there is nothing to open
    # would be false there, and a false boundary claim is worse than none — it
    # is the thing this whole change set is correcting. So the rule keeps the
    # older prohibition-without-mechanism wording instead.
    db_masked = effective_sandboxing(config)
    if db_masked:
        db_rule_admin = (
            "3. Istota's databases are not on your filesystem — the directories "
            "that hold them are empty here, so there is nothing for `sqlite3` or "
            "Python's `sqlite3` to open and no path worth hunting for. Every "
            "read goes through a skill CLI (e.g. `istota-skill kv get`, "
            "`istota-skill tasks status`), which runs outside this sandbox and "
            "returns only your own data; every write goes through one, or via "
            "deferred JSON files in $ISTOTA_DEFERRED_DIR."
        )
        db_rule_user = (
            "3. Istota's databases are not on your filesystem — the directories "
            "that hold them are empty here, so there is nothing for `sqlite3` or "
            "Python's `sqlite3` to open. All database access, read and write, "
            "goes through the skill CLI commands, which run outside this "
            "sandbox and return only your own data, or through the bot's "
            "scheduler."
        )
    else:
        db_rule_admin = (
            "3. Never open a database file directly — not to write, and not to "
            "read. This deployment has no filesystem sandbox, so an attempt may "
            "well succeed and hand you every user's rows; that it works is not "
            "permission. Every read goes through a skill CLI (e.g. "
            "`istota-skill kv get`, `istota-skill tasks status`), which returns "
            "only your own data; every write goes through one, or via deferred "
            "JSON files in $ISTOTA_DEFERRED_DIR."
        )
        db_rule_user = (
            "3. Never open a database file directly — not to write, and not to "
            "read. This deployment has no filesystem sandbox, so an attempt may "
            "well succeed; those files hold every user's data and none of it is "
            "yours to read this way. All database access, read and write, goes "
            "through the skill CLI commands, which return only your own data, "
            "or through the bot's scheduler."
        )

    # Explicit privileges line so admin-gated capabilities (subtasks, shared-KV
    # writes, DB access) don't have to be inferred from indirect signals or
    # discovered by hunting through config/source.
    privileges_line = "Privileges: admin" if is_admin else "Privileges: standard user"

    db_tool_line = ""  # DB writes handled via deferred JSON files

    if is_admin:
        rules_section = f"""## Important rules

1. Only access resources that belong to user '{task.user_id}' as listed above.
2. For sensitive actions, ask for confirmation EXCEPT:
   - Emails to the user's own addresses ({', '.join(user_email_addresses) if user_email_addresses else 'none configured'}) do NOT need confirmation
   - Emails to external addresses DO need confirmation
   - Modifying calendars, deleting files, sharing externally need confirmation
{db_rule_admin}
3a. When you need something your environment can't do — a credentialed request, a network call the allowlist blocks, a read of system state — the answer is a skill CLI subcommand. `istota-skill` runs with credentials and network access this task does not have, and hands you the value synchronously. Check `istota-skill <name> --help` for one before building a workaround out of scheduled jobs, subtasks or file polling; subtasks and jobs are handoffs and never return a value to you. If nothing covers it, say what is missing instead of improvising.
3b. Only wait on out-of-band work when it plausibly finishes within about two minutes — you hold a worker slot for the whole wait, and a scheduled job cannot start before the next minute boundary. When you do wait, never redirect the probe's stderr: `2>/dev/null` makes a broken command indistinguishable from "not ready yet" and runs the loop to its full length. Abort after two consecutive non-zero exits, and cap the total wait. If the work might take longer, hand off and answer in a later turn.
4. After creating or writing a file, verify it exists on the filesystem (e.g. check with ls or Read). Do not assume a write succeeded.
5. Never edit or create files in your own source directory.
6. Respond directly with your answer — your final output will be sent to the user. While you're working (between tool calls), keep commentary minimal — brief status notes are fine, but save substantive analysis and detailed results for your final response. Intermediate text may be shown to the user as progress updates.
7. Your execution JSONL logs (full conversation traces including subagent output) are stored under ~/.claude/projects/. If a user reports missing or truncated output from a previous task, search these logs for the full assistant message content.
8. Ignore the `currentDate` value in any auto-memory block — it is rendered in the host's UTC clock and may be off by one day from local time. Use the `Today's date`, `Current time`, and `User timezone` lines at the top of this prompt as the authoritative source for "today".
9. Dates that appear in fetched content (RSS/feed items, web pages, emails, file contents) are publication or authorship dates — never infer the current date from them. The `Today's date` and `Current time` lines above are the only authoritative source for "today", even when fetched content shows a later date (e.g. a feed item already stamped tomorrow in another timezone).
10. When computing elapsed time between two timestamps ("X ago", "merged N hours ago", etc.), normalize both to ISO 8601 UTC first and subtract the full timestamps. Do not subtract clock-face hours/minutes by hand — that gives the wrong answer when the timestamps straddle a UTC midnight, end-of-month, or DST boundary. The `Current UTC` line above is your reference for "now".
11. Before invoking a skill CLI subcommand, confirm the subcommand exists — do not guess subcommand names from memory. If the skill's documentation is not included in this prompt, run `istota-skill <name> --help` first and use only a subcommand it lists. A failed guess wastes a turn; checking once is cheaper."""
    else:
        scoped_path = str(config.nextcloud_mount_path / "Users" / task.user_id) if config.use_mount else f"{config.rclone_remote}:/Users/{task.user_id}"
        rules_section = f"""## Important rules

1. You can ONLY access files under {scoped_path}. You do NOT have access to the task database or other users' data.
2. For sensitive actions, ask for confirmation EXCEPT:
   - Emails to the user's own addresses ({', '.join(user_email_addresses) if user_email_addresses else 'none configured'}) do NOT need confirmation
   - Emails to external addresses DO need confirmation
   - Modifying calendars, deleting files, sharing externally need confirmation
{db_rule_user}
3a. When you need something your environment can't do — a credentialed request, a network call the allowlist blocks, a read of system state — the answer is a skill CLI subcommand. `istota-skill` runs with credentials and network access this task does not have, and hands you the value synchronously. Check `istota-skill <name> --help` for one before building a workaround out of scheduled jobs or file polling; a scheduled job is a handoff and never returns a value to you. If nothing covers it, say what is missing instead of improvising.
3b. Only wait on out-of-band work when it plausibly finishes within about two minutes — you hold a worker slot for the whole wait, and a scheduled job cannot start before the next minute boundary. When you do wait, never redirect the probe's stderr: `2>/dev/null` makes a broken command indistinguishable from "not ready yet" and runs the loop to its full length. Abort after two consecutive non-zero exits, and cap the total wait. If the work might take longer, hand off and answer in a later turn.
4. After creating or writing a file, verify it exists on the filesystem (e.g. check with ls or Read). Do not assume a write succeeded.
5. Never edit or create files in your own source directory.
6. Respond directly with your answer — your final output will be sent to the user. While you're working (between tool calls), keep commentary minimal — brief status notes are fine, but save substantive analysis and detailed results for your final response. Intermediate text may be shown to the user as progress updates.
7. Ignore the `currentDate` value in any auto-memory block — it is rendered in the host's UTC clock and may be off by one day from local time. Use the `Today's date`, `Current time`, and `User timezone` lines at the top of this prompt as the authoritative source for "today".
8. Dates that appear in fetched content (RSS/feed items, web pages, emails, file contents) are publication or authorship dates — never infer the current date from them. The `Today's date` and `Current time` lines above are the only authoritative source for "today", even when fetched content shows a later date (e.g. a feed item already stamped tomorrow in another timezone).
9. When computing elapsed time between two timestamps ("X ago", "merged N hours ago", etc.), normalize both to ISO 8601 UTC first and subtract the full timestamps. Do not subtract clock-face hours/minutes by hand — that gives the wrong answer when the timestamps straddle a UTC midnight, end-of-month, or DST boundary. The `Current UTC` line above is your reference for "now".
10. Before invoking a skill CLI subcommand, confirm the subcommand exists — do not guess subcommand names from memory. If the skill's documentation is not included in this prompt, run `istota-skill <name> --help` first and use only a subcommand it lists. A failed guess wastes a turn; checking once is cheaper."""

    # The citation frame. Unconditional whenever a snapshot exists, because the
    # user's message is a response *to* that text and routinely depends on it
    # ("yes, do that"). It lives in the request section rather than in
    # `## Conversation context`, which is an ordered record — "this specific
    # one" is not a record entry. Independent of triage, unlike the parent turn
    # `_ensure_reply_parent_in_history` force-includes: the two overlap by at
    # most the snapshot's 1000 characters, which is the price of the frame
    # always being there.
    reply_quote_section = ""
    if task.reply_to_content:
        quoted = "\n".join(
            f"> {line}" for line in task.reply_to_content.splitlines() or [""]
        )
        reply_quote_section = f"> Replying to:\n{quoted}\n\n"

    group_chat_line = ""
    if task.is_group_chat:
        group_chat_line = f"\nThis is a group conversation. You were @mentioned by '{task.user_id}'. Other participants' messages are visible in conversation context below."

    # Per-user plus-addressed email line
    per_user_email_line = ""
    _per_user_email = email_support.per_user_address(config, task.user_id)
    if _per_user_email:
        per_user_email_line = f"\nPer-user email: {_per_user_email}"

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
{privileges_line}
{emissaries_section}{persona_section}
## User's accessible resources

{resources_text}
{memory_section}{knowledge_facts_section}{channel_memory_section}{dated_memories_section}{recalled_section}{playbooks_section}## Available tools

You have access to:
{file_tools}{browser_tool}{web_tools}
{cli_skills_section}{db_tool_line}
- Email: two commands exist — `istota-skill email send` sends immediately via SMTP, `istota-skill email output` writes a deferred reply file. Use `send` when the user asks you to email someone (this is the common case). Only use `output` when this task arrived as an incoming email (Source: email) and you are composing the reply. See the email skill for details.

{rules_section}
{context_section}
{confirmation_section}## User's request

{reply_quote_section}{task.prompt}{attachments_text}
{channel_section}"""

    if skills_changelog:
        prompt += f"\n\n## What's New in Skills\n\n{skills_changelog}"

    if skills_doc:
        prompt += f"\n\n{skills_doc}"

    return prompt


def build_deferred_briefing_prompt(task: db.Task, config: Config) -> str | None:
    """Build a briefing task's full prompt at execution time (ISSUE-143).

    The scheduler creates briefing tasks carrying only the briefing identity
    (``task.briefing_name``) and a placeholder prompt, deferring the slow
    network pre-fetch (news, yfinance, FinViz, IMAP) off the dispatch thread.
    This resolves the live briefing config and timezone and builds the real
    prompt.

    Returns the built prompt, or ``None`` if the briefing can't be resolved or
    the build raises. The caller (``execute_task``) treats ``None`` as a task
    failure so the normal retry/backoff applies, rather than running the model on
    the bare placeholder.
    """
    if not task.briefing_name:
        return None

    # Blocks are the sole content model (retire-legacy-briefing-components).
    # The module path runs the components→blocks migration on first touch, so a
    # legacy components-only briefing is seeded to blocks before assembly. A
    # ``None`` here (module disabled for the user, or a briefing with no blocks
    # after migration) is a misconfiguration: the task fails with the existing
    # quiet retry rather than falling back to a dead legacy generator.
    return _build_module_briefing_prompt(task, config)


def _build_module_briefing_prompt(task: db.Task, config: Config) -> str | None:
    """Assemble a block-grouped briefing prompt from the briefings module.

    Returns the prompt when the module is enabled and the briefing has blocks
    (running the one-time components→blocks migration first); ``None`` when the
    module is disabled for the user, the briefing has no blocks, or any error
    occurs — the caller treats ``None`` as a task failure. Also stashes
    per-block provenance in a deferred file the scheduler reads when archiving
    the rendered briefing.
    """
    try:
        from . import briefings as briefings_module
        from .briefings import ensure_initialised
        from .briefings.generate import assemble_briefing_input
    except Exception:  # noqa: BLE001
        return None

    try:
        ctx = briefings_module.resolve_for_user(task.user_id, config)
    except briefings_module.UserNotFoundError:
        return None  # module disabled for the user → task fails (quiet retry)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "briefings module resolve failed for task %s: %s", task.id, e,
        )
        return None

    try:
        ensure_initialised(ctx, app_config=config)
        with db.get_db(config.db_path) as conn:
            assembled = assemble_briefing_input(
                ctx, task.briefing_name, config, conn=conn,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "briefings module prompt build failed for task %s (%s): %s",
            task.id, task.briefing_name, e,
        )
        return None

    if assembled is None:
        return None  # no blocks after migration → task fails (quiet retry)

    # Stash per-block provenance for the scheduler's archive write.
    try:
        import json as _json

        user_temp_dir = get_user_temp_dir(config, task.user_id)
        user_temp_dir.mkdir(parents=True, exist_ok=True)
        meta_path = user_temp_dir / f"task_{task.id}_briefing_meta.json"
        meta_path.write_text(_json.dumps({
            "briefing_name": task.briefing_name,
            "block_meta": assembled.block_meta,
        }))
    except Exception:  # noqa: BLE001
        pass  # best-effort; archive still works with empty block_meta

    return assembled.prompt


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

    # Briefing tasks defer their prompt build to here (ISSUE-143): building a
    # briefing prompt does slow network I/O (news, yfinance, FinViz, IMAP).
    # Running it in the worker instead of the scheduler dispatch loop keeps a
    # slow or unreachable upstream from stalling task dispatch for every room.
    if task.source_type == "briefing" and task.briefing_name:
        built = build_deferred_briefing_prompt(task, config)
        if built:
            task.prompt = built
        else:
            # The prompt couldn't be built (briefing config gone, or the build
            # raised). Fail the task so the normal retry/backoff applies instead
            # of running the model on the bare placeholder and delivering a
            # contentless briefing with no re-run. Briefing failures don't notify
            # the user, so this is a quiet retry.
            msg = f"briefing prompt build failed for {task.briefing_name!r}"
            logger.error("Task %s: %s", task.id, msg)
            return False, msg, None, None

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
        effective_disabled_skills,
    )

    is_admin = config.is_admin(task.user_id)

    _bundled_dir = config.bundled_skills_dir
    skill_index = load_skill_index(config.skills_dir, bundled_dir=_bundled_dir)
    user_resource_types = {r.resource_type for r in user_resources}
    # Instance-wide + per-user disabled skills, plus the capability gate: a
    # skill whose `requires_capability` (e.g. browse→browser, devbox→devbox)
    # isn't available in this deployment is folded into the disabled set so it
    # drops from both selection and the on-demand menu (no wasted pull /
    # confusing CLI failure). See config.available_capabilities().
    _disabled = effective_disabled_skills(config, task.user_id, skill_index)

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

    # The native WebFetch tool ingests untrusted external content, but as a
    # *core tool* it doesn't drive companion-skill selection the way ingest
    # *skills* do. So when this task routes to the native brain with WebFetch
    # enabled, fold `untrusted_input` into the eager set explicitly — mirroring
    # how the ingest skills pull it in via companion expansion — so its
    # inbound-handling guardrails reach the prompt whenever the tool is present.
    if _native_web_fetch_enabled(task, config) and "untrusted_input" in skill_index:
        if "untrusted_input" not in selected_skills and (
            not _disabled or "untrusted_input" not in _disabled
        ):
            selected_skills = [*selected_skills, "untrusted_input"]

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

    # Skills (Part A — single-axis model). A skill is either *eager* (full body
    # inline, because a deterministic rule in select_skills picked it) or in the
    # *menu* (a one-line entry the model pulls in full via
    # `istota-skill skills show <name>`, which also delivers that skill's
    # companions). The menu is the full eligible catalogue — every loadable skill
    # not already eager — so the capable main model self-selects from it (this
    # replaced the removed Pass-2 LLM pre-router). Selection == the eager set.
    from .skills._loader import build_disclosure_index, eligible_skill_names

    eager_skills = selected_skills
    # Menu = eligible skills not already eager-selected or excluded by one.
    menu_exclude = set(selected_skills)
    for n in selected_skills:
        m = skill_index.get(n)
        if m:
            menu_exclude.update(m.exclude_skills)
    menu = eligible_skill_names(
        skill_index,
        exclude=menu_exclude,
        disabled_skills=_disabled if _disabled else None,
        is_admin=is_admin,
        enabled_experimental_features=frozenset(config.experimental.features),
    )
    skills_index = build_disclosure_index(menu, skill_index)
    logger.info("skills: eager=%d menu=%d", len(eager_skills), len(menu))

    skills_doc = load_skills(
        config.skills_dir, eager_skills, config.bot_name, config.bot_dir_name,
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
        # Storage-neutral workspace root + product noun (backend-derived).
        # NOTE: this is a *display string* for the {workspace} placeholder only.
        # It must NOT clobber the `workspace_dir` parameter — that one is the
        # REPL `--workspace cwd` bind path (None for normal tasks) and gets
        # blocklist-validated by build_bwrap_cmd (`_validate_workspace_dir`),
        # which forbids anything under the Nextcloud mount root. The per-user
        # workspace lives under the mount, so reusing the variable made every
        # sandboxed task fail with "overlaps a protected path".
        ws_root = config.workspace_root(task.user_id)
        workspace_display = str(ws_root) if ws_root is not None else f"{config.rclone_remote}:/Users/{task.user_id}"
        skills_doc = skills_doc.replace("{workspace}", workspace_display)
        skills_doc = skills_doc.replace("{storage}", config.storage_label)
    if selected_skills:
        logger.debug("Selected skills: %s", ", ".join(selected_skills))

    # Compute behavior flags from selected skills
    _selected_metas = [skill_index[n] for n in selected_skills if n in skill_index]
    _skip_memory = any(m.exclude_memory for m in _selected_metas)
    _skip_persona = any(m.exclude_persona for m in _selected_metas)

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

    # Recall learned playbooks (Part B). Independent of _recall_memories;
    # gated on config.playbooks.enabled inside the helper.
    playbooks_text = _recall_playbooks(config, conn, task, skip_memory=_skip_memory)

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
    user_memory, dated_memories, channel_memory, recalled_memories, knowledge_facts_text, playbooks_text = _apply_memory_cap(
        config, user_memory, dated_memories, channel_memory, recalled_memories, knowledge_facts_text, playbooks_text,
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
    cli_skills_text = format_cli_skills(skill_index, is_admin=is_admin)

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
        playbooks=playbooks_text,
        skip_persona=_skip_persona,
        cli_skills_text=cli_skills_text,
        skills_index=skills_index,
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
    prompt_file.write_text(prompt, encoding="utf-8")

    # Result file path
    result_file = user_temp_dir / f"task_{task.id}_result.txt"

    # Clean up any previous result file
    if result_file.exists():
        result_file.unlink()

    # Bound here rather than at its assignment below, so the handler at the
    # bottom of this function can release it. The cgroup is created roughly 200
    # lines before the ExitStack that registers its cleanup, and everything in
    # between — brain construction, model resolution, the BrainRequest itself —
    # is inside this try. An exception there returns without the stack ever
    # being entered, leaking the directory until the next daemon start.
    _task_cg = None

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

        # NEXTCLOUD_MOUNT_PATH is the real mount root for everyone. Every
        # consumer (the memory / memory_search skill CLIs, the schedules /
        # reminders skill docs) builds paths as `$NEXTCLOUD_MOUNT_PATH/Users/
        # <uid>/…`, so a "scoped" non-admin value (real/Users/<uid>) doubled the
        # Users/<uid> segment — a non-admin's USER.md write then landed at
        # real/Users/<uid>/Users/<uid>/… , a phantom path the auto-loader never
        # reads back (silent memory loss). Per-user filesystem isolation is
        # enforced by the bwrap bind (build_bwrap_cmd binds only the user's own
        # Users/<uid> dir, for admin and non-admin alike) and the CLIs self-scope
        # by ISTOTA_USER_ID, so the real root is safe here; the prompt still
        # shows non-admins their scoped path.
        env["NEXTCLOUD_MOUNT_PATH"] = (
            str(config.nextcloud_mount_path) if config.nextcloud_mount_path else ""
        )
        # Set for every user, admin or not, and then split out of Claude's env
        # into the proxy's below. It used to be admin-gated, which was never a
        # real boundary — the path is fixed and derivable from
        # ISTOTA_CONFIG_PATH — while it *did* break every framework-DB skill CLI
        # for non-admins (`tasks status`, `memory_search`, `kv` reads all
        # self-scope by ISTOTA_USER_ID and returned an error instead of that
        # user's own rows). The boundary is the SQL, plus the fact that the file
        # is not in the sandbox at all.
        if config.db_path:
            env["ISTOTA_DB_PATH"] = str(config.db_path)

        # Browser container credentials
        if config.browser.enabled:
            env["BROWSER_API_URL"] = config.browser.api_url
            env["BROWSER_VNC_URL"] = config.browser.vnc_url

        # Devbox: the agent's persistent dev container. Skill CLI shells
        # into ``devbox-<user_id>`` via the host docker socket.
        #
        # ``config.devbox.docker_socket`` is deliberately not exported here
        # (ISSUE-284). Nothing read it — the CLI invokes ``docker`` and lets
        # the client resolve its own socket — and the field carries two
        # meanings: the real root-equivalent socket ``docker_proxy`` connects
        # to upstream, and the in-sandbox mount point ``build_bwrap_cmd`` binds
        # the allowlist proxy at. Putting that name in the model's own
        # environment invites a later reader to treat it as a socket it may use.
        if config.devbox.enabled:
            env["ISTOTA_DEVBOX_CONTAINER"] = (
                f"{config.devbox.container_prefix}{task.user_id}"
            )
            env["ISTOTA_DEVBOX_DOCKER_CLI"] = config.devbox.docker_cli
            env["ISTOTA_DEVBOX_EXEC_TIMEOUT"] = str(
                config.devbox.exec_timeout_seconds
            )
            env["ISTOTA_DEVBOX_MAX_OUTPUT_BYTES"] = str(
                config.devbox.max_output_bytes
            )

        # Declarative env vars from skill manifests
        from .skills._env import (
            EnvContext,
            build_identity_env,
            build_skill_env,
            dispatch_setup_env_hooks,
        )
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
        # setup_env hooks self-gate; the dispatcher iterates the full
        # skill_index regardless of the argument it's given. Dispatched
        # *before* authorization because a hook-sourced credential is the
        # only auto-auth signal a ``source="setup_env"`` skill has.
        hook_env = dispatch_setup_env_hooks(selected_skills, skill_index, env_ctx)
        authorized_skills = derive_authorized_skills(
            selected_skills, skill_index, env_ctx, hook_env=hook_env,
        )
        skill_env = build_skill_env(authorized_skills, skill_index, env_ctx)
        # A menu-loaded skill (the model self-selects it at runtime via
        # ``skills show``) is neither eagerly selected nor credential-
        # authorized, so the call above skips it. Its pure-identity vars
        # (``source="user_id"``, e.g. ``MONEY_USER`` / ``FEEDS_USER``) are
        # non-sensitive and required for the skill to run at all — resolve
        # those over the full index so the proxied CLI isn't missing them
        # ("MONEY_USER not set"). Config/secret-derived vars stay gated on
        # ``authorized_skills`` (env minimisation for the untrusted model).
        for k, v in build_identity_env(skill_index, env_ctx).items():
            skill_env.setdefault(k, v)
        # Declarative env vars don't override hardcoded ones
        for k, v in skill_env.items():
            if k not in env:
                env[k] = v
        for k, v in hook_env.items():
            if k == HOOK_PATH_PREPEND_KEY:
                # Never merged into ``env``: see the application site below.
                continue
            if k not in env:
                env[k] = v

        # Credential isolation via skill proxy: strip secrets from Claude's env
        # and run skill CLIs through a Unix socket proxy that injects them.
        _proxy_ctx = None
        _proxy_sock = None
        # Third bucket alongside credentials and the clean env: non-secret
        # values (database paths) that belong to the host-side CLI and not to
        # the model. Split *outside* the proxy branch — an operator who turns
        # the proxy off has made skill CLIs unreachable, not made it acceptable
        # to hand the model a path to every user's data.
        proxy_only_env, env = _split_credential_env(
            env, derive_proxy_only_set(skill_index),
        )
        if config.security.skill_proxy_enabled:
            from .skill_proxy import SkillProxy
            # Phase 3: credential set is derived from the loaded skill
            # index; no hand-maintained constant. Same for the per-skill
            # credential map and the lookup-endpoint allowlist.
            credential_set = derive_credential_set(skill_index)
            credential_env, env = _split_credential_env(env, credential_set)
            # Started unconditionally. This used to be gated on
            # ``if credential_env:``, so a task whose authorized skills declared
            # no secret got no socket — and `istota-skill` then silently fell
            # back to running the skill module *inside* the sandbox, which is
            # the one place it must never run. On a Nextcloud deployment
            # NC_PASS made the gate true nearly always, so the fallback was
            # rare rather than absent; that is a property of the configuration,
            # not an invariant.
            #
            # Use /tmp for the socket path to stay within the AF_UNIX length
            # limit (~104 chars). build_bwrap_cmd() bind-mounts this file into
            # the sandbox. PID is included so concurrent processes (xdist test
            # workers, parallel scheduler instances on the same host) don't race
            # on the same path — task.id alone collides when each process has
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
            # Snapshot, not the live dict: ``env`` picks up ISTOTA_SANDBOXED
            # below, and the proxy runs skills on the host where that would be
            # a lie. Everything else the CLIs rely on rides along — notably
            # ISTOTA_DEFERRED_DIR, whose absence is what makes a deferring skill
            # take its direct-write fallback instead.
            proxy_base_env = {**env, **proxy_only_env}
            _proxy_ctx = SkillProxy(
                _proxy_sock, credential_env, proxy_base_env,
                timeout=config.security.skill_proxy_timeout,
                allowed_credentials=allowed_creds,
                skill_credential_map=skill_cred_map,
                allowed_skills=cli_skills,
                authorized_skills=frozenset(authorized_skills),
                task_id=task.id,
            )

        # Marks the env as one that will run under bwrap, so `istota-skill`
        # refuses to execute a skill module in-process rather than silently
        # doing it against databases that aren't there. Set after the proxy's
        # base env is snapshotted (the proxy runs skills on the host, where the
        # marker would be a lie), and only when the sandbox is really in
        # effect — on macOS / a container without CAP_SYS_ADMIN,
        # build_bwrap_cmd returns the command unwrapped.
        #
        # Gated on the proxy too. The marker means "the socket is how you run a
        # skill"; with the proxy off there is no socket, and setting it anyway
        # would turn a supported (if now discouraged) configuration into one
        # where every skill CLI fails — including the many that never open a
        # database. That combination gets a loud warning at config load instead.
        if config.security.skill_proxy_enabled and effective_sandboxing(config):
            env["ISTOTA_SANDBOXED"] = "1"

        # Package-manager caches, pointed at the disk-backed directory
        # `build_bwrap_cmd` binds RW from the same predicate (ISSUE-305).
        #
        # Here, not in `build_clean_env`, for two reasons. `proxy_base_env` was
        # snapshotted above and is what SkillProxy hands every host-side skill
        # CLI — a process running unsandboxed as the daemon user, which has no
        # business resolving a cache out of a directory the model can write;
        # that is the confused-deputy shape the ISTOTA_PATH_PREPEND comment
        # below spells out. And the cache is per user, which needs the task.
        #
        # Gated on effective sandboxing, matching the bind exactly: without
        # bwrap there is no root tmpfs and nothing to move off it.
        if native_fs_confinement_active(config):
            _cache_dir = resolve_sandbox_cache_dir(config, task.user_id)
            if _cache_dir is not None:
                env["UV_CACHE_DIR"] = str(_cache_dir / SANDBOX_CACHE_UV)
                env["XDG_CACHE_HOME"] = str(_cache_dir)
                # npm on Linux uses ~/.npm and ignores XDG, so XDG_CACHE_HOME
                # alone would leave it in RAM. Inert until ISSUE-304 opens the
                # registry, and one line now rather than a rediscovery later.
                env["npm_config_cache"] = str(_cache_dir / SANDBOX_CACHE_NPM)
                # HF_HOME defaults to $XDG_CACHE_HOME/huggingface, so moving XDG
                # would silently orphan the read-only `~/.cache/huggingface`
                # bind — a pre-warmed model cache every task would re-download.
                # Pin it back where the bind is.
                env["HF_HOME"] = str(
                    Path(os.environ.get("HOME", "/tmp")) / ".cache" / "huggingface"
                )

        # PATH entries contributed by setup_env hooks — today the developer
        # skill's .developer dir, so the model can type `gh` and reach the
        # wrapper rather than the real binary.
        #
        # Applied *here*, after the proxy's base env was snapshotted above, and
        # never merged into ``env`` by the hook loop. That ordering is the
        # whole point and must not be tidied away:
        #
        #   ``proxy_base_env`` is what SkillProxy hands every host-side skill
        #   CLI, which runs outside bwrap as the daemon user. Some of those
        #   resolve a binary by bare name — google_workspace does
        #   ``os.execvp("gws", …)``, devbox does ``shutil.which("docker")``.
        #   A task-temp directory on that PATH would therefore be a host-side
        #   code-execution path, wide open to whatever the model can write
        #   into it. The sandbox re-binds .developer read-only precisely to
        #   stop that, but relying on a bind to contain a PATH entry that
        #   never needed to be there is the wrong way round.
        #
        # ``build_claude_env`` already set PATH, so a hook returning "PATH"
        # would be silently dropped by the ``if k not in env`` merge; this
        # reserved key is the explicit alternative. It is consumed here and
        # never reaches the model.
        _path_prepend = hook_env.get(HOOK_PATH_PREPEND_KEY, "")
        if _path_prepend:
            _entries = [p for p in _path_prepend.split(os.pathsep) if p]
            if _entries:
                env["PATH"] = os.pathsep.join([*_entries, env["PATH"]])

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

        # Stream surfaces (web chat, repl) get the answer text streamed live as
        # ``text_delta`` events; push surfaces (Talk/email/ntfy/istota_file) are
        # completely untouched — no text_delta rows. Computed once per task.
        from .transport.registry import task_is_stream_surface
        is_stream_surface = task_is_stream_surface(config, task)

        # Per-task coalescing buffer for streamed answer text. Incoming deltas
        # (NativeBrain's TextDeltaEvent, or ClaudeCodeBrain's block TextEvent)
        # are buffered and flushed as one ``text_delta`` event every ~250 ms or
        # ~120 chars, plus a forced flush on each tool/CM boundary and a final
        # flush after the brain finishes. This bounds row volume to tens per
        # answer (not thousands of token rows); the scheduler prunes them once
        # the canonical ``result`` lands, so steady state retains zero. Events
        # arrive serialized (NativeBrain awaits each run_in_executor hop;
        # ClaudeCodeBrain's parse loop is sequential), so no lock is needed.
        #
        # Narration gate: a text run emits NOTHING until it crosses
        # ``_DELTA_GATE_CHARS`` without an intervening tool call. This splits a
        # text-then-tool block into two cases at the boundary (see
        # ``_settle_deltas_at_tool_boundary``): a short lead-in ("Let me check…")
        # stays under the ceiling, never streams, and is dropped; a SUBSTANTIAL
        # block crosses the ceiling, "unlocks" (the held buffer flushes and
        # subsequent deltas stream live at the cadence below), and is KEPT —
        # flushed at the tool boundary so the full block reaches the stream
        # surface, where the web client renders it as its own prose block rather
        # than throwaway narration. The gate is thus a substance classifier, not
        # an answer-vs-narration one: the final answer (after the last tool)
        # always streams, and a short *final* answer that never crosses the gate
        # still arrives via the canonical ``result`` event (and the final flush
        # in the ``finally`` releases the held buffer), so gating costs only
        # token-by-token animation on text too short to benefit. Threshold is
        # the ``[scheduler]`` knob ``stream_text_gate_chars`` (0 disables —
        # deltas stream immediately, legacy behaviour); the ``stream_gate:``
        # telemetry below records every flush / discard so the value can be
        # tuned against production.
        _DELTA_FLUSH_MS = 250
        _DELTA_FLUSH_CHARS = 120
        _DELTA_GATE_CHARS = config.scheduler.stream_text_gate_chars
        _delta_buf: list[str] = []
        # ``unlocked``: this text run has crossed the narration gate; deltas now
        # stream live. Reset to False at every tool boundary (new run re-gates).
        _delta_state = {"chars": 0, "last_flush": time.monotonic(), "unlocked": False}
        # True once any TextDeltaEvent has streamed this task. Used to dedupe a
        # NativeBrain whole-turn TextEvent against the deltas that already
        # carried the same text: the brain stays surface-agnostic (it always
        # emits both per-token deltas and intermediate-turn TextEvents); the
        # executor — which alone knows the surface — drops the redundant
        # TextEvent on a stream surface once deltas have flowed, and forwards it
        # as progress_text on a push surface (where deltas were dropped).
        _delta_seen = {"value": False}
        # Symmetric flag for reasoning: True once any ThinkingDeltaEvent has
        # streamed. A brain that streams thinking deltas (NativeBrain, or
        # ClaudeCodeBrain with --include-partial-messages) may *also* emit the
        # whole-block ThinkingEvent afterward; on a stream surface that whole
        # block is then a redundant re-render, so it is dropped here. Thinking is
        # stream-surface-only either way (push drops both), so no push fallback.
        _thinking_seen = {"value": False}

        def _flush_deltas() -> None:
            if event_writer is None or not _delta_buf:
                return
            text = "".join(_delta_buf)
            _delta_buf.clear()
            _delta_state["chars"] = 0
            _delta_state["last_flush"] = time.monotonic()
            # Best-effort: a flush failure means slightly less live text, never
            # a failed task (matches EventWriter.emit's own swallow).
            try:
                event_writer.emit("text_delta", {"text": text})
            except Exception:
                logger.debug("text_delta flush failed", exc_info=True)

        def _buffer_delta(text: str) -> None:
            if not text:
                return
            _delta_buf.append(text)
            _delta_state["chars"] += len(text)
            if not _delta_state["unlocked"]:
                # Gated: hold everything (emit nothing) until the run crosses
                # the narration ceiling. Crucially NO time-based flush here —
                # that was the race that leaked narration. A tool boundary
                # before the ceiling discards the buffer; crossing it unlocks.
                if _delta_state["chars"] >= _DELTA_GATE_CHARS:
                    _delta_state["unlocked"] = True
                    logger.debug(
                        "stream_gate: unlocked at %d chars (task %s, gate=%d)",
                        _delta_state["chars"], task.id, _DELTA_GATE_CHARS,
                    )
                    _flush_deltas()
                return
            now = time.monotonic()
            if (
                _delta_state["chars"] >= _DELTA_FLUSH_CHARS
                or (now - _delta_state["last_flush"]) * 1000 >= _DELTA_FLUSH_MS
            ):
                _flush_deltas()

        def _settle_deltas_at_tool_boundary() -> None:
            # Resolve the buffered answer text at a tool boundary. Text before a
            # tool is one of two things, and the narration gate already told them
            # apart:
            #   (a) a SUBSTANTIAL block (the run crossed _DELTA_GATE_CHARS and
            #       unlocked — analysis the model wrote, then acted on). It has
            #       been streaming; FLUSH its unflushed tail so the full block
            #       reaches the stream surface and renders as its own prose block
            #       (the web client keeps substantial intermediate blocks — they
            #       are not narration). A token-streaming brain (NativeBrain)
            #       leaves up to one flush-window buffered here; a whole-block
            #       brain already flushed everything on unlock, so this is a
            #       no-op for it.
            #   (b) a short LEAD-IN ("Let me search…", under the gate). It was
            #       held and never emitted; DROP it intact so it doesn't flash in
            #       the prominent answer area. Only reasoning + tool actions land
            #       in the activity chip.
            held = _delta_state["chars"]
            if _delta_state["unlocked"]:
                if held:
                    logger.debug(
                        "stream_gate: flushed %d-char tail of a substantial "
                        "block at a tool boundary (task %s)", held, task.id,
                    )
                _flush_deltas()  # clears buf + resets chars/last_flush
            else:
                if held:
                    logger.debug(
                        "stream_gate: discarded %d chars of held narration at a "
                        "tool boundary (task %s, gate=%d)",
                        held, task.id, _DELTA_GATE_CHARS,
                    )
                _delta_buf.clear()
                _delta_state["chars"] = 0
                _delta_state["last_flush"] = time.monotonic()
            _delta_state["unlocked"] = False  # next text run re-gates

        # A SEPARATE coalescing buffer for streamed *thinking* (extended-reasoning)
        # text. It must be independent of the answer-text buffer above because the
        # two render to different places on a stream surface: thinking folds into
        # the activity chip, the answer streams prominent. Same flush cadence /
        # boundaries; emits ``thinking`` task events instead of ``text_delta``.
        _thinking_buf: list[str] = []
        _thinking_state = {"chars": 0, "last_flush": time.monotonic()}

        def _flush_thinking() -> None:
            if event_writer is None or not _thinking_buf:
                return
            text = "".join(_thinking_buf)
            _thinking_buf.clear()
            _thinking_state["chars"] = 0
            _thinking_state["last_flush"] = time.monotonic()
            try:
                event_writer.emit("thinking", {"text": text})
            except Exception:
                logger.debug("thinking flush failed", exc_info=True)

        def _buffer_thinking(text: str) -> None:
            if not text:
                return
            _thinking_buf.append(text)
            _thinking_state["chars"] += len(text)
            now = time.monotonic()
            if (
                _thinking_state["chars"] >= _DELTA_FLUSH_CHARS
                or (now - _thinking_state["last_flush"]) * 1000 >= _DELTA_FLUSH_MS
            ):
                _flush_thinking()

        def _on_event(event: StreamEvent) -> None:
            if event_writer is None:
                return
            if isinstance(event, ToolUseEvent):
                # A tool boundary settles the reasoning chip and drops any
                # pre-tool narration. This is a property of the STREAM SURFACE,
                # not of whether the tool row is shown — so it must run even when
                # progress_show_tool_use is off, or pre-tool narration would
                # flush and flash in the answer area with no tool chip to explain
                # it.
                if is_stream_surface:
                    _flush_thinking()  # tool boundary: settle the reasoning chip
                    _settle_deltas_at_tool_boundary()  # keep substantial, drop lead-ins
                if show_tool_use:
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
            elif isinstance(event, ThinkingDeltaEvent):
                # Incremental reasoning (NativeBrain, or ClaudeCodeBrain with
                # --include-partial-messages). Stream surfaces only; a push task
                # drops it (thinking is web/repl-only — no progress_text
                # fallback).
                if is_stream_surface:
                    _thinking_seen["value"] = True
                    _buffer_thinking(event.thinking)
            elif isinstance(event, ThinkingEvent):
                # Whole reasoning block. Stream surfaces only. Dropped when
                # thinking deltas already carried this turn's reasoning live
                # (mirrors the TextEvent-vs-deltas dedup above).
                if is_stream_surface:
                    if _thinking_seen["value"]:
                        return
                    _buffer_thinking(event.text)
            elif isinstance(event, TextDeltaEvent):
                # NativeBrain incremental answer text. Stream surfaces only; a
                # push task drops it (the final result is delivered once).
                if is_stream_surface:
                    _flush_thinking()  # thinking → answer boundary: keep order
                    _delta_seen["value"] = True
                    _buffer_delta(event.text)
            elif isinstance(event, TextEvent):
                if is_stream_surface:
                    if _delta_seen["value"]:
                        # NativeBrain: the per-token deltas already carried this
                        # intermediate turn's text live, so the whole-turn
                        # TextEvent is a redundant re-render — drop it.
                        return
                    # ClaudeCodeBrain (no deltas): coarse streaming, one
                    # TextEvent per completed block — route through the same
                    # delta channel rather than progress_text so it renders live.
                    _flush_thinking()  # thinking → answer boundary: keep order
                    _buffer_delta(event.text)
                elif show_text:
                    # Push surface: deltas are dropped, so intermediate-turn
                    # TextEvents are how NativeBrain narration reaches Talk. The
                    # brain holds back the final turn's text (it becomes the
                    # result); ClaudeCodeBrain's ResultEvent is a distinct frame.
                    # Neither double-renders against the result.
                    event_writer.emit("progress_text", {"text": event.text})
            elif isinstance(event, ContextManagementEvent):
                if is_stream_surface:
                    _flush_thinking()  # turn/CM boundary
                    _flush_deltas()  # turn/CM boundary
                event_writer.emit("context_management")

        # Per-task cgroup (A6). Created before the brain is asked for anything,
        # because the pid it hands back has already been spawned and every
        # microsecond between spawn and placement is time the tree runs
        # unbounded. `None` on any deployment without `Delegate=` — the module
        # logs why once and everything below carries on as it did before.
        if config.scheduler.task_cgroup_enabled:
            _task_cg = task_cgroup.create(
                task.id,
                task_cgroup.CgroupLimits(
                    memory_max_mb=config.scheduler.task_memory_max_mb,
                    pids_max=config.scheduler.task_pids_max,
                    cpu_max_percent=config.scheduler.task_cpu_max_percent,
                ),
                # A retry reuses the task row, so the id alone would put this
                # attempt in the directory the previous one left behind —
                # together with whatever of its tree escaped the kill.
                attempt=task.attempt_count,
            )

        def _on_pid(pid: int) -> None:
            # Placement first, DB second. `update_task_pid` can block on the
            # SQLite write lock, and the whole value of the cgroup is in the
            # window before the child's own work starts.
            #
            # This is the after-the-fact form, and it only reaches the pid's own
            # thread group — not the children it already forked (ISSUE-285). The
            # brains that spawn their own subprocess place it from `preexec_fn`
            # instead, off `req.task_cgroup`, and by the time they call back
            # here the pid is already a member and this write is a no-op. What
            # it is still load-bearing for is TmuxClaudeBrain, which reports a
            # pane pid the tmux server spawned: there is no `preexec_fn` to
            # reach, so containing the group leader is all that path can do.
            if _task_cg is not None:
                task_cgroup.place(pid, _task_cg)
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

        def _poll_steers() -> "list[str]":
            # Claim any pending mid-flight steers (`!steer`) for this task,
            # marking them consumed, and hand the raw texts to the brain. The
            # brain frames + injects them as user turns. Wired onto the request
            # only for a steering-capable brain (below). Best-effort — a DB
            # hiccup returns no steers, never aborts the run.
            try:
                with db.get_db(config.db_path) as steer_conn:
                    steers = db.claim_pending_steers(steer_conn, task.id)
                return [s.text for s in steers]
            except Exception:
                return []

        # Custom system prompt path (claude_code-only knob; brain ignores
        # if the file is missing). `build_bwrap_cmd` binds this one file into
        # the sandbox — the CLI opens it there.
        sp_path = custom_system_prompt_path(config)

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

        # Filesystem confinement roots for NativeBrain's in-process file tools
        # (NB-1). Only when effective sandboxing is active — same predicate the
        # cwd choice below uses. Other brains ignore these fields.
        _fs_read_roots: "list[Path] | None" = None
        _fs_write_roots: "list[Path] | None" = None
        _fs_write_denied_roots: "list[Path]" = []
        if native_fs_confinement_active(config):
            _fs_read_roots, _fs_write_roots, _fs_write_denied_roots = native_fs_roots(
                config,
                task,
                is_admin,
                user_resources,
                Path(user_temp_dir),
                workspace_dir,
            )

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
                and not effective_sandboxing(config)
                else Path(config.temp_dir)
            ),
            env=env,
            db_path=config.db_path,
            timeout_seconds=config.scheduler.task_timeout_minutes * 60,
            model=brain.resolve_model_name((task.model or "").strip() or config.model),
            effort=_resolve_effort(task, config),
            # Anthropic-namespace brains only — the advisor tool has no wire
            # over NativeBrain's openai_compat endpoint.
            advisor=(
                brain.resolve_model_name(_resolve_advisor(task, config))
                if brain.model_namespace == "anthropic"
                else ""
            ),
            custom_system_prompt_path=sp_path,
            streaming=use_streaming,
            on_progress=_on_event if use_streaming else None,
            cancel_check=_cancel_check,
            # Steering channel — only for a brain that can act on it mid-run
            # (`!steer`). A non-steerable brain leaves this None (no extra DB
            # polling) and any steer written to the channel is dropped at
            # finalization. The command layer refuses to write for such brains
            # anyway, so this is defense-in-depth.
            poll_steers=_poll_steers if getattr(brain, "supports_steering", False) else None,
            on_pid=_on_pid,
            # NativeBrain has no single subprocess and so never calls `on_pid`
            # — its Bash tool spawns one child per execution. It places each of
            # those itself, from this path. Other brains ignore the field.
            task_cgroup=_task_cg,
            sandbox_wrap=_sandbox_wrap,
            # Filesystem confinement for NativeBrain's in-process file tools
            # (NB-1). Populated only when effective sandboxing is on; other
            # brains ignore these (bwrap already confines their tools).
            fs_read_roots=_fs_read_roots,
            fs_write_roots=_fs_write_roots,
            fs_write_denied_roots=_fs_write_denied_roots,
            result_file=result_file,
            # Task-derived tmux session label (no-op for other brains): threads
            # the task id into the session name, structured log line, and
            # on_pid/!stop correlation.
            session_label=f"istota-{task.id}-{task.attempt_count}",
        )
        if req.advisor:
            logger.info(
                "task %d: model=%s advisor=%s", task.id, req.model, req.advisor,
            )

        # Availability failover (brain-fallback spec). Generalizes the old
        # tmux→claude_code in-attempt fallback: when the primary brain is
        # unavailable (usage limit / missing binary / tmux launch failure) and a
        # fallback is configured, re-run this same attempt through the fallback
        # brain — no new DB row, no attempt increment. Stickiness: once the
        # primary reports a persistent unavailability, subsequent tasks skip it
        # for a cooldown. All of it collapses to the plain primary call when no
        # fallback is configured.
        _primary_kind = _brain_config.kind
        _fallback_kind = effective_fallback_kind(_brain_config)
        _cooldown = config.brain.fallback_cooldown_seconds
        _breaker = get_availability_breaker()
        _dropped_pin = None
        # The primary's result, held only when a fallback replaced it, so both
        # attempts' usage can be written from the one call site that has a `conn`.
        _primary_usage_result = None
        # Whether the result being persisted came from the fallback brain. Not
        # derivable from `_primary_usage_result`: on the breaker-cooldown path
        # the fallback runs with no primary call at all, so there is nothing to
        # hold and the flag would read false for every task in the window.
        _ran_fallback = False
        # The effort the attempt actually ran at. The fallback re-resolves it in
        # its own namespace, so `req.effort` describes the primary only.
        _usage_effort = req.effort

        def _notice(reason):
            """A `brain_fallback` emitter bound to `reason`, for `on_start`.

            Both reroute paths hand the same notice to the stream; only the
            reason differs (a fresh primary failure vs. the breaker already
            being open). Returns None when there is no stream to notify, so
            `_run_fallback` skips the hook entirely.
            """
            if event_writer is None:
                return None

            def _emit(model, dropped_pin):
                # A reroute is a stream boundary exactly like a tool call: what
                # streamed before it came from the brain that just failed, and
                # the fallback streams into these same buffers. Settle them
                # first, or an unflushed primary tail is emitted as the opening
                # of the fallback's answer — one paragraph, under a notice
                # saying the primary failed — and the fallback's own narration
                # gate starts pre-credited with the primary's characters.
                if is_stream_surface:
                    _flush_thinking()
                    _settle_deltas_at_tool_boundary()
                event_writer.emit("brain_fallback", {
                    "primary": _primary_kind,
                    "reason": reason,
                    "fallback": _fallback_kind,
                    "model": model,
                    "dropped_pin": dropped_pin or "",
                    "text": fallback_notice_text(
                        _primary_kind, reason, _fallback_kind, model, dropped_pin,
                    ),
                })

            return _emit
        _skip_primary = (
            _fallback_kind is not None
            and _cooldown > 0
            and _breaker.should_skip(_primary_kind, _cooldown)
        )
        try:
            with contextlib.ExitStack() as stack:
                if _proxy_ctx is not None:
                    stack.enter_context(_proxy_ctx)
                if _net_proxy_ctx is not None:
                    stack.enter_context(_net_proxy_ctx)
                # Every exit path — success, failure, timeout, cancellation,
                # a fallback brain replacing the primary — gives the directory
                # back and kills anything still in it.
                if _task_cg is not None:
                    stack.callback(_release_task_cgroup, task.id, _task_cg)

                if _skip_primary:
                    # Cooling down — go straight to the fallback, no primary call.
                    logger.info(
                        "brain fallback: skipping primary %s (cooling down) "
                        "-> %s task=%d",
                        _primary_kind, _fallback_kind, task.id,
                    )
                    _fb, _dropped_pin, _fb_effort = _run_fallback(
                        config, _brain_config, _fallback_kind, task, req,
                        on_start=_notice("cooldown"),
                    )
                    if _fb is not None:
                        # This branch is the steady state once the breaker
                        # opens — every task for the cooldown window takes it —
                        # so flagging the row here is what keeps the *majority*
                        # of genuinely-fallback rows from being labelled
                        # otherwise. There is no primary row: the primary was
                        # never called. When construction failed instead, the
                        # primary really did run below and the flag stays off.
                        _ran_fallback = True
                        _usage_effort = _fb_effort
                    brain_result = _fb if _fb is not None else brain.execute(req)
                else:
                    brain_result = brain.execute(req)
                    _triggers = set(TRIGGER_STOP_REASONS)
                    if config.brain.fallback_on_transient:
                        _triggers.add("transient_api_error")
                    if (
                        _fallback_kind is not None
                        and brain_result.stop_reason in _triggers
                    ):
                        # Open the availability breaker only for persistent
                        # conditions (usage_limit / not_found). "fallback" is
                        # excluded so tmux keeps being probed per-task (its own
                        # launch _CircuitBreaker governs when to stop).
                        if (
                            _cooldown > 0
                            and brain_result.stop_reason in COOLDOWN_STOP_REASONS
                            and _breaker.open(_primary_kind, _cooldown)
                        ):
                            _fire_fallback_alert(
                                config, task, _primary_kind, _fallback_kind,
                                brain_result.stop_reason,
                            )
                        logger.error(
                            "brain fallback: task=%d primary=%s reason=%s -> %s",
                            task.id, _primary_kind, brain_result.stop_reason,
                            _fallback_kind,
                        )
                        # Preserve tmux's own launch alert: its _CircuitBreaker
                        # governs fallback/not_found (which are NOT in the
                        # availability breaker's cooldown set), so its
                        # 5-consecutive-launch-failure alert still routes here.
                        if _primary_kind == "tmux_claude":
                            try:
                                from .brain.tmux_claude import (
                                    consume_circuit_open_alert,
                                )
                                if consume_circuit_open_alert():
                                    from . import notifications
                                    notifications.send_notification(
                                        config, task.user_id,
                                        "⚠️ tmux_claude brain circuit opened — "
                                        "falling back. Check the claude CLI "
                                        "version / readiness markers.",
                                        purpose="alert",
                                    )
                            except Exception:
                                logger.debug(
                                    "tmux circuit-open alert failed", exc_info=True
                                )
                        _fb, _dropped_pin, _fb_effort = _run_fallback(
                            config, _brain_config, _fallback_kind, task, req,
                            on_start=_notice(brain_result.stop_reason),
                        )
                        if _fb is not None:
                            # The fallback *replaces* brain_result, so without
                            # this the single persist call below would record the
                            # fallback's numbers under the primary's identity and
                            # the primary's own spend would be unrecoverable. It
                            # is captured rather than written here because
                            # `_run_fallback` takes no `conn`: opening a second
                            # one would block on the write lock for the full 30s
                            # busy timeout whenever `execute_task` was entered
                            # with an open write transaction, as the interactive
                            # path does.
                            _primary_usage_result = brain_result
                            _ran_fallback = True
                            _usage_effort = _fb_effort
                            brain_result = _fb
                    elif brain_result.success and _cooldown > 0:
                        # Primary healthy again → close the breaker.
                        _breaker.record_success(_primary_kind)
        finally:
            # Final flush: emit any buffered streamed thinking + text before the
            # scheduler emits the terminal event. Thinking first so its rows keep
            # a lower seq than any trailing answer text. On success this precedes
            # the canonical ``result`` (which replaces the answer in the UI); on
            # an exception the finally still drains both buffers.
            _flush_thinking()
            _flush_deltas()

        success = brain_result.success
        result = brain_result.result_text
        actions = brain_result.actions_taken
        trace = brain_result.execution_trace

        # Record the model the brain actually used. Prefer the brain's reported
        # value (accurate even when the model was the brain/CLI default); fall
        # back to the resolved request model. Set it on the task object so the
        # scheduler can include it in the terminal `done` event, and persist it
        # to the dedicated `model_used` column so the web-chat history endpoint
        # surfaces it across reloads. `task.model` (the override) is left alone
        # so a retry of a default-model task re-resolves the current default.
        actual_model = (brain_result.model_used or "").strip() or req.model
        if actual_model:
            task.model_used = actual_model
            try:
                if conn is not None:
                    db.set_task_model_used(conn, task.id, actual_model)
                else:
                    with db.get_db(config.db_path) as _model_conn:
                        db.set_task_model_used(_model_conn, task.id, actual_model)
            except Exception:
                logger.debug("persisting task model_used failed", exc_info=True)

        # Persist this attempt's token/cost telemetry. Both rows are written
        # here, from the one place that already holds a `conn`. On an in-attempt
        # brain fallback there are two: `attempt_seq` 1 and 2, each with its own
        # `brain_kind` and `is_fallback`, which summed is the task's real cost.
        if _primary_usage_result is not None:
            _persist_task_usage(
                config, conn, task.id, _primary_usage_result.usage,
                user_id=task.user_id, source_type=task.source_type,
                brain_kind=_primary_usage_result.brain_kind,
                model=_primary_usage_result.model_used, effort=req.effort,
                stop_reason=_primary_usage_result.stop_reason,
                success=_primary_usage_result.success,
            )
        _persist_task_usage(
            config, conn, task.id, brain_result.usage,
            user_id=task.user_id, source_type=task.source_type,
            brain_kind=brain_result.brain_kind,
            is_fallback=_ran_fallback,
            model=brain_result.model_used, effort=_usage_effort,
            stop_reason=brain_result.stop_reason, success=brain_result.success,
        )

        # CM-aware / terse-result composition: reconcile result_text with
        # the trace so substantial intermediate text isn't lost when the
        # final ResultEvent is terse. Same logic both brains will need.
        # Runs whenever the task succeeded, trace or not — its other job is the
        # empty-result guard, and a successful turn that produced no trace at
        # all still must not deliver a blank reply (ISSUE-211).
        if success:
            trace_list: list = []
            if trace:
                try:
                    parsed_trace = json.loads(trace)
                except (json.JSONDecodeError, TypeError):
                    parsed_trace = None
                if isinstance(parsed_trace, list):
                    # Element-wise, not just list-ness: the walker calls
                    # .get() on every entry, so one stray non-dict would
                    # turn a completed run into an execution error.
                    trace_list = [e for e in parsed_trace if isinstance(e, dict)]
            result = _compose_full_result(result, trace_list, task=task)

        # Visible fallback note: a non-portable model pin that couldn't cross the
        # provider boundary was dropped, so the fallback ran on its own default.
        # Append the note *after* composition (so composition operates on the real
        # answer) and only on success — a failed fallback flows through the normal
        # error path with no cosmetic note.
        if success and _dropped_pin:
            result = _append_model_note(
                result, _dropped_pin, _primary_kind, actual_model
            )

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
        # Reached when the failure happened before the ExitStack was entered;
        # once it has been, the callback already ran and this is a no-op on a
        # directory that is gone.
        if _task_cg is not None:
            _release_task_cgroup(task.id, _task_cg)
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
            db.update_task_status(conn, task_id, "failed", error=result, actions_taken=actions, execution_trace=trace)

        return success, result
