"""Inbound WhatsApp media, between the wire and the attachment pipeline.

The whole surface funnels into `webhook.handle_whatsapp_batch`, which holds
the write lock across the dedup claim, identity resolution, the binding latch
and task creation. A media fetch is a network round trip, and one under
`BEGIN IMMEDIATE` is the hazard `.claude/rules/notifications.md` names: a
second connection waits out its 30s busy timeout against the lock the caller
holds, and under `istota serve` that router is on the web app's event loop, so
it stalls the receiver and the web UI together.

So **the bytes land on disk before the transaction opens, and the transaction
sees only a path**. That falls out per adapter — the sidecar downloads on the
Baileys path because it already holds the decryption keys, and the daemon
downloads in the route on the Cloud path because Meta's media endpoint needs
the access token — and everything from the pre-check onwards is this one
module, called by both in the same order.

**Who owns which bound.** The per-file byte cap is the fetcher's, because the
daemon only ever sees a file that already exists: on Baileys the sidecar
aborts past the cap and sends a marker rather than a path, and on Cloud the
daemon gates on Meta's declared `file_size` and again during the stream. The
staging ceiling and the orphan sweep are the daemon's, because only the daemon
knows the whole directory and which files were consumed.

`db`, `identity` and `storage` are imported inside the two functions that
need them, which keeps the import-time surface to `du`, `image_sniff` and the
package — **and is not a leaf boundary, which an earlier version of this
paragraph claimed it was.** That claim was that a module-level `storage`
import would put the whole graph behind `baileys_protocol`, which has to
validate a name off the wire with `is_staged_name`. Measured, it is false in
both directions: `from . import message_fingerprint` reaches
`transport/whatsapp/__init__.py`, which imports `transport._types`, which
already pulls `db`, `storage` and `config` — and `baileys_protocol` pulls the
same graph through the same package `__init__` on its own. Nothing here is
load-bearing and nothing pins it; a real boundary would mean a stdlib leaf
plus a transitive-import guard, the way `tests/native/test_session_log.py`
pins one.

Nothing here raises into `handle_whatsapp_batch`: the batch's contract is that
an exception rolls back to a 503 the provider retries against, and a media
failure must cost the media, never the batch. Nothing here logs a filename off
the wire, a caption, a media URL or a provider error body — a log line carries
the message fingerprint, a byte count, the sniffed type and a fixed reason.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ... import du, image_sniff
from . import message_fingerprint

if TYPE_CHECKING:  # pragma: no cover - typing only
    import sqlite3

    from ...config import Config
    from ._types import WhatsAppUserIdentity

logger = logging.getLogger("istota.transport.whatsapp.media")


MEDIA_DIR_NAME = "whatsapp-media"

MEDIA_DIR_MODE = 0o700
"""What the staging directory is held to, named so a reader can ask.

`ensure_media_dir` narrows to it and `doctor.whatsapp.media_staging` reports
against it, which is the whole reason it is a constant: a diagnostic carrying
its own copy of a mode could pass while the thing that sets it disagreed.
"""

MEDIA_FILE_MODE = 0o600
"""What one staged file is created at, named beside the directory's mode so the
pair reads as one rule.

Read by `open_staged_write` alone. `doctor.whatsapp.media_staging` deliberately
has no per-file mode arm: it reports the directory, which is what decides
whether another account can reach a staged photograph at all.
"""

MAX_MEDIA_BYTES = 16 * 1024 * 1024
"""What one inbound file may weigh. WhatsApp's own image ceiling.

Held **below** `image_attachments.MAX_SOURCE_BYTES` (64 MiB), so nothing this
accepts is refused downstream for size — a file staged, copied into somebody's
inbox and then skipped by the pipeline is the worst shape available, since the
model answers without the image. The relation is pinned by test rather than by
an import, since a cap is not a reason for this module to pull in Pillow's.
"""

MEDIA_STAGING_CEILING_BYTES = 256 * 1024 * 1024
"""What the whole staging directory may occupy before a write is refused.

The backpressure behind the untrusted-sender case: the fetch happens before
identity is authoritatively resolved, so a stranger who knows the number can
make bytes land on disk. The per-file cap bounds one message and this bounds
the directory.

