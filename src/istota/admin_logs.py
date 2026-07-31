"""Read-only log access for the admin web UI (ISSUE-203).

Two sources, deliberately no more:

``app``
    The rotating application log file — ``[logging] file`` plus its
    ``RotatingFileHandler`` siblings (``istota.log``, ``istota.log.1``, …),
    treated as one continuous stream newest-first. On a stock server deploy
    (``output = "both"``) this *is* the scheduler/poller/brain output, which is
    what the issue asks for.

``tasks``
    The ``task_logs`` table — per-task lifecycle (claimed, completed, retrying,
    failed) written by the scheduler.

Per-task *execution traces* are deliberately absent here: they already have a
reader at ``/admin/tasks/{id}/events``, and the JSONL session store under
``~/.claude/projects/`` is not path-scoped to anything this module can confine.

Security posture. A client never supplies a path — it supplies a **source id**
validated against the enumerated set, and the only place a path is derived is
:func:`resolve_app_log_chain`, which confines every candidate to the resolved
log directory (symlink-resolved) before it is read. Reads are byte-bounded in
both directions so a large log can neither be loaded whole nor scanned
unboundedly by a filter that matches nothing.

Cross-user exposure is real and intended: ``task_logs`` embeds truncated task
results, so an admin reading this source sees other users' task output. That is
the point of a gated read path — every record carries its ``user_id`` so the
admin can see whose it is, and the source is filterable by user.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Config

# Deliberately no module-level `logger`: three public functions take a
# `logger=` filter argument, which would shadow it inside exactly the code most
# likely to want to log. Nothing here logs — a read failure is returned as an
# empty page and reported by the caller.

# Ordered low → high. `level_rank` uses the index, so order is the contract.
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_LEVEL_ALIASES = {"WARN": "WARNING", "FATAL": "CRITICAL"}

# Mirrors the file formatter in `logging_setup.setup_logging`:
#   "%(asctime)s %(levelname)-5s [%(name)-18s] %(message)s"  datefmt "%Y-%m-%d %H:%M:%S"
# A line that does not match is a continuation (traceback body) and is folded
# into the preceding record rather than dropped.
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r"\s+(?P<level>[A-Z][A-Z0-9_]*)"
    r"\s+\[(?P<logger>[^\]]*)\]"
    r" ?(?P<msg>.*)$"
)

# Rotation siblings written by RotatingFileHandler: `<name>.1` … `<name>.N`.
# Compressed siblings (logrotate's `.gz`) are excluded — seeking into them is
# not worth the complexity for a viewer whose value is recent history.
_ROTATION_SUFFIX_RE = re.compile(r"^\.(\d+)$")

_DEFAULT_WINDOW_BYTES = 256 * 1024
_DEFAULT_MAX_SCAN_BYTES = 8 * 1024 * 1024
_DEFAULT_TAIL_MAX_BYTES = 1024 * 1024
# Ceiling the tail window may grow to while hunting for a record boundary. Above
# this, a single record is emitted split (or an unterminated line skipped)
# rather than reading unboundedly to keep it whole.
_TAIL_MAX_WINDOW_BYTES = 8 * 1024 * 1024

_MAX_LIMIT = 1000


def normalize_level(raw: str | None) -> str:
    """Uppercase a level name, folding known aliases.

    An unrecognized level is preserved rather than remapped: it is more honest
    to show a level we do not model than to relabel it as something we do.
    """
    if not raw:
        return "INFO"
    upper = raw.strip().upper()
    return _LEVEL_ALIASES.get(upper, upper)


def level_rank(level: str | None) -> int:
    """Severity rank for filtering. Unknown levels rank above CRITICAL.

    Ranking an unknown level *high* is the fail-open choice: a `min_level`
    filter hides things, and hiding a record because we could not classify it
    is how a viewer silently loses the one line that mattered.
    """
    name = normalize_level(level)
    try:
        return LEVELS.index(name)
    except ValueError:
        return len(LEVELS)


@dataclass(frozen=True)
class LogRecord:
    """One log entry from either source.

    ``cursor`` is opaque and monotonic *within a source*: a byte position for
    the file source, a row id for the DB source. It is never a path.
    """

    cursor: str
    level: str
    message: str
    timestamp: str | None = None
    logger: str | None = None
    task_id: int | None = None
    user_id: str | None = None
    source_type: str | None = None

    def to_dict(self) -> dict:
        return {
            "cursor": self.cursor,
            "timestamp": self.timestamp,
            "level": self.level,
            "logger": self.logger,
            "message": self.message,
            "task_id": self.task_id,
            "user_id": self.user_id,
            "source_type": self.source_type,
        }


@dataclass(frozen=True)
class LogSource:
    id: str
    label: str
    kind: str  # "file" | "db"
    description: str
    available: bool
    detail: str = ""
    time_basis: str = "utc"  # "utc" | "server-local" — the UI labels rather than converts
    path: str | None = None
    bytes: int = 0
    files: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "description": self.description,
            "available": self.available,
            "detail": self.detail,
            "time_basis": self.time_basis,
            "path": self.path,
            "bytes": self.bytes,
            "files": self.files,
        }


@dataclass(frozen=True)
class LogPage:
    """A page of records, always ordered oldest-first (reading order).

    ``next_before`` is the cursor to pass back for the previous (older) page,
    or ``None`` once the start of the source is reached. ``truncated`` says the
    scan budget was spent before ``limit`` was filled — distinct from "there is
    genuinely nothing older", which is ``next_before is None``.
    """

    records: list[LogRecord] = field(default_factory=list)
    next_before: str | None = None
    tail_cursor: str | None = None
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "records": [r.to_dict() for r in self.records],
            "next_before": self.next_before,
            "tail_cursor": self.tail_cursor,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class LogTail:
    """Records appended since a cursor.

    ``reset`` marks that the underlying source restarted (the live file was
    rotated out from under us), so the client should clear its buffer rather
    than append to a transcript that no longer continues.
    """

    records: list[LogRecord] = field(default_factory=list)
    cursor: str = ""
    reset: bool = False

    def to_dict(self) -> dict:
        return {
            "records": [r.to_dict() for r in self.records],
            "cursor": self.cursor,
            "reset": self.reset,
        }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_log_line(line: str) -> LogRecord | None:
    """Parse one formatted line, or ``None`` if it is a continuation line."""
    match = _LINE_RE.match(line)
    if not match:
        return None
    return LogRecord(
        cursor="",
        timestamp=match.group("ts").replace(" ", "T"),
        level=normalize_level(match.group("level")),
        logger=match.group("logger").strip() or None,
        message=match.group("msg").rstrip(),
    )


def _matches(
    rec: LogRecord,
    *,
    min_level: str | None,
    q: str | None,
    logger_prefix: str | None,
) -> bool:
    if min_level and level_rank(rec.level) < level_rank(min_level):
        return False
    if logger_prefix and not (rec.logger or "").startswith(logger_prefix):
        return False
    if q:
        needle = q.lower()
        haystack = f"{rec.logger or ''}\n{rec.message}".lower()
        if needle not in haystack:
            return False
    return True


# ---------------------------------------------------------------------------
# File source
# ---------------------------------------------------------------------------


def resolve_app_log_chain(config: "Config") -> list[Path]:
    """The app log file and its rotation siblings, newest-first.

    This is the single point where a path is derived, so it is where
    containment lives: every candidate is symlink-resolved and must still sit
    in the resolved log directory. A sibling that escapes (a planted symlink to
    ``/etc/shadow`` named ``istota.log.1``) is dropped, not read.
    """
    log_cfg = config.logging
    if log_cfg.output not in ("file", "both") or not log_cfg.file:
        return []

    try:
        primary = Path(log_cfg.file)
        parent = primary.parent.resolve(strict=False)
    except OSError:
        return []

    chain: list[Path] = []
    rotated: list[tuple[int, Path]] = []
    stem = primary.name

    def _contained(path: Path) -> bool:
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return False
        try:
            resolved.relative_to(parent)
        except ValueError:
            return False
        return resolved.is_file()

    if _contained(primary):
        chain.append(primary)

    try:
        siblings = list(parent.iterdir())
    except OSError:
        siblings = []

    for candidate in siblings:
        if not candidate.name.startswith(stem + "."):
            continue
        match = _ROTATION_SUFFIX_RE.match(candidate.name[len(stem):])
        if not match:
            continue
        if _contained(candidate):
            rotated.append((int(match.group(1)), candidate))

    rotated.sort(key=lambda pair: pair[0])
    chain.extend(path for _, path in rotated)
    return chain


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _split_lines(buf: bytes, base: int) -> list[tuple[int, int, str]]:
    """Split a byte buffer into ``(start, end, text)`` triples.

    Offsets are byte offsets from the file start, so they stay exact under
    multi-byte UTF-8 — which is why the buffer is split as bytes and each line
    decoded individually rather than decoding the whole window first. ``end``
    is past the trailing newline. A final fragment with no newline is dropped;
    callers only ever pass windows whose upper bound is a line boundary.
    """
    out: list[tuple[int, int, str]] = []
    pos = 0
    while True:
        nl = buf.find(b"\n", pos)
        if nl == -1:
            break
        text = buf[pos:nl].decode("utf-8", errors="replace").rstrip("\r")
        out.append((base + pos, base + nl + 1, text))
        pos = nl + 1
    return out


def _blocks_from_lines(
    lines: list[tuple[int, int, str]], *, keep_leading_orphans: bool
) -> list[tuple[int, int, LogRecord]]:
    """Group lines into records, folding continuation lines into the previous.

    Leading continuation lines belong to a record that started before this
    window. They are dropped unless ``keep_leading_orphans`` (the window
    reaches byte 0, so there is nothing earlier they could belong to).
    """
    blocks: list[tuple[int, int, LogRecord]] = []
    extra: list[list[str]] = []

    for start, end, text in lines:
        rec = parse_log_line(text)
        if rec is None:
            if blocks:
                extra[-1].append(text)
                blocks[-1] = (blocks[-1][0], end, blocks[-1][2])
            elif keep_leading_orphans:
                orphan = LogRecord(cursor="", level="INFO", message=text)
                blocks.append((start, end, orphan))
                extra.append([])
            continue
        blocks.append((start, end, rec))
        extra.append([])

    merged: list[tuple[int, int, LogRecord]] = []
    for (start, end, rec), tail in zip(blocks, extra):
        if tail:
            rec = LogRecord(
                cursor=rec.cursor,
                timestamp=rec.timestamp,
                level=rec.level,
                logger=rec.logger,
                message="\n".join([rec.message, *tail]).rstrip(),
            )
        merged.append((start, end, rec))
    return merged


def _read_window(path: Path, lo: int, hi: int) -> bytes:
    try:
        with path.open("rb") as fh:
            fh.seek(lo)
            return fh.read(max(0, hi - lo))
    except OSError:
        return b""


def _scan_window(
    path: Path, hi: int, window_bytes: int
) -> tuple[list[tuple[int, LogRecord]], int, int]:
    """Read one backward window, returning ``(records, new_hi, bytes_read)``.

    ``new_hi`` is the *start offset of the first complete record found*, so the
    next window ends exactly on a record boundary. That is what keeps a
    multi-line traceback whole: its continuation lines always fall inside the
    window that also holds its header, never split across two.

    ``records`` is oldest-first within the window and empty when the window
    landed entirely inside one record's body — the caller grows the window and
    retries rather than looping forever.
    """
    lo = max(0, hi - window_bytes)
    buf = _read_window(path, lo, hi)
    if not buf:
        return [], 0, 0

    lines = _split_lines(buf, lo)
    if lo > 0 and lines:
        # The first line may be a fragment of one that started before `lo`.
        # It is re-read intact by the next (further back) window.
        lines = lines[1:]

    blocks = _blocks_from_lines(lines, keep_leading_orphans=(lo == 0))
    read_bytes = hi - lo

    if not blocks:
        return [], (0 if lo == 0 else hi), read_bytes

    new_hi = 0 if lo == 0 else blocks[0][0]
    return [(start, rec) for start, _end, rec in blocks], new_hi, read_bytes


def _file_cursor(path: Path, offset: int) -> str:
    return f"{path.name}:{offset}"


def _skipped_record(path: Path, offset: int, byte_count: int) -> LogRecord:
    """A synthetic record standing in for bytes the reader had to skip.

    Emitted only for a line longer than the tail's byte window. Saying so beats
    both silence (the reader looks broken) and a wedged cursor (it *is* broken).
    """
    return LogRecord(
        cursor=_file_cursor(path, offset),
        timestamp=None,
        level="WARNING",
        logger="istota.admin_logs",
        message=(
            f"[log viewer] skipped {byte_count} bytes of a single line longer "
            f"than the read window."
        ),
    )


def parse_file_cursor(chain: list[Path], cursor: str) -> tuple[int, int]:
    """Resolve ``"name:offset"`` against the chain, or raise ``ValueError``.

    The name is matched against the enumerated chain rather than joined to a
    directory, so a cursor can only ever name a file the reader already
    resolved and confined.
    """
    if not cursor or ":" not in cursor:
        raise ValueError("malformed log cursor")
    name, _, raw_offset = cursor.rpartition(":")
    try:
        offset = int(raw_offset)
    except ValueError as exc:
        raise ValueError("malformed log cursor") from exc
    if offset < 0:
        raise ValueError("malformed log cursor")
    for index, path in enumerate(chain):
        if path.name == name:
            return index, offset
    raise ValueError("unknown log file in cursor")


def read_file_page(
    chain: list[Path],
    *,
    limit: int = 200,
    before: str | None = None,
    min_level: str | None = None,
    q: str | None = None,
    logger: str | None = None,
    window_bytes: int = _DEFAULT_WINDOW_BYTES,
    max_scan_bytes: int = _DEFAULT_MAX_SCAN_BYTES,
) -> LogPage:
    """Read up to ``limit`` records ending at ``before`` (default: the tail).

    Scans backward in windows so a 200 MB log costs a few hundred KB to show
    the last screenful, and stops at ``max_scan_bytes`` so a filter matching
    nothing cannot walk the whole archive.
    """
    limit = max(1, min(limit, _MAX_LIMIT))
    if not chain:
        return LogPage()

    # One stat, used for both the page's starting position and the tail cursor
    # the caller will seed a live stream with. Two stats would let the file grow
    # between them, so the page would include records at offsets past
    # `tail_cursor` and the stream would then re-deliver those same records.
    live_size = _file_size(chain[0])
    tail_cursor = _file_cursor(chain[0], live_size)

    if before:
        file_index, hi = parse_file_cursor(chain,before)
    else:
        file_index, hi = 0, live_size

    collected: list[tuple[int, LogRecord]] = []  # newest-first
    oldest_start: tuple[int, int] | None = None
    scanned = 0
    truncated = False
    reached_start = False

    while len(collected) < limit:
        if file_index >= len(chain):
            reached_start = True
            break
        path = chain[file_index]
        if hi <= 0:
            file_index += 1
            hi = _file_size(chain[file_index]) if file_index < len(chain) else 0
            continue

        window = window_bytes
        while True:
            records, new_hi, read_bytes = _scan_window(path, hi, window)
            scanned += read_bytes
            if records or new_hi == 0 or read_bytes == 0:
                break
            if scanned >= max_scan_bytes or window >= max_scan_bytes:
                break
            window *= 2

        if not records and new_hi != 0 and scanned >= max_scan_bytes:
            truncated = True
            break

        for start, rec in reversed(records):
            if not _matches(rec, min_level=min_level, q=q, logger_prefix=logger):
                oldest_start = (file_index, start)
                continue
            stamped = LogRecord(
                cursor=_file_cursor(path, start),
                timestamp=rec.timestamp,
                level=rec.level,
                logger=rec.logger,
                message=rec.message,
            )
            collected.append((start, stamped))
            oldest_start = (file_index, start)
            if len(collected) >= limit:
                break

        hi = new_hi
        if hi == 0:
            file_index += 1
            hi = _file_size(chain[file_index]) if file_index < len(chain) else 0

        if len(collected) < limit and scanned >= max_scan_bytes:
            truncated = True
            break

    # `reached_start` is set only by the in-loop break that walks off the end of
    # the chain. It must *not* be inferred from `file_index` afterwards: filling
    # the page on the last window also advances the index, and reading that as
    # "nothing older exists" drops `next_before` and makes the next page repeat
    # the tail forever.
    next_before: str | None = None
    # A page that fills *exactly* on byte 0 of the oldest file has consumed the
    # chain without the loop noticing. Handing back that cursor would offer a
    # "Load older" that fetches an empty page.
    if oldest_start is not None and oldest_start == (len(chain) - 1, 0):
        reached_start = True
    if not reached_start and oldest_start is not None:
        next_before = _file_cursor(chain[oldest_start[0]], oldest_start[1])
    # Deliberately no `elif ... : next_before = before` fallback. Handing back
    # the caller's own cursor offers a "Load older" that re-issues an identical
    # request forever — the one reachable case is a single record larger than
    # max_scan_bytes, where there is no smaller cursor to give. `truncated` is
    # already set there, which is the honest signal.

    return LogPage(
        records=[rec for _start, rec in reversed(collected)],
        next_before=next_before,
        tail_cursor=tail_cursor,
        # Spending the budget *and* consuming the chain is not a truncated scan
        # — there was simply nothing more to find. Reporting it as truncated
        # would tell the user to widen a search that already saw everything.
        truncated=truncated and not reached_start,
    )


def read_file_tail(
    chain: list[Path],
    cursor: str,
    *,
    min_level: str | None = None,
    q: str | None = None,
    logger: str | None = None,
    max_bytes: int = _DEFAULT_TAIL_MAX_BYTES,
) -> LogTail:
    """Read records appended to the live file since ``cursor``.

    A file that has *shrunk* below the cursor was rotated out from under us —
    seeking to the old offset would put the reader past the end of a fresh file
    and it would go permanently deaf. That case re-reads from zero and reports
    ``reset`` so the client clears rather than appends.
    """
    if not chain:
        return LogTail(cursor=cursor)

    _index, offset = parse_file_cursor(chain,cursor)
    live = chain[0]
    if not cursor.startswith(live.name + ":"):
        raise ValueError("tail cursor must name the live log file")

    size = _file_size(live)
    reset = False
    if size < offset:
        offset = 0
        reset = True
        # A rotation-time backlog can be arbitrarily large; show its tail only.
        if size > max_bytes:
            offset = size - max_bytes

    if size <= offset:
        return LogTail(records=[], cursor=_file_cursor(live, offset), reset=reset)

    # Read a window, growing it until it ends on a *record* boundary rather than
    # merely a line boundary. Cutting mid-record loses the rest of that record:
    # the next poll starts past its header, so `_blocks_from_lines` discards the
    # remaining lines as leading orphans — and a multi-line traceback is exactly
    # the payload this reader exists for. Mirrors `_scan_window`'s growth in the
    # page path; without it, a filter/cap that lands inside one record either
    # truncates it silently or (with no newline at all) wedges the cursor.
    window = max_bytes
    while True:
        hi = min(size, offset + window)
        at_eof = hi >= size
        buf = _read_window(live, offset, hi)
        last_nl = buf.rfind(b"\n")

        if last_nl == -1:
            if at_eof:
                # A record still being written. Leave the bytes for the next poll.
                return LogTail(records=[], cursor=_file_cursor(live, offset), reset=reset)
            if window >= _TAIL_MAX_WINDOW_BYTES:
                # One line longer than the growth ceiling. Returning the
                # unchanged cursor would re-read the same bytes on every poll
                # forever — the tail goes silent while the UI still says "Live",
                # with no reset and no error. Consume it, and say so.
                return LogTail(
                    records=[_skipped_record(live, offset, hi - offset)],
                    cursor=_file_cursor(live, hi),
                    reset=reset,
                )
            window *= 2
            continue

        lines = _split_lines(buf[: last_nl + 1], offset)
        blocks = _blocks_from_lines(lines, keep_leading_orphans=(offset == 0))
        consumed = offset + last_nl + 1

        if at_eof or not blocks:
            # Nothing lies beyond, so the last record is whole. Holding it back
            # here would make a quiet log look dead.
            break

        head = blocks[-1][0]
        if head > offset:
            # Stop at the final record's header and leave it whole for the next
            # read, which will then see its continuation lines.
            consumed = head
            blocks = blocks[:-1]
            break

        # A single record fills the whole window; there is nowhere to back off
        # to. Grow and look again, and only emit it split once growing stops
        # being reasonable.
        if window >= _TAIL_MAX_WINDOW_BYTES:
            break
        window *= 2

    records: list[LogRecord] = []
    for start, _end, rec in blocks:
        if not _matches(rec, min_level=min_level, q=q, logger_prefix=logger):
            continue
        records.append(
            LogRecord(
                cursor=_file_cursor(live, start),
                timestamp=rec.timestamp,
                level=rec.level,
                logger=rec.logger,
                message=rec.message,
            )
        )

    return LogTail(records=records, cursor=_file_cursor(live, consumed), reset=reset)


# ---------------------------------------------------------------------------
# Task-log (DB) source
# ---------------------------------------------------------------------------

_TASK_LOG_SELECT = """
    SELECT tl.id, tl.task_id, tl.timestamp, tl.level, tl.message,
           t.user_id AS user_id, t.source_type AS source_type
    FROM task_logs tl
    LEFT JOIN tasks t ON t.id = tl.task_id
