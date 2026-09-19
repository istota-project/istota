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

**Module-level imports are stdlib plus `image_sniff`; `db`, `identity` and
`storage` are imported inside the two functions that need them.** That is not
style: `baileys_protocol` imports `._types` and nothing else from the package,
deliberately, and it is the module that has to validate a name off the wire
with `is_staged_name`. A module-level `storage` import here would put the
whole graph behind that. `message_fingerprint`'s own function-scope import is
the in-tree precedent.

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

MAX_MEDIA_BYTES = 16 * 1024 * 1024
"""What one inbound file may weigh. WhatsApp's own image ceiling.

Held **below** `image_attachments.MAX_SOURCE_BYTES` (64 MiB), so nothing this
accepts is refused downstream for size — a file staged, copied into somebody's
inbox and then skipped by the pipeline is the worst shape available, since the
model answers without the image. The relation is pinned by test rather than by
an import, since a cap is not a reason for this module to pull in Pillow's.
"""

MEDIA_STAGING_CEILING_BYTES = 256 * 1024 * 1024
"""What the whole staging directory may weigh before a write is refused.

The backpressure behind the untrusted-sender case: the fetch happens before
identity is authoritatively resolved, so a stranger who knows the number can
make bytes land on disk. The per-file cap bounds one message and this bounds
the directory.
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

#: 64 characters all told, against the 34 :func:`staged_name` produces.
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

    A separate function rather than a call to that one: it also refuses a
    directory holding a foreign session and logs against the credential
    vocabulary, neither of which is true here.

    Raises rather than degrading. A staging directory that cannot be made
    private is every user's inbound photo readable by every account on the
    host, and carrying on would write into it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir(mode=0o700, exist_ok=True)
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
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
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
    """
    cleaned = "".join(
        ch for ch in str(ext or "").lower() if ch.isascii() and ch.isalnum()
    )[:8]
    return f"{message_fingerprint(message_id)}-{secrets.token_hex(8)}.{cleaned or 'bin'}"


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
        0o600,
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
    """What the staging directory occupies, du-style. Never raises."""
    return du.tree_bytes(media_dir)


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
    transaction, which is after the copy. The claimed-id half also closes the
    redelivery loop on both — Meta retries a callback it got no 200 for and
    the Baileys inbound worker has a bounded retry of its own, and without it
    each replay would re-fetch or make a second inbox copy.

    **A pre-filter and not a boundary.** The authoritative resolution and the
    authoritative claim still happen inside the transaction, where they always
    did. This one is allowed to be stale, which is why its answer is carried
    on the event as `attached_for_user` and compared there.

    `provider` is required, and the rule is `WhatsAppUserIdentity`'s own:
    which field is read is decided by the adapter the event came from, never
    by which happens to be populated. Never raises — a failure here costs the
    attachment, and the message goes on without it.
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

    Falls back to the local path when the upload fails, which is that module's
    shipped behaviour for the same situation. The re-derived suffix goes on
    the fallback too, since it is handed to the same screen.

    Catches `OSError` and `ValueError` and returns `None` rather than raising,
    because the caller is about to open `BEGIN IMMEDIATE` and a media failure
    must cost the media rather than the batch.
    """
    from ...storage import ensure_user_directories_v2, upload_file_to_inbox_v2

    staged = Path(staged)
    media_type = sniff_staged(staged)
    if media_type is None:
        logger.info(
            "whatsapp.media.rejected reason=not_a_decodable_image: staged "
            "bytes matched no signature the image pipeline can open",
        )
        _discard(staged)
        return None

    extension = image_sniff.EXTENSION_BY_MEDIA_TYPE[media_type]
    inbox_name = f"{INBOX_NAME_PREFIX}_{staged.stem}.{extension}"
    byte_count = _size(staged)
    try:
        ensure_user_directories_v2(config, user_id)
        remote_path = upload_file_to_inbox_v2(config, user_id, staged, inbox_name)
        if remote_path:
            _discard(staged)
            logger.info(
                "whatsapp.media.attached type=%s bytes=%d", media_type, byte_count,
            )
            return remote_path
        # The upload failed and the bytes are still the only copy, so they are
        # kept — renamed in place, because the fallback path is handed to the
        # same suffix screen the inbox copy would have been.
        # `os.rename`, the spelling `baileys_bridge`'s archive move uses: this
        # publishes nothing and is not a temp-file writer, so it is neither a
        # copy of `atomic_write` nor a place that wants replace semantics.
        local = staged.with_name(inbox_name)
        os.rename(staged, local)
        logger.warning(
            "whatsapp.media.inbox_upload_failed type=%s: attaching the local "
            "staged copy instead",
            media_type,
        )
        return str(local)
    except (OSError, ValueError):
        logger.warning(
            "whatsapp.media.stage_failed type=%s: the staged file could not "
            "be placed in the inbox",
            media_type,
        )
        _discard(staged)
        return None


def _size(staged: Path) -> int:
    """Byte count for a log line, best effort. Never raises."""
    try:
        return staged.stat().st_size
    except OSError:
        return -1


def _discard(staged: Path) -> None:
    """Unlink a staged file, tolerating one that is already gone."""
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
    "MAX_MEDIA_BYTES",
    "MEDIA_DIR_NAME",
    "MEDIA_ORPHAN_SECONDS",
    "MEDIA_STAGING_CEILING_BYTES",
    "STAGED_NAME_RE",
    "default_media_dir",
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