Compared against `staging_bytes`, which measures blocks rather than apparent
size, so on a filesystem of 4 KiB blocks a directory of small files reaches
this figure sooner than their sizes would suggest. The error is toward
refusing a fetch, and `has_staging_room` is a check before a write rather than
a reservation — two processes can each pass it and jointly pass the ceiling,
which is what a check-then-write costs and is bounded by the per-file cap.
"""

MEDIA_ORPHAN_SECONDS = 600
"""How long a staged file may sit before the sweep treats it as abandoned.

A file is unlinked as soon as it has been copied into the inbox, so the steady
state is an empty directory and this window is for the orphan: a task creation
that raised, a message dropped after the fetch, a sidecar that wrote a file for
a frame that never arrived, a daemon killed between the write and the copy.
"""

INBOX_NAME_PREFIX = "whatsapp"
"""What an inbox copy is called, so a user can see where it came from."""

MEDIA_UNATTRIBUTED = "the image was not attributed to a user"
"""What a staged file the pre-check would not name a user for becomes.

Both adapters reach it and each reaches it for its own reasons: an unknown
sender, a message id already claimed, a sender who has opted out, or a read
that could not be answered. It is a `WhatsAppInboundMedia.error` rather than a
dropped record, because dropping one would send an image *with a caption* into
the `unsupported_type` gate — and a caption is a message, so `START` typed on a
photograph has to keep working for exactly the opted-out sender this reason is
most often about.
"""

MEDIA_NOT_PLACED = "the image could not be placed in the user's workspace"
"""What a decodable image nothing could copy into an inbox becomes.

Distinct from a file the sniff refused, which is not an image at all and takes
the `unsupported_type` reply the surface already had. This one is istota's own
failure and says so.
"""

MEDIA_FETCH_FAILED = "the image could not be downloaded from WhatsApp"
MEDIA_OVER_CAP = "the image was larger than this surface accepts"
MEDIA_WRITE_FAILED = "the image could not be written to disk"
"""The three ways a *fetch* ends badly, in one place because both adapters
reach them.

The sidecar names them as keys (`download_failed`, `over_the_cap`,
`write_failed`, since its own words carry the destination JID and, on a Boom
error, the whole request) and `baileys_protocol._MEDIA_ERRORS` maps each key
onto the sentence here; `client.fetch_media` raises them directly, because on
that adapter the daemon is the fetcher and there is no wire to cross. This
module is the authoritative copy of the prose and the only one — a second
spelling drifts, and what a user is told is the thing that would drift.

Neither adapter may build a reason from an exception: Meta's prose, PyWa's
exception text and an httpx repr all carry the request URL, which carries the
recipient and the access token's path segment.
"""

MAX_DECLARED_MIME_CHARS = 128
"""How much of a declared media type is kept, and there is a second half.

The value is what the *sender* said their file was, echoed back by the adapter,
so it reaches a log line and must not be able to forge one — the length bound
is half the rule and `bounded_media_type` is the other. Nothing branches on the
value either way: `stage_to_attachment` sniffs the bytes and names the inbox
copy from its own answer.
"""


def bounded_media_type(value: object) -> str:
    """A declared media type fit to log: printable, bounded, never trusted.

    Both *daemon-side* producers take this one — `webhook._pending_media` off
    Meta's callback and `client.fetch_media` off Meta's media-url answer — so a
    newline or an ANSI escape cannot forge a log line from either.

    `baileys_protocol` deliberately does **not**: it *refuses* a frame whose
    `media_mime` is not printable rather than repairing it, because a value
    arriving over the sidecar socket that fails a bounds test is evidence about
    the frame rather than a string to tidy, and that module's rule is to drop
    what it cannot read. Here there is no frame to refuse — the value came back
    from a call this process made — so the useful answer is a bounded string.

    Takes `object`, because both callers read it out of somebody else's JSON.
    """
    text = value if isinstance(value, str) else ""
    return "".join(ch for ch in text if ch.isprintable())[:MAX_DECLARED_MIME_CHARS]


NO_MESSAGE_ID = "nomessageid"
"""The fingerprint half of a staged name for a message with no id."""

#: 64 characters all told, against the 33 :func:`staged_name` produces.
STAGED_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
"""The charset and length a staged name is held to, beside the component test.

