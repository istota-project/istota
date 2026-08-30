"""Profile pictures: normalization, and the two tables that hold the bytes.

One store for every identity the web UI renders. `user_avatars` holds one row
per (user, source) — the row set *is* the precedence chain, so removing an
upload reveals an imported Nextcloud picture rather than leaving nothing behind
— and `bot_avatar` holds the deployment's one bot icon.

Two things about the shape are worth knowing before changing anything here.

**The bytes are in the framework DB, not in the workspace.** Health's
`{uploads_dir}/{panel_id}/original.{ext}` is the precedent for user *documents*,
which are large, kept verbatim and served rarely; an avatar is about 10 KB,
disposable, regenerable and requested on every page load. `Config.workspace_root`
also returns `None` on an rclone deployment, so a file-backed avatar would be
unavailable on exactly the deployments with no other place to put it.

**Every store function takes a connection.** Both callers already hold one: a
route inside its own, and `user_profiles.delete_profile` inside an open write
transaction. `db.get_db` uses `timeout=30.0` and Python's legacy
`isolation_level`, so a *second* connection opened from inside such a block
waits the full thirty seconds on the write lock the caller is holding and then
raises — the hazard AGENTS.md documents for `notification_store`. Nothing in
this module opens a connection of its own.

That connection must carry `row_factory = sqlite3.Row`, which both in-repo
factories (`db.get_db`, `user_profiles._connect`) already set. The readers index
by column name, so a plain connection hands back tuples and raises `TypeError`
rather than the `sqlite3` error a route is prepared to let through.

Stdlib plus a function-local Pillow import, matching `executor.py` and
`health/ocr.py`. Importing Pillow at module scope would put it on the import
graph of everything that reads an avatar hash, including `/me`.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import sqlite3
from dataclasses import dataclass

logger = logging.getLogger(__name__)


AVATAR_EDGE = 192
# Proportionate to a 192px output, not to what a camera can produce. 8 MP is
# already ~20x the pixels that survive, and it bounds the decode at roughly
# 32 MB rather than the ~200 MB a 50 MP ceiling would allow per concurrent
# upload, on a host that instruments its own memory pressure.
#
# It bounds the *decode*, which is not the whole peak. Pillow decompresses a
# PNG's zTXt/iTXt chunks inside `Image.open`, before this ceiling or the format
# check is reached, and bounds them itself at `PngImagePlugin.MAX_TEXT_MEMORY`
# (64 MiB) — measured, a 67 KB PNG of nothing but text chunks peaks near 67 MB
# and is then accepted as an ordinary avatar, since neither the byte cap nor
# this ceiling can see it. So the honest figure is this plus Pillow's text
# budget. Left at Pillow's bound rather than tightened here, because the two
# knobs are module globals and this module writes none of them for the reason
# given below; a per-request memory budget belongs with the upload route.
AVATAR_MAX_PIXELS = 8_000_000
NORMALIZED_MIME = "image/webp"
# Pillow *format* names, not MIME types: `img.format` is the thing being
# checked, and round-tripping it through `Image.MIME` adds a failure mode for
# nothing (that map is populated lazily by `preinit()`, so a format can open
# fine and still have no MIME entry, which would 415 a valid image).
ACCEPTED_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "GIF", "HEIF"})
# What the file picker offers. Deliberately narrower than `image/*`, which
# matches TIFF, BMP, AVIF and SVG — all of which the server refuses, and the
# user would find out only after the upload.
ACCEPT_ATTRIBUTE = "image/jpeg,image/png,image/webp,image/gif,image/heic,image/heif"

SOURCE_UPLOAD = "upload"
SOURCE_NEXTCLOUD = "nextcloud"
# Precedence order, most preferred first. The resolver reads this, so adding a
# third source is one entry rather than a new branch.
SOURCE_PRECEDENCE = (SOURCE_UPLOAD, SOURCE_NEXTCLOUD)

# What makes a row a picture, written once because every reader of it has to
# agree exactly. `get_user_avatar` serves the bytes while `user_avatar_hash`
# supplies the ETag and the cache-busting `?v` for the same row, so a row one
# accepts and the other rejects renders as an avatar with no version, or as
# `/me` reporting nothing while the image endpoint serves something. Three
# clauses because three states are not pictures: the NULL-image probe row, a
# zero-length blob, and a row whose hash never got written (the column's own
# schema default is '').
_IS_A_PICTURE = "image IS NOT NULL AND length(image) > 0 AND content_hash <> ''"

# WebP encoder settings. One output format means the `mime` column has one
# possible value and `Content-Type` cannot be got wrong.
_WEBP_QUALITY = 82
_WEBP_METHOD = 4


@dataclass(frozen=True)
class Avatar:
    user_id: str
    source: str  # one of SOURCE_PRECEDENCE
    mime: str
    content_hash: str
    image: bytes
    updated_at: str


@dataclass(frozen=True)
class BotAvatar:
    mime: str
    content_hash: str
    image: bytes
    updated_at: str


class AvatarError(Exception):
    """Refusal to store an image, carrying the status the caller should see."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# --- normalization ----------------------------------------------------------


