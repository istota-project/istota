"""Append-only JSONL transcript of one ``NativeBrain`` task attempt.

Nothing persisted the conversation the native brain holds with the model. When
a task finished, the message list, every tool result, every thinking block and
every compaction summary were garbage-collected, and what survived was built
for delivery rather than for inspection: ``tasks.execution_trace`` carries tool
*labels* and no tool output at all, ``task_events`` carries a capped payload and
only when streaming is on, and ``task_N_prompt.txt`` is the input rather than
the run. So a native task that produced a wrong answer could not be
reconstructed. ``ClaudeCodeBrain`` never had this problem — the ``claude`` CLI
writes its own session JSONL and ``build_bwrap_cmd`` binds it out of the sandbox
— so the asymmetry was accidental. The format here adapts pi's session store
(the same prior art ``agent/types.py`` already cites for ``prepareNextTurn``) to
istota's unit of work, which is a task *attempt* rather than an interactive
session.

**One file per attempt, not per task.** A retry re-executes the prompt from
scratch with a fresh message list, so two attempts are two runs and merging them
would produce a transcript that never existed. ``task_usage`` already draws the
line at ``(task_id, attempt_seq)``.

**Linear records, no ``id``/``parentId``.** pi's tree exists for ``/fork`` and
``/tree``, which are interactive affordances. A task attempt has no user at the
keyboard and no way to rewind, so order in the file is the order of the run.
Adding the two fields later is a format-version bump the reader can absorb,
which is why they are left out rather than added speculatively.

**Two rules keep the artifact bounded, and they are the difference between an
observability feature and a disk-filling one.** Images are never written as
bytes — a single screenshot is megabytes of base64, so an ``ImageContent``
serializes as a descriptor whose ``sha256`` still identifies two records as the
same image. Text is capped per content block, **head and tail** rather than head
alone, because a truncated build log's tail is where the error is and a
head-only cut reliably discards the one part anybody opens the file for. Tool
call arguments are capped separately into an honest marker object, since a
truncated *fragment* of a JSON object is worse than a marker saying so.
``result_text`` is deliberately uncapped: it is the deliverable, the same
reasoning that put ``result`` in ``events._UNCAPPED_EVENT_KINDS``.

**The writer never raises, and it never nags.** A task must not fail because a
log could not be written, so every public method is wrapped; the first failure
logs one warning, disables the writer and closes the handle, and every later
call is a no-op — a disk that filled up must not produce one warning per tool
call for the rest of the day. A single record that will not serialize costs that
record and not the session: it becomes a ``serialization_error`` line and the
run carries on. Writes are ``flush``ed and never ``fsync``ed; a daemon that dies
loses the records the OS had buffered, which is a cost worth paying to keep an
``fsync`` off the agent loop's hot path. pi makes the same trade.

**Permissions are the only content control.** Files are ``0600`` behind a
``0700`` directory, set through the open flags. There is no group-readable case
and no content-based redaction: a regex sweep for credentials would miss the
shapes it does not know and mangle legitimate content that resembles a token.
``api_key`` and ``extra_headers`` are never serialized and ``base_url`` reaches
the header as its host only, because an operator can put a token in a URL path.
The residual risk is stated rather than denied — a ``Bash`` call that cats a
credential file puts that credential in the log, and the retention window is how
long it stays there. That is already true of ``task_N_prompt.txt`` and of the
app log; an operator who cannot accept it sets the feature off.

:func:`sweep_session_logs` lives here rather than in the scheduler, on the
:mod:`istota.worktree_reaper` precedent: the delete rule and the write rule
belong in one file. It enforces **two independent rules**, and neither implies
the other. Age bounds how long a transcript is retrievable, which is a privacy
question. Bytes bound how much disk a burst of long agentic tasks can take from
the filesystem the framework database is writing to, which is an availability
one — on the reference deployment ``data/`` holds ``istota.db``, every module DB
and these logs, so a logging artifact that can fill it takes SQLite writes down
with it. :mod:`istota.sandbox_cache_sweeper` wrote the reasoning down first: a
rule phrased in days either keeps everything or throws away something minutes
old, because the growth arrives in bursts rather than at a rate.

**The ceiling is deployment-wide, not per user**, because the thing being
protected is a filesystem and a filesystem has no per-user quota. Under a
per-user ceiling the real limit is ``users x ceiling`` — a number that changes
whenever a user is added and that appears nowhere in the config. The cost is a
fairness question per-user eviction never faced, and the answer is
**largest-user-first, then oldest-within-that-user**. Plain global oldest-first
is the obvious rule and it inverts the outcome: the globally oldest files belong
to the *quietest* users precisely because they are quiet, so one user producing
a flood would evict everyone else's history to make room for their own fresh
output. Water-filling from the largest tree down trims the heaviest producer
toward the others before anybody else loses anything, and the two rules are
identical on a single-user deployment.

**Never a file being written.** A file whose mtime is inside
:data:`LIVE_WINDOW_SECONDS` is never evicted by the ceiling — the same guard
shape ``sandbox_cache_sweeper`` uses for a live writer, and cheaper here because
there is no task table to consult. A tree that cannot be brought under the
ceiling without touching those files reports ``still_over`` and stops, rather
than deleting a live session or looping.

Measurement is du-style, ``st_blocks * 512``, because a volume is filled by
blocks. Directory inodes are deliberately *not* counted, which is where this
diverges from ``sandbox_cache_sweeper.measure_cache``: a per-user directory is
overhead the sweep can never reclaim, so counting it would let a many-user
deployment sit permanently over a ceiling no eviction could clear. There are no
hardlinks here, so no inode deduplication is needed.

Enumerating user directories from disk is safe here in a way it is not in
``sandbox_cache_sweeper``, which takes its user list from ``config.users``
because its tree is bound read-write into a sandbox and an entry there is
model-plantable. This tree is bound into no sandbox at any path, so a directory
in it can only have been created by the writer.

stdlib-only leaf apart from :mod:`istota.llm.types`, which the serializer needs
for its ``isinstance`` dispatch: no config, no brain, no database, roots and
policy are parameters, and it never raises.
"""