Deliberately **not** a match of what :func:`staged_name` produces. The sidecar
mints the name on the Baileys path and cannot compute `message_fingerprint` —
the salt is the daemon's — so a validator spelled as that format would refuse
every real frame. What it has to guarantee is that the value is one ordinary
component this module is willing to join under the staging root.
"""


def default_media_dir(config: "Config") -> Path:
    """`{db_path.parent}/whatsapp-media`, and no operator override.

    `baileys_bridge.default_session_dir`'s location with its configured-path
    arm deliberately absent: the directory is a property of the surface rather
    than of one adapter, so an override would be `[whatsapp] media_dir` rather
    than a Baileys key, and none ships.

    Three independent things make this the location. The Ansible unit sets
    `ReadWritePaths={istota_home}/data` with `PrivateTmp=true`, so the sidecar
    can write here and cannot write under `temp_dir` — and widening the unit
    would hand it every user's task temp directory. The compose stack already
    mounts `istota_data:/data` into both containers, so they see one inode set
    with no new volume. And `build_bwrap_cmd` masks `db_path.parent` with an
    empty read-only tmpfs after every other mount, so on both shapes this
    directory is bound into no sandbox at any path.

    The standalone residual is stated rather than hidden: `setup_wizard` puts
    `db_path`, `workspace_path` and `temp_dir` in one directory, where
    `_mask_dir` refuses outright, so there this sits inside `workspace_path`
    and a `user_resources` row naming the workspace root reaches it. It is the
    residual `baileys_bridge.default_pairing_relay_path` already carries, and
    milder: a staged file is one user's own photo, lives for the few hundred
    milliseconds between the write and the inbox copy, and carries no
    credential.
    """
    return Path(config.db_path).parent / MEDIA_DIR_NAME


def ensure_media_dir(path: Path) -> Path:
    """Create or tighten the staging directory, and return it.

    `baileys_bridge.ensure_session_dir`'s rule, for its reason: the mode
    passed to `mkdir` applies **only to a directory the call creates**, so it
    is asserted on an `O_NOFOLLOW | O_DIRECTORY` descriptor every time, and a
    directory already at 0700 skips the `fchmod` and with it the `EPERM` that
    would otherwise be the only sign another uid owns it.

    **A deliberate second copy, and `ensure_session_dir` is the authoritative
    one.** The spec's reason for the split — that the original also refuses a
    directory holding a foreign session and logs against the credential
    vocabulary — is not true of it: that function is this one line for line
    apart from two error strings. What holds the two in step is
    `tests/test_whatsapp_media.py::TestTheTwoPrivateDirectoryGuardsAgree`,
    which drives both over the same four cases, rather than a comment asking
    the next reader to remember. Collapsing them into one implementation is
    the better answer and is left for whoever next has reason to touch both:
    it inverts the import direction between this module and a 180 KB one, for
    a gain this stage does not need.

    Raises rather than degrading. A staging directory that cannot be made
    private is every user's inbound photo readable by every account on the
    host, and carrying on would write into it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir(mode=MEDIA_DIR_MODE, exist_ok=True)
    except FileExistsError:
        # `exist_ok=True` still raises this for a non-directory at the name.
        raise NotADirectoryError(
            f"whatsapp media staging path is not a directory: {path}"
        ) from None
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid():
            raise PermissionError(
                f"whatsapp media staging directory belongs to uid "
                f"{info.st_uid}, not to this process: {path}"
            )
        if stat.S_IMODE(info.st_mode) != MEDIA_DIR_MODE:
            os.fchmod(fd, MEDIA_DIR_MODE)
    finally:
        os.close(fd)
    return path