def normalize(
    raw: bytes, *, declared_format: str | None, max_bytes: int
) -> tuple[bytes, str]:
    """Decode, square-crop, resize and re-encode. Returns (webp_bytes, sha256_hex).

    Raises ``AvatarError(413)`` when `raw` exceeds `max_bytes` or the *declared*
    dimensions exceed ``AVATAR_MAX_PIXELS``, and ``AvatarError(415)`` when the
    bytes do not decode as a raster in an accepted format, and
    ``AvatarError(500)`` when Pillow itself is missing — a broken install rather
    than a bad upload. Nothing else escapes: a Pillow exception reaching a route
    would render as a 500 for what is an ordinary bad upload.

    `declared_format` is the client's claim about the content type. It is logged
    and never trusted — a browser routinely sends `application/octet-stream` for
    a dragged file, so a mismatch is not itself an error.
    """
    # First of two size checks. The second is the route's, on the stream, before
    # the body exists in memory at all; neither substitutes for the other.
    if len(raw) > max_bytes:
        raise AvatarError(413, "that image is too large")

    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError:  # pragma: no cover - Pillow is a core dependency
        # Not a 415: the upload is fine and the install is broken. Converted
        # rather than propagated so the module keeps its "only AvatarError
        # escapes" contract, and logged at error so it reads as what it is.
        logger.error("Pillow is unavailable; avatar normalization cannot run")
        raise AvatarError(500, "image support is unavailable") from None

    # Optional HEIC/HEIF support — iPhone photos arrive in this format. The
    # registration mutates process-global Pillow dicts and is already called
    # from the executor's worker threads; it is idempotent and each write is
    # GIL-atomic, so calling it from a second thread is safe.
    try:
        import pillow_heif  # type: ignore[import-not-found]

        pillow_heif.register_heif_opener()
    except ImportError:
        pass

    try:
        img = Image.open(io.BytesIO(raw))
    except UnidentifiedImageError:
        # SVG lands here and needs no special case: Pillow has no SVG decoder.
        raise AvatarError(415, "could not read that image") from None
    except Image.DecompressionBombError:
        # Pillow runs its own bomb check inside `open`, against a ceiling
        # (2 x MAX_IMAGE_PIXELS, ~179 MP) far above ours. Caught by name so the
        # very largest declared images get the same 413 as the merely large
        # ones below — swept into the blanket handler they came back 415
        # "could not read that image", telling the sender of a huge scan that
        # their file was corrupt rather than that it was too big.
        raise AvatarError(413, "that image has too many pixels") from None
    except Exception:
        logger.debug("avatar upload failed to open", exc_info=True)
        raise AvatarError(415, "could not read that image") from None

    with img:
        # The header is all that has been read so far. Refuse on the *declared*
        # dimensions before anything decodes. Deliberately a per-call check
        # rather than a write to `Image.MAX_IMAGE_PIXELS`, which is a module
        # global and would race the attachment pre-shrink in another thread.
        width, height = img.size
        if width * height > AVATAR_MAX_PIXELS:
            raise AvatarError(413, "that image has too many pixels")

        fmt = (img.format or "").upper()
        if fmt not in ACCEPTED_FORMATS:
            logger.info(
                "avatar upload refused: format %r (client declared %r)",
                fmt or "unknown",
                declared_format,
            )
            raise AvatarError(415, "that image format is not supported")

        try:
            if fmt == "JPEG":
                # Ask libjpeg to downsample at decode time, so a large
                # photograph does not fully decode into RAM before being
                # thumbnailed. `executor.py` does this for the same reason.
                img.draft("RGB", (AVATAR_EDGE, AVATAR_EDGE))

            # The first call that actually decodes.
            img = ImageOps.exif_transpose(img)

            # The mode is normalized *before* the resize, not after, and the
            # order is load-bearing rather than tidy: `Image.resize` silently
            # replaces the resampling filter with NEAREST for modes "P" and
            # "1", so a palette image reaching `fit` is downscaled by point
            # sampling whatever filter it was handed. GIF is always "P" and is
            # a first-class accepted format here, and PNG-8 is common; measured,
            # a fine-detail palette image came out of `fit` as a solid block of
            # one colour. Converting first also means the alpha flatten happens
            # at full resolution, which is where it belongs — downscaling RGBA
            # first bleeds the colour under transparent pixels into the edges,
            # since Pillow does not premultiply.
            if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
                # Flatten onto white. WebP transparency over the two themes'
                # different surfaces reads as a hole.
                img = img.convert("RGBA")
                flat = Image.new("RGB", img.size, (255, 255, 255))
                flat.paste(img, mask=img.split()[3])
                img = flat
            elif img.mode not in ("RGB", "L"):
                img = img.convert("RGB")

            # A centre crop to square, not a letterbox: every render site is a
            # square box, and a letterboxed portrait in a 33px chip is
            # unreadable.
            img = ImageOps.fit(
                img,
                (AVATAR_EDGE, AVATAR_EDGE),
                Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )

            # `exif_transpose` writes the source EXIF back onto its result and
            # `resize` copies `info` forward, so the image handed to `save`
            # carries the uploader's EXIF and ICC profile. Pillow 12's WebP
            # writer reads neither from `info` — but `pyproject.toml` permits
            # any Pillow >= 10, and the failure mode of trusting that is a
            # photograph's GPS coordinates served to every co-member. Dropped
            # here so the guarantee is this module's rather than the writer's.
            for key in ("exif", "icc_profile", "xmp", "XML:com.adobe.xmp"):
                img.info.pop(key, None)

            buf = io.BytesIO()
            # Nothing is passed as `exif=` or `icc_profile=` either, so the two
            # ways metadata could reach the output are both shut.
            # `TestNormalizeStrips` holds it.
            img.save(buf, "WEBP", quality=_WEBP_QUALITY, method=_WEBP_METHOD)
        except AvatarError:
            raise
        except Exception:
            # A truncated file, or a decoder error mid-decode.
            logger.debug("avatar upload failed to decode", exc_info=True)
            raise AvatarError(415, "could not read that image") from None

    out = buf.getvalue()
    return out, hashlib.sha256(out).hexdigest()