import base64
import hashlib
import json
import logging
import os
import stat
import traceback
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from istota.llm.types import (
    AssistantMessage,
    Content,
    ImageContent,
    Message,
    TextContent,
    ThinkingContent,
    ToolCallContent,
    ToolResultMessage,
    Usage,
    UserMessage,
)

logger = logging.getLogger("istota.session.session_log")

# Bumped when a record's shape changes in a way a reader cannot absorb. Line 1
# carries it so an old file stays readable after the format moves on.
FORMAT_VERSION = 1

LOG_SUFFIX = ".jsonl"

# Bytes per block in the unit `st_blocks` is defined in. POSIX fixes it at 512
# regardless of the filesystem's own block size.
_BLOCK = 512

_GIB = 1024 ** 3

# Defaults, restated by `SessionLogConfig` in `config.py`. They live here too so
# a caller with no config — a test, a one-off script — gets the shipped policy.
DEFAULT_MAX_CONTENT_CHARS = 32768
DEFAULT_MAX_ARGS_CHARS = 8192
DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_TOTAL_GB = 2.0

# The ceiling is clamped to this. Below half a gigabyte the bound is under a
# single busy day's worth of transcripts, so every sweep would evict a file
# somebody is about to read.
MIN_MAX_TOTAL_GB = 0.5

# A file stamped inside this window is assumed to belong to a run happening now
# and is never evicted by the ceiling.
LIVE_WINDOW_SECONDS = 3600.0

# How much of an over-cap arguments object survives in the marker. Enough to
# recognise the call, far short of the cap it is standing in for.
_ARGS_PREVIEW_CHARS = 512

_SECONDS_PER_DAY = 86400.0


# --------------------------------------------------------------------------
# Policy and identity
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionLogPolicy:
    """What gets written and how much of it."""

    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS
    max_args_chars: int = DEFAULT_MAX_ARGS_CHARS
    include_thinking: bool = True


@dataclass(frozen=True)
class SessionLogIdentity:
    """Which run a file belongs to. Every field comes from the task, never from
    anything the model wrote."""

    task_id: int
    attempt: int
    user_id: str
    source_type: str = ""
    conversation_token: str = ""
    is_group_chat: bool = False


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

def _utcnow() -> datetime:
    """Indirection so a test can freeze the clock without patching stdlib."""
    return datetime.now(timezone.utc)