def staged_name(message_id: str | None, ext: object) -> str:
    """`<message fingerprint>-<random hex>.<ext>`.

    The fingerprint is `message_fingerprint`'s, whose salt domain is shared
    across this module so a staged file, the log line about it and the
    delivery row all name the same message. The random half keeps two media
    parts of one message apart.

    **The extension is advisory.** On the Cloud path the daemon picks it from
    its own sniff and on the Baileys path the sidecar picks it from a declared
    mimetype nothing trusts — and neither reaches `task.attachments`, because
    `stage_to_attachment` re-derives the suffix from its own sniff when it
    names the inbox copy. What the staged name is load-bearing for is
    uniqueness and containment, not type.

    **A message id it cannot fingerprint gets a fixed token instead.**
    `short_fingerprint` answers `""` for a falsy value, and an empty leading
    half produces a name starting with `-` — which `is_staged_name` refuses,
    so this function would mint a name `open_staged_write` then raises on. The
    signature admits `None`, so that is a contract this module owes rather
    than a caller's mistake; the random half still keeps such names apart.
    """
    cleaned = "".join(
        ch for ch in str(ext or "").lower() if ch.isascii() and ch.isalnum()
    )[:8]
    fingerprint = message_fingerprint(message_id) or NO_MESSAGE_ID
    return f"{fingerprint}-{secrets.token_hex(8)}.{cleaned or 'bin'}"


def is_staged_name(value: object) -> bool:
    """Whether *value* may be joined under the staging root.

    `session_log_read.find_logs`' single-path-component rule, applied to a
    string a sidecar chose, plus a charset and a length bound. The empty
    string mattering is the point there and it is the point here: `PurePath`
    discards an empty component, so `media_dir / ""` is the staging root
    itself, an absolute component replaces the root outright, and `..` is a
    child by name and the parent on disk.

    Takes `object` because the caller is a JSON decoder reading a frame off a
    socket, so the type is whatever was on the line.
    """
    if not isinstance(value, str) or not value or value in (".", ".."):
        return False
    if "\x00" in value:
        return False
    if os.sep in value or (os.altsep and os.altsep in value):
        return False
    if value != os.path.basename(value):
        return False
    return STAGED_NAME_RE.fullmatch(value) is not None


def open_staged_write(media_dir: Path, name: str) -> int:
    """A descriptor to write one staged file, 0600. The caller closes it.

    `O_EXCL` so the name is claimed rather than written through, and
    `O_NOFOLLOW` so a symlink planted at it is refused rather than followed —
    either alone covers the symlink, and both are cheap.

    Raises `ValueError` for a name that is not one ordinary component, before
    anything touches the filesystem: refusing at the join is the whole of the
    containment story, and a caller that has not validated must not be given a
    descriptor to somewhere else.
    """
    if not is_staged_name(name):
        raise ValueError("staged media name is not a single ordinary component")
    return os.open(
        Path(media_dir) / name,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
        MEDIA_FILE_MODE,
    )