# --- the user avatar store --------------------------------------------------


def put_user_avatar(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    source: str,
    image: bytes | None,
    content_hash: str,
    remote_etag: str | None = None,
) -> None:
    """Write the (user, source) row, replacing whatever was there.

    `remote_etag=None` means "I did not look at the remote" and leaves the
    stored value alone; `""` means "the remote named no ETag" and clears it.
    They are different statements and the default is the first. A plain `""`
    default would make the one call in the import job that stores an actual
    image — the call most likely to be written without threading the ETag
    through — wipe the validator, which reverts that user to an unconditional
    download every tick, silently and for good.
    """
    conn.execute(
        """
        INSERT INTO user_avatars
            (user_id, source, mime, content_hash, image, remote_etag,
             checked_at, updated_at)
        VALUES (?, ?, ?, ?, ?, COALESCE(?, ''), datetime('now'), datetime('now'))
        ON CONFLICT(user_id, source) DO UPDATE SET
            mime = excluded.mime,
            content_hash = excluded.content_hash,
            image = excluded.image,
            remote_etag = COALESCE(?, user_avatars.remote_etag),
            checked_at = datetime('now'),
            updated_at = datetime('now')
        """,
        (
            user_id, source, NORMALIZED_MIME, content_hash, image,
            remote_etag, remote_etag,
        ),
    )


def touch_import_probe(
    conn: sqlite3.Connection, user_id: str, *, remote_etag: str
) -> None:
    """Record that the import probe ran and found no custom Nextcloud avatar.

    A NULL-image row is not an avatar — it is the negative result, kept so the
    next tick can send `If-None-Match` instead of re-downloading a generated
    placeholder for every user, every tick, forever.

    The UPDATE branch deliberately does not touch `image`, `mime` or
    `content_hash`. Two reasons, and both matter: an ETag-only change must not
    push 10 KB of overflow pages into the WAL, and a user who *removes* their
    Nextcloud avatar keeps the copy already imported rather than having it
    silently blanked.
    """
    conn.execute(
        """
        INSERT INTO user_avatars
            (user_id, source, mime, content_hash, image, remote_etag,
             checked_at, updated_at)
        VALUES (?, ?, ?, '', NULL, ?, datetime('now'), datetime('now'))
        ON CONFLICT(user_id, source) DO UPDATE SET
            remote_etag = excluded.remote_etag,
            checked_at = datetime('now')
        """,
        (user_id, SOURCE_NEXTCLOUD, NORMALIZED_MIME, remote_etag),
    )


