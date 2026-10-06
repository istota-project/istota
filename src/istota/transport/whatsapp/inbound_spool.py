"""Inbound Baileys frames kept on disk until they are applied (ISSUE-669).

Baileys acknowledges an inbound message to WhatsApp before the sidecar
forwards it, and the sidecar forgets a frame once it has written it to the
daemon (ISSUE-666). From then on the bridge's in-memory queue was the only
copy, and a scheduler stop cancels the inbound worker with that queue
undrained: `AsyncRuntime._shutdown` cancels every task before the cleanup hook
that calls `BaileysBridge.stop()` runs, inside a five-second budget, while a
group turn waits on a classifier call of up to twenty seconds. So a message in
the worker's hands at a deploy was lost with nothing left to resend it.

Each frame is written here when the read loop takes it off the socket and
removed when the worker is done with it, applied or dropped for a reason that
would recur. A frame still here at the next `start()` is handed to the worker
ahead of anything the sidecar sends. Replay is safe because the apply is: an
inbound message's `processed_whatsapp` claim makes a second apply a
`duplicate`, a roster is applied as a diff against the room, and a receipt
moves its ledger row by rank.

**A file that was staged is recorded beside its frame** (`record_staged`),
from the staging thread, which runs to completion even when the task awaiting
it is cancelled. Staging moves the file into the user's inbox and unlinks the
staged copy, so a replay that staged again would find nothing and answer
"media failed" for a photo already sitting in the inbox; it reuses the record
instead.

**A draining stop was the other way to do this and is not enough on its own**:
it covers only a stop that runs to completion within its budget, and a
`SIGKILL`, an OOM or a hung classifier leave the queue in memory either way.

One file per frame, `O_EXCL | O_NOFOLLOW`, 0600, in `{db_path.parent}/
whatsapp-inbound`, which the sandbox masks with the rest of that directory. A
frame carries the message text, so the directory is private like the media
staging directory, and a file lives only as long as the frame is unapplied, or
`SPOOL_MAX_AGE_SECONDS` if no bridge starts to apply it. Written without
`fsync`: the loss this closes is a process restart, which the page cache
survives, and an `fsync` per message on the event loop is a cost for a host
crash this does not claim to cover.

Never raises: every function here returns a value saying what happened, and
the bridge treats a spool it cannot write as the in-memory queue it had before.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...config import Config

logger = logging.getLogger(__name__)

SPOOL_DIR_NAME = "whatsapp-inbound"
SPOOL_FILE_MODE = 0o600
SPOOL_SUFFIX = ".json"
_TEMP_SUFFIX = ".tmp"

#: How many frames may wait here. Past it a frame is carried in memory only,
#: as every frame was before, rather than refused: the spool narrows a loss, it
#: never causes one. Four times the in-memory queue, so a full queue is never
#: the reason a spool write is refused. Counted by the bridge, not by listing
#: the directory, because the write runs on the event loop for every frame.
SPOOL_MAX_ENTRIES = 4096

#: A kept frame older than this is dropped at load rather than applied. Longer
#: than any restart or deploy, and shorter than the point where answering a
#: message, or reading a bare "yes" as the answer to whatever is parked now,
#: stops making sense. The sidecar's own hold is four minutes.
SPOOL_MAX_AGE_SECONDS = 3600

_FORMAT_VERSION = 1
#: Names sort by this process's start, then by a counter, so arrival order
#: within a run does not depend on a wall clock that can step backwards.
_PROCESS_STAMP = time.time_ns()
_sequence = itertools.count()


@dataclass(frozen=True)
class SpooledFrame:
    path: Path
    message_type: str
    payload: dict[str, Any]
    #: `{"media": <WhatsAppInboundMedia fields or None>, "claimed_from": ...}`
    #: once the frame's file was staged, else None.
    staged: dict[str, Any] | None = None


def default_spool_dir(config: "Config") -> Path:
    """`{db_path.parent}/whatsapp-inbound`, beside the media staging directory."""
    return Path(config.db_path).parent / SPOOL_DIR_NAME


def _write_new(path: Path, body: bytes) -> None:
    fd = os.open(
        path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
        SPOOL_FILE_MODE,
    )
    try:
        view = memoryview(body)
        while view:
            view = view[os.write(fd, view):]
    except OSError:
        os.close(fd)
        remove(path)
        raise
    os.close(fd)


def _encode(message_type: str, payload: dict, staged: dict | None) -> bytes:
    record: dict[str, Any] = {
        "v": _FORMAT_VERSION, "type": message_type, "payload": payload,
    }
    if staged is not None:
        record["staged"] = staged
    return json.dumps(record, separators=(",", ":")).encode("utf-8")


def write(spool_dir: Path, message_type: str, payload: dict) -> Path | None:
    """Keep one frame until `remove` is called for it, or say it could not.

    `None` means the frame is in memory only, and the reason is logged.
    """
    name = f"{_PROCESS_STAMP:020d}-{next(_sequence):010d}{SPOOL_SUFFIX}"
    path = Path(spool_dir) / name
    try:
        _write_new(path, _encode(message_type, payload, None))
    except (OSError, ValueError, TypeError):
        logger.warning(
            "whatsapp.baileys.spool_write_failed dir=%s: this frame is held in "
            "memory only", spool_dir, exc_info=True,
        )
        return None
    return path


def record_staged(
    path: Path | None, message_type: str, payload: dict,
    media: dict | None, claimed_from: str | None,
) -> bool:
    """Replace a kept frame with one that carries its staging result.

    Written to a temporary name and renamed over the original, so a stop
    between the two leaves either whole record and never half of one.
    """
    if path is None:
        return False
    path = Path(path)
    temp = path.with_name(path.stem + _TEMP_SUFFIX)
    try:
        _write_new(temp, _encode(
            message_type, payload,
            {"media": media, "claimed_from": claimed_from},
        ))
        os.replace(temp, path)
    except (OSError, ValueError, TypeError):
        remove(temp)
        logger.warning(
            "whatsapp.baileys.spool_record_failed path=%s: a replay of this "
            "frame stages its file again", path, exc_info=True,
        )
        return False
    return True


def remove(path: Path | None) -> bool:
    """Forget a frame; whether a file was removed. Never raises."""
    if path is None:
        return False
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning(
            "whatsapp.baileys.spool_remove_failed path=%s: the frame will be "
            "replayed at the next start, where its claim makes it a duplicate",
            path, exc_info=True,
        )
        return False
    return True


def _read(path: Path) -> SpooledFrame | None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        record = json.loads(handle.read().decode("utf-8"))
    if not isinstance(record, dict):
        return None
    message_type = record.get("type")
    payload = record.get("payload")
    staged = record.get("staged")
    if not isinstance(message_type, str) or not isinstance(payload, dict):
        return None
    if staged is not None and not (
        isinstance(staged, dict)
        and (staged.get("media") is None or isinstance(staged.get("media"), dict))
    ):
        return None
    return SpooledFrame(path, message_type, payload, staged)


def pending(spool_dir: Path, *, now: float | None = None) -> list[SpooledFrame]:
    """Every frame left unapplied, oldest first.

    An entry that cannot be read as a frame is removed and counted: it can
    never be applied, and left in place it would be read again at every
    start. One past `SPOOL_MAX_AGE_SECONDS` is removed and counted apart. A
    temporary file is a `record_staged` a stop interrupted; its original is
    still whole, so the temporary is removed.
    """
    spool_dir = Path(spool_dir)
    cutoff = (time.time() if now is None else now) - SPOOL_MAX_AGE_SECONDS
    try:
        names = sorted(os.listdir(spool_dir))
    except (OSError, ValueError):
        return []
    frames: list[SpooledFrame] = []
    damaged = 0
    expired = 0
    for name in names:
        path = spool_dir / name
        if name.endswith(_TEMP_SUFFIX):
            remove(path)
            continue
        if not name.endswith(SPOOL_SUFFIX):
            continue
        try:
            if os.lstat(path).st_mtime < cutoff:
                expired += 1
                remove(path)
                continue
            frame = _read(path)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, UnicodeDecodeError):
            frame = None
        if frame is None:
            damaged += 1
            remove(path)
            continue
        frames.append(frame)
    if damaged:
        logger.warning(
            "whatsapp.baileys.spool_damaged count=%d dir=%s: kept frames that "
            "could not be read were removed, and the messages in them are lost",
            damaged, spool_dir,
        )
    if expired:
        logger.warning(
            "whatsapp.baileys.spool_expired count=%d dir=%s: kept frames older "
            "than %ds were removed unapplied",
            expired, spool_dir, SPOOL_MAX_AGE_SECONDS,
        )
    return frames