def sniff_staged(path: Path) -> str | None:
    """What the staged bytes actually are, or None.

    `image_sniff.sniff_decodable` over the first `SNIFF_BYTES`, and **no
    signature table of its own** — a second sniffer is exactly the duplication
    that module exists to prevent, and a test asserts the absence.

    `None` means delete it and treat the message as unsupported: it is the
    SVG-named-`.png` case, and the MP4-with-an-`ftyp`-box case, and every
    other file a declared mimetype would have got wrong.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        head = os.read(fd, image_sniff.SNIFF_BYTES)
    except OSError:
        return None
    finally:
        os.close(fd)
    return image_sniff.sniff_decodable(head)


def staging_bytes(media_dir: Path) -> int:
    """What the staging directory occupies, du-style. Never raises.

    **Measures exactly what the sweep can reclaim**, which is why it is a flat
    scan of regular files rather than `du.tree_bytes`' recursive walk: the two
    have to share a domain or the difference is permanent ceiling debt. Under
    a recursive measurement, anything the sweep skips — a subdirectory's
    contents, a fifo, a socket — counts toward the ceiling and is unlinked by
    nothing, so a large enough one refuses every later fetch on the surface
    with no way to clear it. Nothing in this module can create such an entry
    (`open_staged_write` refuses a name with a separator in it), so this is a
    domain agreeing with itself rather than a reachable defect.

    Blocks rather than apparent size, `du.entry_bytes`' arithmetic and its
    reason: a volume is filled by blocks. So a directory of small files
    measures above the sum of their sizes while the incoming figure the
    ceiling is compared against is nominal — a conservative mismatch, and the
    ceiling's own docstring says which unit it is in.
    """
    total = 0
    try:
        entries = sorted(Path(media_dir).iterdir())
    except (OSError, ValueError):
        return 0
    for entry in entries:
        try:
            info = entry.lstat()
            if stat.S_ISREG(info.st_mode):
                total += du.entry_bytes(info)
        except (OSError, ValueError):
            continue
    return total


def prune_media_dir(media_dir: Path, *, now: float | None = None) -> int:
    """Unlink every staged file past the orphan window; say how many.

    `outbound._prune_parked_statuses`' arrangement: pruned whenever the
    directory is touched — before a write and after a consume — rather than on
    a scheduler gate of its own, because a gate is a new interval, a new
    config key and a new thing to be wrong about for a directory whose steady
    state is empty. `cleanup_old_temp_files` is deliberately not reused: it
    walks `temp_dir`, which this is not under, and its window is days against
    files that should live for milliseconds.

    The count is logged at warning, because a staged file istota fetched and
    could not place is a message somebody sent that nobody answered.

    **Age is the only rule, and the ceiling never deletes a young file.** A
    file inside the window may be mid-consume for another message, so a sweep
    that freed space by taking one would be racing that consume; an over-full
    directory refuses the incoming write instead, which is the writer's call.

    **Runs in two processes on the split Ansible shape** — the receiver prunes
    on the Cloud path and the scheduler on the Baileys one — so every unlink
    is `ENOENT`-tolerant and this never raises: two sweeps racing the same
    orphan is the ordinary case, not a fault.
    """
    cutoff = (time.time() if now is None else now) - MEDIA_ORPHAN_SECONDS
    dropped = 0
    try:
        entries = sorted(Path(media_dir).iterdir())
    except (OSError, ValueError):
        return 0
    for entry in entries:
        try:
            info = entry.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff:
                continue
            entry.unlink()
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            logger.warning(
                "whatsapp.media.prune_failed: a staged file could not be "
                "removed from %s", media_dir,
            )
            continue
        dropped += 1
    if dropped > 0:
        logger.warning(
            "whatsapp.media.orphans_swept count=%d dir=%s: staged files "
            "nothing consumed, which is a message somebody sent that nobody "
            "answered",
            dropped, media_dir,
        )
    return dropped


def has_staging_room(
    media_dir: Path, *, incoming_bytes: int, now: float | None = None
) -> bool:
    """Prune first, then say whether a write of this size fits.

    The spec's "prunes first and then refuses if it is still over", in one
    place rather than at each fetcher — the two callers must not disagree
    about which half runs first, since pruning after the measurement is a
    directory that never recovers.
    """
    prune_media_dir(media_dir, now=now)
    used = staging_bytes(media_dir)
    fits = used + max(0, int(incoming_bytes)) <= MEDIA_STAGING_CEILING_BYTES
    if not fits:
        logger.warning(
            "whatsapp.media.staging_full used=%d incoming=%d ceiling=%d: "
            "refusing the fetch",
            used, incoming_bytes, MEDIA_STAGING_CEILING_BYTES,
        )
    return fits


def precheck(
    conn_factory: "Callable[[], sqlite3.Connection]",
    *,
    identity: "WhatsAppUserIdentity",
    message_id: str,
    provider: str,
) -> str | None:
    """Who this file is probably for, asked without taking the write lock.

    Two questions, both cheap and both read-only: does this sender's
    adapter-native identity resolve to a user, and has this `message_id`
    already been claimed. `None` to either means the media goes no further —
    nothing is fetched on the Cloud path and the already-staged file is
    unlinked on the Baileys one.

    It is common to both adapters rather than Cloud's, and that is
    load-bearing: `stage_to_attachment` copies into *a user's* inbox, so it
    needs a `user_id`, and the authoritative one does not exist until the
    transaction, which is after the copy.

    The claimed-id half closes the redelivery loop for a **delivered** message
    whose acknowledgement was lost — Meta retries a callback it got no 200
    for, and the Baileys inbound worker has a bounded retry of its own — where
    without it each replay would re-fetch or make a second inbox copy. It does
    not close the rollback case, and cannot: a batch that raises rolls its
    claim back with everything else, so the retry finds nothing written and is
    a genuinely new message as far as this read can tell.

    **A pre-filter and not a boundary.** The authoritative resolution and the
    authoritative claim still happen inside the transaction, where they always
    did. This one is allowed to be stale, which is why its answer is carried
    on the event as `attached_for_user` and compared there.

    `provider` is required, and the rule is `WhatsAppUserIdentity`'s own:
    which field is read is decided by the adapter the event came from, never
    by which happens to be populated.

    **It takes ownership of the connection the factory returns**: the row
    factory is set on it, because the binding lookups index rows by column
    name, and it is closed on every path. A caller handing over a connection
    it means to keep would lose it on the first call. The factory is the
    caller's choice of connector rather than this module's — the spec names
    `sqlite_util.connect_read_only`, whose read-write branch can checkpoint a
    WAL on last close, so a caller putting this on a per-message path should
    know that is what it picked.

    Never raises — a failure here costs the attachment, and the message goes
    on without it.
    """
    import sqlite3

    from . import identity as identity_rules

    if not message_id or len(message_id) > 255:
        return None
    try:
        conn = conn_factory()
    except Exception:  # noqa: BLE001 - never raises; the caller loses the media
        logger.warning("whatsapp.media.precheck_unavailable: no read connection")
        return None
    try:
        # `sqlite_util.connect_read_only` leaves the row factory alone, and
        # the binding lookups this delegates to index rows by column name.
        # Set here rather than asked of the caller, because the requirement
        # belongs to the queries rather than to the connection.
        conn.row_factory = sqlite3.Row
        claimed = conn.execute(
            "SELECT 1 FROM processed_whatsapp WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if claimed is not None:
            return None
        return identity_rules.resolve_for_precheck(
            conn, identity, provider=provider,
        )
    except Exception:  # noqa: BLE001 - same contract
        logger.warning(
            "whatsapp.media.precheck_failed message=%s: the read-only "
            "pre-check could not be answered",
            message_fingerprint(message_id),
        )
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - nothing useful to do with it
            pass


def stage_to_attachment(
    config: "Config", user_id: str, staged: Path
) -> str | None:
    """Sniff a staged file, copy it into the user's inbox, unlink the staged one.

    Returns the path that goes on `task.attachments`, or `None` for a file
    that is not a decodable image or could not be placed — in which case the
    staged file is gone and the message takes the media-failed path.

    **The inbox copy is named from this function's own sniff, always**, and
    never from the staged file's suffix or from a declared mimetype.
    `prepare_image_attachments` screens by `Path(candidate).suffix`, so a
    correct HEIC staged as `.bin` would be skipped in silence and the model
    would answer without the image and without knowing one was sent. This is a
    deliberate departure from `transport/email/inbound.py`'s
    `{prefix}_{local_path.name}`, which keeps the sender's name because an
    email attachment's name is the one the user recognises: here the daemon
    named the file in the first place and there is no user-chosen name to
    preserve.

    Falls back to a local path when the upload fails, which is
    `transport/email/inbound.py`'s shipped behaviour for the same situation —
    and **the fallback leaves the staging directory**, which is the half that
    precedent turns on. Email's local copy lands under `temp_dir`, swept by
    `cleanup_old_temp_files` on a `retention_days` window; a fallback left
    where it was staged would instead be deleted by this module's own sweep
    600 seconds later, and the spec rejects naming a swept path in
    `task.attachments` for exactly that reason: a task can sit well past ten
    minutes behind queue pressure, the retry ladder or a parked confirmation.
    The staging directory is also inside the tmpfs `build_bwrap_cmd` masks, so
    a path there is unreadable from inside the task's own sandbox. It goes to
    `{temp_dir}/whatsapp-media/` instead, and the re-derived suffix goes with
    it, since it is handed to the same screen.

    **The staged file is refused above `MAX_MEDIA_BYTES`.** The per-file cap
    belongs to the fetcher, which is the sidecar on one path and the daemon on
    the other, and this is the one funnel both reach — an oversized file that
    got past a fetcher would otherwise be copied into somebody's inbox and
    then skipped by `image_attachments` at its own source cap, which is the
    silent shape the suffix rule above exists to avoid.

    Catches `OSError` and `ValueError` and returns `None` rather than raising,
    because the caller is about to open `BEGIN IMMEDIATE` and a media failure
    must cost the media rather than the batch.
    """
    from ...storage import ensure_user_directories_v2, upload_file_to_inbox_v2

    staged = Path(staged)
    if not is_staged_name(staged.name):
        # The stem is interpolated into a filename below, so the name is held
        # to the same rule at the consume as at the write — the two callers
        # are different modules and only one of them opened the file here.
        logger.warning(
            "whatsapp.media.rejected reason=not_a_staged_name: the consume "
            "step was handed a path this module did not name",
        )
        return None
    media_type = sniff_staged(staged)
    if media_type is None:
        logger.info(
            "whatsapp.media.rejected reason=not_a_decodable_image file=%s: "
            "staged bytes matched no signature the image pipeline can open",
            staged.stem,
        )
        discard_staged(staged)
        return None

    byte_count = _size(staged)
    if byte_count > MAX_MEDIA_BYTES:
        logger.warning(
            "whatsapp.media.rejected reason=over_the_file_cap file=%s "
            "bytes=%d cap=%d",
            staged.stem, byte_count, MAX_MEDIA_BYTES,
        )
        discard_staged(staged)
        return None

    extension = image_sniff.EXTENSION_BY_MEDIA_TYPE[media_type]
    inbox_name = f"{INBOX_NAME_PREFIX}_{staged.stem}.{extension}"
    try:
        ensure_user_directories_v2(config, user_id)
        remote_path = upload_file_to_inbox_v2(config, user_id, staged, inbox_name)
        if remote_path:
            discard_staged(staged)
            logger.info(
                "whatsapp.media.attached type=%s bytes=%d file=%s",
                media_type, byte_count, staged.stem,
            )
            return remote_path
        # The upload failed and these bytes are the only copy, so they are
        # kept — moved out of the sweep's reach rather than renamed in place.
        # `shutil.move` rather than `os.rename` because the two directories
        # need not share a filesystem; it is not a temp-file writer and so is
        # not a second copy of `atomic_write`.
        fallback_dir = ensure_media_dir(Path(config.temp_dir) / MEDIA_DIR_NAME)
        local = fallback_dir / inbox_name
        shutil.move(str(staged), str(local))
        logger.warning(
            "whatsapp.media.inbox_upload_failed type=%s file=%s: attaching a "
            "local copy outside the staging directory instead",
            media_type, staged.stem,
        )
        return str(local)
    except (OSError, ValueError):
        logger.warning(
            "whatsapp.media.stage_failed type=%s file=%s: the staged file "
            "could not be placed in the inbox",
            media_type, staged.stem,
        )
        discard_staged(staged)
        return None


def _size(staged: Path) -> int:
    """Byte count for a log line, best effort. Never raises."""
    try:
        return staged.stat().st_size
    except OSError:
        return -1


def discard_staged(staged: Path) -> None:
    """Unlink a staged file, tolerating one that is already gone.

    Public because the file this module unlinks on its own refusals is the
    same file a *caller* has to unlink on theirs — a pre-check that named
    nobody, a message the transaction dropped — and a second `unlink` written
    at each adapter would have to relearn the never-raises rule and the
    already-gone case. The sweep is the backstop for both, not the mechanism:
    every staged file istota consumes **and** every one it drops is unlinked
    when it is decided, which is what bounds the Baileys path in the absence
    of a staging ceiling the daemon can enforce there.
    """
    try:
        staged.unlink()
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        logger.warning(
            "whatsapp.media.discard_failed: a staged file could not be "
            "removed; the sweep will take it",
        )


__all__ = [
    "INBOX_NAME_PREFIX",
    "MAX_DECLARED_MIME_CHARS",
    "MAX_MEDIA_BYTES",
    "MEDIA_DIR_MODE",
    "MEDIA_DIR_NAME",
    "MEDIA_FILE_MODE",
    "MEDIA_FETCH_FAILED",
    "MEDIA_NOT_PLACED",
    "MEDIA_OVER_CAP",
    "MEDIA_WRITE_FAILED",
    "MEDIA_ORPHAN_SECONDS",
    "MEDIA_STAGING_CEILING_BYTES",
    "MEDIA_UNATTRIBUTED",
    "NO_MESSAGE_ID",
    "STAGED_NAME_RE",
    "bounded_media_type",
    "default_media_dir",
    "discard_staged",
    "ensure_media_dir",
    "has_staging_room",
    "is_staged_name",
    "open_staged_write",
    "precheck",
    "prune_media_dir",
    "sniff_staged",
    "stage_to_attachment",
    "staged_name",
    "staging_bytes",
]