"""


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _task_log_filters(
    *,
    min_level: str | None,
    q: str | None,
    user_id: str | None,
    task_id: int | None,
) -> tuple[list[str], list]:
    clauses: list[str] = []
    params: list = []
    if min_level:
        keep = [name for name in LEVELS if level_rank(name) >= level_rank(min_level)]
        # `warn` is what the scheduler writes; match stored spellings, and keep
        # any level we do not model (level_rank ranks those above CRITICAL).
        stored = {name.lower() for name in keep}
        if "warning" in stored:
            stored.add("warn")
        if "critical" in stored:
            stored.add("fatal")
        known = {name.lower() for name in LEVELS} | {"warn", "fatal"}
        placeholders = ",".join("?" for _ in stored)
        unknown = ",".join("?" for _ in known)
        clauses.append(
            f"(LOWER(tl.level) IN ({placeholders}) OR LOWER(tl.level) NOT IN ({unknown}))"
        )
        params.extend(sorted(stored))
        params.extend(sorted(known))
    if q:
        clauses.append("tl.message LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(q)}%")
    if user_id:
        clauses.append("t.user_id = ?")
        params.append(user_id)
    if task_id is not None:
        clauses.append("tl.task_id = ?")
        params.append(task_id)
    return clauses, params


def _task_log_record(row: sqlite3.Row) -> LogRecord:
    timestamp = row["timestamp"]
    if timestamp and " " in timestamp:
        timestamp = timestamp.replace(" ", "T")
    return LogRecord(
        cursor=str(row["id"]),
        timestamp=timestamp,
        level=normalize_level(row["level"]),
        logger=None,
        message=row["message"] or "",
        task_id=row["task_id"],
        user_id=row["user_id"],
        source_type=row["source_type"],
    )


def _max_task_log_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_logs").fetchone()
    return int(row["m"] if isinstance(row, sqlite3.Row) else row[0])


def read_task_log_page(
    conn: sqlite3.Connection,
    *,
    limit: int = 200,
    before: str | None = None,
    min_level: str | None = None,
    q: str | None = None,
    user_id: str | None = None,
    task_id: int | None = None,
) -> LogPage:
    """Read up to ``limit`` task-log rows ending at ``before`` (default: newest)."""
    limit = max(1, min(limit, _MAX_LIMIT))
    clauses, params = _task_log_filters(
        min_level=min_level, q=q, user_id=user_id, task_id=task_id
    )
    if before:
        try:
            before_id = int(before)
        except ValueError as exc:
            raise ValueError("malformed log cursor") from exc
        clauses.append("tl.id < ?")
        params.append(before_id)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"{_TASK_LOG_SELECT}{where} ORDER BY tl.id DESC LIMIT ?"
    rows = conn.execute(sql, [*params, limit + 1]).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]
    records = [_task_log_record(row) for row in reversed(rows)]

    next_before = records[0].cursor if (has_more and records) else None
    return LogPage(
        records=records,
        next_before=next_before,
        tail_cursor=str(_max_task_log_id(conn)),
        truncated=False,
    )


def read_task_log_tail(
    conn: sqlite3.Connection,
    cursor: str,
    *,
    limit: int = 200,
    min_level: str | None = None,
    q: str | None = None,
    user_id: str | None = None,
    task_id: int | None = None,
) -> LogTail:
    """Read task-log rows written since ``cursor``."""
    try:
        since = int(cursor)
    except (TypeError, ValueError) as exc:
        raise ValueError("malformed log cursor") from exc
    if since < 0:
        raise ValueError("malformed log cursor")

    # Take the ceiling *before* the SELECT and bound the SELECT with it. The
    # writer is a different process (the scheduler unit) on a connection with no
    # open transaction, so reading MAX(id) afterwards would be a TOCTOU: a row
    # inserted between the two statements would be skipped past by the cursor
    # and never delivered. Bounding by a ceiling taken first makes "everything
    # up to `ceiling` was scanned" true by construction.
    ceiling = _max_task_log_id(conn)
    if ceiling <= since:
        # Nothing new. A ceiling *below* the cursor means the table was pruned
        # and rowids restarted (task_logs has no AUTOINCREMENT, and
        # `cleanup_old_tasks` deletes from it) — the cursor now points past the
        # end of a fresh sequence and would never match again, so reset.
        if ceiling < since:
            return LogTail(records=[], cursor=str(ceiling), reset=True)
        return LogTail(records=[], cursor=str(since), reset=False)

    clauses, params = _task_log_filters(
        min_level=min_level, q=q, user_id=user_id, task_id=task_id
    )
    clauses.append("tl.id > ?")
    params.append(since)
    clauses.append("tl.id <= ?")
    params.append(ceiling)

    limit = max(1, min(limit, _MAX_LIMIT))
    sql = f"{_TASK_LOG_SELECT} WHERE {' AND '.join(clauses)} ORDER BY tl.id ASC LIMIT ?"
    rows = conn.execute(sql, [*params, limit]).fetchall()
    records = [_task_log_record(row) for row in rows]

    # Advance to the max id *scanned*, not the last one emitted, or a client
    # sitting behind a run of filtered-out rows never catches up. Same rule the
    # room stream follows. A full read means the LIMIT bound the scan, so the
    # cursor must stop at the last row actually returned.
    if len(rows) >= limit:
        next_cursor = int(rows[-1]["id"])
    else:
        next_cursor = ceiling

    return LogTail(records=records, cursor=str(next_cursor), reset=False)


# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------


def list_sources(config: "Config") -> list[LogSource]:
    """Enumerate the readable log sources for this deployment."""
    chain = resolve_app_log_chain(config)
    log_cfg = config.logging

    if chain:
        app = LogSource(
            id="app",
            label="Application log",
            kind="file",
            description=(
                "Scheduler, pollers, brain and web output — the rotating file "
                "every istota process writes to."
            ),
            available=True,
            detail=f"{len(chain)} file{'s' if len(chain) != 1 else ''} in the rotation chain",
            time_basis="server-local",
            path=str(chain[0]),
            bytes=sum(_file_size(p) for p in chain),
            files=len(chain),
        )
    else:
        if log_cfg.output not in ("file", "both"):
            detail = (
                f'File logging is off ([logging] output = "{log_cfg.output}"); '
                "output goes to the console/journal only."
            )
        elif not log_cfg.file:
            detail = "No [logging] file is configured."
        else:
            detail = f"No readable log file at {log_cfg.file}."
        app = LogSource(
            id="app",
            label="Application log",
            kind="file",
            description="Scheduler, pollers, brain and web output.",
            available=False,
            detail=detail,
            time_basis="server-local",
            path=log_cfg.file or None,
        )

    tasks = LogSource(
        id="tasks",
        label="Task lifecycle",
        kind="db",
        description=(
            "Per-task lifecycle written by the scheduler — claimed, completed, "
            "retrying, failed. Includes truncated task output."
        ),
        available=True,
        detail="From the task_logs table; pruned with the task retention sweep.",
        time_basis="utc",
    )

    return [app, tasks]


def get_source(config: "Config", source_id: str) -> LogSource | None:
    """Look a source up by id. Returns ``None`` for anything not enumerated.

    The whole point: a request names a source, never a path, so traversal is
    not a class of bug that exists here.
    """
    for source in list_sources(config):
        if source.id == source_id:
            return source
    return None