def get_user_avatar(conn: sqlite3.Connection, user_id: str) -> Avatar | None:
    """The user's picture: the first source in `SOURCE_PRECEDENCE` that has one.

    A NULL-image row is a probe result, not a picture, so it is filtered out
    here and at every other read of the chain.
    """
    rows = conn.execute(
        """
        SELECT user_id, source, mime, content_hash, image, updated_at
        FROM user_avatars
        WHERE user_id = ? AND {pred}
        """.format(pred=_IS_A_PICTURE),
        (user_id,),
    ).fetchall()
    row = _most_preferred(rows)
    if row is None:
        return None
    return Avatar(
        user_id=row["user_id"],
        source=row["source"],
        mime=row["mime"] or NORMALIZED_MIME,
        content_hash=row["content_hash"] or "",
        image=bytes(row["image"]),
        updated_at=row["updated_at"] or "",
    )


def user_avatar_hash(conn: sqlite3.Connection, user_id: str) -> str | None:
    """The same walk as `get_user_avatar`, without loading the blob.

    `/me` and the room list call this per render; selecting the image column
    would pull ~10 KB out of SQLite for a 64-character answer.
    """
    rows = conn.execute(
        """
        SELECT source, content_hash
        FROM user_avatars
        WHERE user_id = ? AND {pred}
        """.format(pred=_IS_A_PICTURE),
        (user_id,),
    ).fetchall()
    row = _most_preferred(rows)
    if row is None:
        return None
    return row["content_hash"]


def _most_preferred(rows) -> sqlite3.Row | None:
    """Pick the row whose source comes first in `SOURCE_PRECEDENCE`.

    A source outside that tuple is ignored rather than ranked last: precedence
    is what the tuple states, and a row nothing declared has no place in it.
    """
    best: sqlite3.Row | None = None
    best_rank = len(SOURCE_PRECEDENCE)
    for row in rows:
        try:
            rank = SOURCE_PRECEDENCE.index(row["source"])
        except ValueError:
            continue
        if rank < best_rank:
            best, best_rank = row, rank
    return best


def delete_user_avatar(conn: sqlite3.Connection, user_id: str, source: str) -> bool:
    """Drop one (user, source) row. True when a row went."""
    cur = conn.execute(
        "DELETE FROM user_avatars WHERE user_id = ? AND source = ?",
        (user_id, source),
    )
    return cur.rowcount > 0


def delete_all_user_avatars(conn: sqlite3.Connection, user_id: str) -> int:
    """Drop every row for a user, probe rows included. Returns rows deleted."""
    cur = conn.execute("DELETE FROM user_avatars WHERE user_id = ?", (user_id,))
    return cur.rowcount


def import_probe_state(conn: sqlite3.Connection) -> dict[str, str]:
    """`{user_id: remote_etag}` over the `nextcloud` rows, probe rows included.

    This is the ETag lookup for the import job and emphatically **not** its user
    set. The job enumerates `config.users`: a user with no `nextcloud` row is
    exactly the user who needs the first import, so deriving the set from this
    table excludes precisely them and the feature imports nothing, ever.

    This is the one read that does not name a user, so it is the one the primary
    key does not cover: a full scan of `user_avatars`, once per import tick,
    bounded by the deployment's user count. Deliberately not indexed.
    """
    rows = conn.execute(
        "SELECT user_id, remote_etag FROM user_avatars WHERE source = ?",
        (SOURCE_NEXTCLOUD,),
    ).fetchall()
    return {row["user_id"]: row["remote_etag"] or "" for row in rows}