def _iso_ms(dt: datetime) -> str:
    """ISO 8601 UTC at millisecond precision: ``2026-08-31T14:22:01.993Z``.

    A naive datetime is read as UTC rather than as local time. Reading it as
    local would put a record's timestamp an hour or more away from the file name
    built from the same value.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _ts() -> str:
    return _iso_ms(_utcnow())


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def is_one_component(value: str) -> bool:
    """Whether *value* is a single, ordinary path component.

    ``user_id`` reaches the writer from the task row rather than from anything
    the model wrote, so this is defence in depth rather than the boundary. It is
    cheap, and it is the difference between a directory under the log root and
    an append somewhere else on the disk.
    """
    if not isinstance(value, str) or not value or value in (".", ".."):
        return False
    if "\x00" in value:
        return False
    if os.sep in value or (os.altsep and os.altsep in value):
        return False
    return value == os.path.basename(value)


def _short_suffix(session_id: str) -> str:
    """Four alphanumerics off the session id, for a colliding file name."""
    cleaned = "".join(ch for ch in str(session_id) if ch.isalnum()).lower()
    return cleaned[:4] if len(cleaned) >= 4 else uuid.uuid4().hex[:4]


def session_log_path(
    root: Path | str,
    ident: SessionLogIdentity,
    now: datetime,
    *,
    session_id: str = "",
) -> Path:
    """``{root}/{user_id}/{timestamp}_task-{id}-{attempt}.jsonl``.

    The timestamp is ISO 8601 UTC with ``:`` and ``.`` replaced by ``-``, as pi
    does, so lexical order is chronological order and the name is safe on any
    filesystem. ``task-{id}-{attempt}`` is what makes ``ls | grep task-4471``
    enough to find every attempt of a task without opening a file.

    A path that already exists gets a short suffix off *session_id* rather than
    being reused: a ``usage_limit`` reroute to a fallback brain can produce a
    second native run for the same ``(task_id, attempt)``, and overwriting the
    first would destroy the record of the run that failed.

    Raises ``ValueError`` for a ``user_id`` that is not a single path component.
    The writer catches it, since the writer never raises.
    """
    if not is_one_component(ident.user_id):
        raise ValueError(f"user_id is not a single path component: {ident.user_id!r}")
    stamp = _iso_ms(now).replace(":", "-").replace(".", "-")
    base = f"{stamp}_task-{int(ident.task_id)}-{int(ident.attempt)}"
    directory = Path(root) / ident.user_id

    candidate = directory / f"{base}{LOG_SUFFIX}"
    if not candidate.exists():
        return candidate

    suffix = _short_suffix(session_id)
    candidate = directory / f"{base}-{suffix}{LOG_SUFFIX}"
    n = 2
    while candidate.exists() and n < 1000:
        candidate = directory / f"{base}-{suffix}-{n}{LOG_SUFFIX}"
        n += 1
    return candidate


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------

def _cap_text(text: str, limit: int) -> tuple[str, bool, int]:
    """Head-and-tail truncation. Returns ``(text, truncated, chars_total)``.

    Head *and* tail: a long tool result's tail is usually where the error is.
    """
    total = len(text)
    if limit <= 0 or total <= limit:
        return text, False, total
    half = max(1, limit // 2)
    dropped = total - 2 * half
    note = f"\n… [truncated {dropped} chars] …\n"
    return text[:half] + note + text[-half:], True, total


def _image_descriptor(block: ImageContent) -> dict:
    """An image as identity and size, never as bytes.

    ``bytes`` is the *decoded* length, so it is what the image costs rather than
    what its base64 costs. The hash makes two records identifiable as the same
    image without either containing it.
    """
    data = block.data or ""
    try:
        raw = base64.b64decode(data, validate=False)
        decoded = True
    except Exception:
        # Bad padding. Report the encoded form's size rather than nothing, and
        # say that is what happened.
        raw = data.encode("utf-8", "backslashreplace")
        decoded = False
    descriptor = {
        "type": "image",
        "media_type": block.media_type,
        "display_name": block.display_name,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    if not decoded:
        descriptor["decode_error"] = True
    return descriptor


def _cap_arguments(arguments: Any, limit: int) -> Any:
    """The arguments dict, or an honest marker where it is over the cap.

    A marker rather than a clipped string, because a truncated fragment of a
    JSON object is worse than a record that says it dropped one.
    """
    try:
        encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    except Exception as exc:
        return {
            "_truncated": True,
            "_unserializable": True,
            "chars_total": 0,
            "preview": f"<{type(exc).__name__}>",
        }
    if limit <= 0 or len(encoded) <= limit:
        return arguments
    preview_chars = max(1, min(_ARGS_PREVIEW_CHARS, limit))
    return {
        "_truncated": True,
        "chars_total": len(encoded),
        "preview": encoded[:preview_chars],
    }


def serialize_content(block: Content, policy: SessionLogPolicy) -> dict | None:
    """One content block as a record fragment.

    ``None`` means the block is deliberately absent: today that is thinking
    under ``include_thinking = False``, which is a drop rather than a cap.
    """
    if isinstance(block, ThinkingContent):
        if not policy.include_thinking:
            return None
        text, truncated, total = _cap_text(block.thinking, policy.max_content_chars)
        record: dict = {"type": "thinking", "thinking": text}
        if truncated:
            record["truncated"] = True
            record["chars_total"] = total
        return record

    if isinstance(block, TextContent):
        text, truncated, total = _cap_text(block.text, policy.max_content_chars)
        record = {"type": "text", "text": text}
        if truncated:
            record["truncated"] = True
            record["chars_total"] = total
        return record

    if isinstance(block, ImageContent):
        return _image_descriptor(block)

    if isinstance(block, ToolCallContent):
        return {
            "type": "tool_call",
            "id": block.id,
            "name": block.name,
            "arguments": _cap_arguments(block.arguments, policy.max_args_chars),
        }

    # A block type this module has not learned yet. Recording something the
    # reader can act on beats a serialization_error line that loses the turn.
    return {
        "type": str(getattr(block, "type", "unknown")),
        "unrecognized": True,
        "repr": _cap_text(repr(block), policy.max_content_chars)[0],
    }


def _serialize_blocks(blocks: Iterable[Content], policy: SessionLogPolicy) -> list[dict]:
    out = []
    for block in blocks or ():
        serialized = serialize_content(block, policy)
        if serialized is not None:
            out.append(serialized)
    return out


def _serialize_usage(usage: Usage | None) -> dict | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "cost_usd": usage.cost_usd,
    }


def serialize_message(msg: Message, policy: SessionLogPolicy) -> dict:
    """One ``istota.llm.types`` message as a record fragment.

    Field names stay ``snake_case``, as they are in Python. Deliberately not
    renamed to pi's ``camelCase``: this is istota's format, and a reader that
    half-matches pi's is worse than one that plainly does not.
    """
    if isinstance(msg, UserMessage):
        return {"role": "user", "content": _serialize_blocks(msg.content, policy)}

    if isinstance(msg, AssistantMessage):
        return {
            "role": "assistant",
            "model": msg.model,
            "stop_reason": msg.stop_reason,
            "error_message": msg.error_message,
            "usage": _serialize_usage(msg.usage),
            "content": _serialize_blocks(msg.content, policy),
        }

    if isinstance(msg, ToolResultMessage):
        return {
            "role": "tool_result",
            "tool_call_id": msg.tool_call_id,
            "tool_name": msg.tool_name,
            "is_error": bool(msg.is_error),
            "content": _serialize_blocks(msg.content, policy),
        }

    return {
        "role": str(getattr(msg, "role", "unknown")),
        "unrecognized": True,
        "content": [],
        "repr": _cap_text(repr(msg), policy.max_content_chars)[0],
    }


def _count_truncations(obj: Any) -> int:
    """Whether anything in a record was capped, so ``result`` can report it."""
    if isinstance(obj, dict):
        found = 1 if (obj.get("truncated") is True or obj.get("_truncated") is True) else 0
        for value in obj.values():
            found += _count_truncations(value)
        return found
    if isinstance(obj, list):
        return sum(_count_truncations(value) for value in obj)
    return 0


# --------------------------------------------------------------------------
# The writer
# --------------------------------------------------------------------------

class SessionLogWriter:
    """One file, one task attempt, append-only, and it never raises.

    ``root=None`` (or ``enabled=False``) is the disabled writer: every method is
    a no-op and :attr:`path` is ``None``. That is how the feature switches off,
    so the caller has no ``if self._log is not None`` at eight call sites.
    """

    def __init__(
        self,
        root: Path | str | None,
        ident: SessionLogIdentity,
        policy: SessionLogPolicy,
        *,
        enabled: bool = True,
    ) -> None:
        self._ident = ident
        self._policy = policy
        self._root = Path(root) if root is not None else None
        self._disabled = self._root is None or not enabled
        self._fh = None
        self._path: Path | None = None
        self._truncated = 0
        self._warned = False
        self.session_id = "" if self._disabled else str(uuid.uuid4())

    # -- state -------------------------------------------------------------

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def truncated_records(self) -> int:
        return self._truncated

    @property
    def active(self) -> bool:
        """Whether a record written now would land in a file."""
        return not self._disabled and self._fh is not None

    # -- lifecycle ---------------------------------------------------------

    def open(self, header: dict | None = None) -> None:
        """Create the file and write the ``session`` record.

        *header* carries what the caller knows and this module must not import
        to find out: the brain, the provider, the model, the effort. ``type``,
        ``v`` and ``ts`` are this module's and cannot be overridden.
        """
        if self._disabled or self._fh is not None:
            return
        try:
            # Before any mkdir, not just before the open: `{root}/../escape`
            # creates a directory outside the root whether or not a file ever
            # lands in it.
            if not is_one_component(self._ident.user_id):
                raise ValueError(
                    f"user_id is not a single path component: {self._ident.user_id!r}"
                )
            directory = Path(self._root) / self._ident.user_id
            os.makedirs(self._root, mode=0o700, exist_ok=True)
            os.makedirs(directory, mode=0o700, exist_ok=True)
            self._fh, self._path = self._open_exclusive()
        except Exception as exc:
            self._fail("could not open", exc)
            return

        record = {
            "type": "session",
            "v": FORMAT_VERSION,
            "ts": _ts(),
            "session_id": self.session_id,
            "task_id": self._ident.task_id,
            "attempt": self._ident.attempt,
            "user_id": self._ident.user_id,
            "source_type": self._ident.source_type,
            "conversation_token": self._ident.conversation_token,
            "is_group_chat": bool(self._ident.is_group_chat),
        }
        if header:
            record.update({k: v for k, v in header.items() if k not in ("type", "v", "ts")})
        self._write(record)

    def _open_exclusive(self):
        """``O_EXCL`` so a colliding name is never overwritten, only renamed."""
        now = _utcnow()
        last: Exception | None = None
        for _ in range(8):
            path = session_log_path(self._root, self._ident, now, session_id=self.session_id)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:  # lost the race; take the next name
                last = exc
                continue
            handle = os.fdopen(fd, "w", encoding="utf-8", errors="backslashreplace")
            return handle, path
        raise last if last is not None else OSError("could not pick a session log name")

    def close(self) -> None:
        handle, self._fh = self._fh, None
        if handle is None:
            return
        try:
            handle.flush()
            handle.close()
        except Exception as exc:
            # Nothing a caller can do, and the records are already on their way.
            logger.debug("session log: close failed for %s (%s)", self._path, exc)

    # -- records -----------------------------------------------------------

    def context(
        self,
        system_prompt: str,
        tools: Sequence[str],
        schema_sha: str,
        **extra: Any,
    ) -> None:
        """The system prompt and the tool surface, recorded once.

        Tool *names* plus a hash over the sorted schema JSON: the full schemas
        are large, identical across nearly every task, and their drift is what
        the hash is for.
        """

        def build() -> dict:
            text, truncated, total = _cap_text(system_prompt or "", self._policy.max_content_chars)
            body: dict = {
                "system_prompt": text,
                "tools": list(tools or ()),
                "tools_schema_sha256": schema_sha or "",
            }
            if truncated:
                body["truncated"] = True
                body["chars_total"] = total
            body.update({k: v for k, v in extra.items() if k not in ("type", "ts")})
            return body

        self._record("context", build)

    def message(self, msg: Message) -> None:
        self._record("message", lambda: {"message": serialize_message(msg, self._policy)})

    def compaction(self, **fields: Any) -> None:
        self._record("compaction", lambda: dict(fields))

    def steer(self, text: str) -> None:
        def build() -> dict:
            capped, truncated, total = _cap_text(str(text), self._policy.max_content_chars)
            body: dict = {"text": capped}
            if truncated:
                body["truncated"] = True
                body["chars_total"] = total
            return body

        self._record("steer", build)

    def nudge(self, **fields: Any) -> None:
        self._record("nudge", lambda: dict(fields))

    def error(self, exc: BaseException) -> None:
        def build() -> dict:
            formatted = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            message, _, _ = _cap_text(str(exc), self._policy.max_content_chars)
            tb, _, _ = _cap_text(formatted, self._policy.max_content_chars)
            return {"kind": type(exc).__name__, "message": message, "traceback": tb}

        self._record("error", build)

    def result(self, **fields: Any) -> None:
        """The terminal record. ``result_text`` is deliberately not capped."""
        self._record("result", lambda: dict(fields))

    # -- plumbing ----------------------------------------------------------

    def _record(self, kind: str, build: Callable[[], dict]) -> None:
        if self._disabled or self._fh is None:
            return
        try:
            body = build()
        except Exception as exc:
            # One unserializable object must not end the session log.
            self._write(
                {
                    "type": "serialization_error",
                    "ts": _ts(),
                    "record_type": kind,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return
        record = {"type": kind, "ts": _ts()}
        record.update(body)
        self._write(record)

    def _write(self, record: dict) -> None:
        if self._disabled or self._fh is None:
            return
        try:
            line = json.dumps(record, ensure_ascii=False)
        except Exception as exc:
            record = {
                "type": "serialization_error",
                "ts": _ts(),
                "record_type": record.get("type") if isinstance(record, dict) else None,
                "error": f"{type(exc).__name__}: {exc}",
            }
            line = json.dumps(record, ensure_ascii=False)
        try:
            self._fh.write(line + "\n")
            self._fh.flush()
        except Exception as exc:
            self._fail("could not write to", exc)
            return
        if _count_truncations(record):
            self._truncated += 1

    def _fail(self, what: str, exc: Exception) -> None:
        """One warning, then silence for the rest of the run."""
        self._disabled = True
        if not self._warned:
            self._warned = True
            logger.warning(
                "Session log disabled for task %s attempt %s: %s %s (%s: %s)",
                self._ident.task_id,
                self._ident.attempt,
                what,
                self._path or self._root,
                type(exc).__name__,
                exc,
            )
        handle, self._fh = self._fh, None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SweepResult:
    """What one sweep did.

    ``deleted_size > 0`` is the signal ``doctor`` reads to say the configured
    retention is not the retention actually in force.
    """

    deleted_age: int = 0
    deleted_size: int = 0
    dirs_removed: int = 0
    bytes_after: int = 0
    still_over: bool = False
    errors: int = 0


@dataclass
class _Candidate:
    """One evictable file: where it is, what it costs, when it was last written."""

    path: Path
    size: int
    mtime: float


def _scan_user_dir(directory: Path) -> tuple[int, list[_Candidate], int]:
    """``(bytes, jsonl candidates, errors)`` for one user's tree.

    Everything under the directory counts toward the bytes — an operator's
    stray file fills the volume like any other — but only ``*.jsonl`` is a
    candidate for eviction, because the sweep deletes only what it wrote.
    Directory inodes are not counted; see the module docstring.
    """
    total = 0
    candidates: list[_Candidate] = []
    errors = 0

    def _on_error(exc: OSError) -> None:
        nonlocal errors
        errors += 1
        logger.debug(
            "session log sweep: cannot read %s (%s)", getattr(exc, "filename", "?"), exc
        )

    for dirpath, _dirnames, filenames in os.walk(directory, onerror=_on_error, followlinks=False):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                info = os.lstat(full)
            except OSError:
                errors += 1
                continue
            size = info.st_blocks * _BLOCK
            total += size
            if name.endswith(LOG_SUFFIX) and not stat.S_ISLNK(info.st_mode):
                candidates.append(_Candidate(Path(full), size, info.st_mtime))
    return total, candidates, errors


def _user_dirs(root: Path) -> tuple[list[Path], int]:
    """The per-user directories under *root*, and how many entries could not be
    read. A symlink is never followed: nothing outside the root is swept."""
    errors = 0
    found: list[Path] = []
    try:
        entries = sorted(root.iterdir())
    except FileNotFoundError:
        return [], 0
    except OSError as exc:
        logger.debug("session log sweep: cannot read root %s (%s)", root, exc)
        return [], 1
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
        except OSError:
            errors += 1
            continue
        found.append(entry)
    return found, errors


def sweep_session_logs(
    root: Path | str,
    *,
    retention_days: int,
    max_total_gb: float,
    now: float,
    floor_gb: float = MIN_MAX_TOTAL_GB,
) -> SweepResult:
    """Apply the age rule, then the deployment-wide ceiling. Never raises.

    The two rules are independent and the caller's gate must be ``or``:
    ``retention_days = 0`` keeps everything indefinitely by age and still wants
    the disk bound in force, and ``max_total_gb = 0`` drops the bound while the
    age rule carries on.

    *floor_gb* is the clamp :data:`MIN_MAX_TOTAL_GB` describes, exposed so a
    test can exceed a ceiling without writing half a gigabyte. Production
    callers take the default.
    """
    root = Path(root)
    deleted_age = deleted_size = dirs_removed = 0

    directories, errors = _user_dirs(root)

    # -- age, for privacy --------------------------------------------------
    if retention_days > 0:
        cutoff = now - retention_days * _SECONDS_PER_DAY
        for directory in directories:
            try:
                # Read before the deletions: unlinking a file stamps its parent
                # `now`, and the gate below would then never fire for a
                # directory this sweep emptied.
                dir_mtime = directory.lstat().st_mtime
            except OSError:
                errors += 1
                continue

            _bytes, candidates, scan_errors = _scan_user_dir(directory)
            errors += scan_errors
            for candidate in candidates:
                if candidate.mtime >= cutoff:
                    continue
                try:
                    candidate.path.unlink()
                    deleted_age += 1
                except OSError as exc:
                    errors += 1
                    logger.debug("session log sweep: cannot remove %s (%s)", candidate.path, exc)

            # Only once the directory itself has gone untouched past the
            # window: `open` creates it and writes its first record a moment
            # later, and a tick in between must not rmdir it out from under an
            # in-flight task. Same gate `cleanup_old_temp_files` carries.
            if dir_mtime < cutoff:
                try:
                    directory.rmdir()  # only succeeds if empty
                    dirs_removed += 1
                except OSError:
                    pass

    # -- bytes, for the disk ----------------------------------------------
    total = 0
    per_user: dict[Path, int] = {}
    evictable: dict[Path, list[_Candidate]] = {}
    live_cutoff = now - LIVE_WINDOW_SECONDS

    for directory in directories:
        if not directory.is_dir():
            continue  # removed by the age pass above
        size, candidates, scan_errors = _scan_user_dir(directory)
        errors += scan_errors
        per_user[directory] = size
        total += size
        # Oldest first within a user, and never a file a run may be writing now.
        evictable[directory] = sorted(
            (c for c in candidates if c.mtime <= live_cutoff),
            key=lambda c: (c.mtime, str(c.path)),
        )

    still_over = False
    if max_total_gb > 0:
        ceiling = int(max(max_total_gb, floor_gb) * _GIB)
        while total > ceiling:
            # Largest-user-first: water-filling trims the heaviest producer
            # toward the others rather than evicting a quiet user's whole
            # history — which is what plain global oldest-first does, since the
            # globally oldest files belong to the quietest users.
            heaviest = None
            for directory in sorted(per_user, key=lambda d: (-per_user[d], str(d))):
                if evictable.get(directory):
                    heaviest = directory
                    break
            if heaviest is None:
                # Everything left is live, or is not ours to delete.
                still_over = True
                break

            victim = evictable[heaviest].pop(0)
            try:
                victim.path.unlink()
            except OSError as exc:
                errors += 1
                logger.debug("session log sweep: cannot remove %s (%s)", victim.path, exc)
                continue
            deleted_size += 1
            total -= victim.size
            per_user[heaviest] -= victim.size

    return SweepResult(
        deleted_age=deleted_age,
        deleted_size=deleted_size,
        dirs_removed=dirs_removed,
        bytes_after=total,
        still_over=still_over,
        errors=errors,
    )