def import_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many `nextcloud` rows are pictures, and how many are negative probes.

    Two numbers rather than one, because they mean opposite things to whoever is
    reading: `imported` is the feature working, and `probes` is the feature
    having asked and been told no. A deployment where every row is a probe and
    none is a picture is either one where nobody has set a Nextcloud avatar or
    one where the custom-avatar header never arrives — which is why the doctor
    check reads the recorded tick state as well as these.

    The same full scan `import_probe_state` is, and for the same reason: the
    primary key is `(user_id, source)` and this names no user.
    """
    row = conn.execute(
        f"""
        SELECT
            SUM(CASE WHEN {_IS_A_PICTURE} THEN 1 ELSE 0 END) AS imported,
            SUM(CASE WHEN {_IS_A_PICTURE} THEN 0 ELSE 1 END) AS probes
        FROM user_avatars WHERE source = ?
        """,
        (SOURCE_NEXTCLOUD,),
    ).fetchone()
    return {
        "imported": int(row["imported"] or 0),
        "probes": int(row["probes"] or 0),
    }


# --- what the last import tick saw ------------------------------------------

# The import job runs in the daemon; `doctor` runs there too, but also in the
# `istota doctor` process and in the web process behind the admin Health pane.
# So "what did the last tick see" cannot be module state — it has to be written
# down somewhere all three can read.
#
# `shared_kv` rather than a table of its own: it is one small JSON row of
# deployment-wide daemon state, which is exactly what that table holds, and a
# whole table plus a migration for six integers is not worth it. The leading
# underscore is what keeps it away from the model — `kv_namespaces` reserves the
# prefix, so the `kv` skill refuses the namespace on every verb and the deferred
# op replay refuses it again for a sandboxed task.
IMPORT_STATE_NAMESPACE = "_avatar_import"
IMPORT_STATE_KEY = "last_tick"
_IMPORT_STATE_WRITER = "scheduler:avatar_import"

# Whether the tick could tell a user-set Nextcloud avatar from a generated one.
# Three values, not a boolean, because "no response this tick could have carried
# the header" is a third thing: a tick where every user answered 304 sees no
# header at all, and reporting that as `absent` would page an operator about a
# Nextcloud that is answering perfectly.
HEADER_SEEN = "seen"
HEADER_ABSENT = "absent"
HEADER_UNOBSERVED = "unobserved"


def write_import_state(conn: sqlite3.Connection, state: dict) -> None:
    """Record what the tick that just finished did. Replaces the previous row."""
    from . import db as _db

    _db.shared_kv_set(
        conn,
        IMPORT_STATE_NAMESPACE,
        IMPORT_STATE_KEY,
        json.dumps(state, sort_keys=True),
        _IMPORT_STATE_WRITER,
    )


def read_import_state(conn: sqlite3.Connection) -> dict | None:
    """The last tick's record, or None when no tick has been recorded.

    Returns None rather than raising on a row that will not parse: the one
    caller is a doctor check, and a check that raised on a malformed value would
    take the daemon's start-up path with it. A row nobody can read is
    indistinguishable, for the operator, from no row at all.
    """
    from . import db as _db

    row = _db.shared_kv_get(conn, IMPORT_STATE_NAMESPACE, IMPORT_STATE_KEY)
    if row is None:
        return None
    try:
        parsed = json.loads(row["value"])
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


# --- the bot avatar store ---------------------------------------------------


def get_bot_avatar(conn: sqlite3.Connection) -> BotAvatar | None:
    row = conn.execute(
        "SELECT mime, content_hash, image, updated_at FROM bot_avatar "
        f"WHERE id = 1 AND {_IS_A_PICTURE}"
    ).fetchone()
    if row is None:
        return None
    return BotAvatar(
        mime=row["mime"] or NORMALIZED_MIME,
        content_hash=row["content_hash"] or "",
        image=bytes(row["image"]),
        updated_at=row["updated_at"] or "",
    )


def bot_avatar_hash(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        f"SELECT content_hash FROM bot_avatar WHERE id = 1 AND {_IS_A_PICTURE}"
    ).fetchone()
    if row is None:
        return None
    return row["content_hash"]


def put_bot_avatar(
    conn: sqlite3.Connection, *, image: bytes, content_hash: str
) -> None:
    """Set the deployment's bot icon. One row, so last writer wins."""
    conn.execute(
        """
        INSERT INTO bot_avatar (id, mime, content_hash, image, updated_at)
        VALUES (1, ?, ?, ?, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            mime = excluded.mime,
            content_hash = excluded.content_hash,
            image = excluded.image,
            updated_at = datetime('now')
        """,
        (NORMALIZED_MIME, content_hash, image),
    )


def delete_bot_avatar(conn: sqlite3.Connection) -> bool:
    """Clear the bot icon. True when a row went; idempotent."""
    cur = conn.execute("DELETE FROM bot_avatar WHERE id = 1")
    return cur.rowcount > 0
