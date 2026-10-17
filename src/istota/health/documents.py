"""Document storage for the health module.

Bytes on disk under ``{uploads_dir}/documents/{id}/{filename}``, metadata in
the per-user health DB (``documents`` + ``document_links``). Kept out of
:mod:`istota.health.routes` so the skill CLI and the deferred-op replayer
share the same code path verbatim rather than reimplementing containment
and sanitisation.

The parent is ``uploads_dir``, the same tree panel sources live in, so one
open covers both: ``open_upload``, a no-follow walk from the user root
(ISSUE-781).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
import stat
import time
from pathlib import Path

from istota.lib.atomic_write import replace_at
from istota.lib.filenames import safe_filename
from istota.health import db as health_db
from istota.health.models import Document, HealthContext


logger = logging.getLogger(__name__)


# Deliberately narrow. Everything here is either a scan (PDF / image) or a
# plain-text export; anything active (HTML, SVG) is refused at the door
# rather than relying on the Content-Disposition header alone.
_ALLOWED_DOCUMENT_MIMES = frozenset({
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/heic",
    "image/heif",
    "image/webp",
    "image/tiff",
    "text/plain",
})

DEFAULT_MAX_DOCUMENT_BYTES = 25 * 1024 * 1024


def max_document_bytes(config) -> int:
    """The operator's ``[health] max_document_bytes``, ``0`` meaning unlimited.

    The one reader for the web routes, the daemon's import extraction and the
    agent's deferred ops: the replayer once fell through to the default, so a
    cap of ``0`` silently did not apply to a document the agent filed.
    """
    raw = getattr(getattr(config, "health", None), "max_document_bytes", None)
    if raw is None:
        return DEFAULT_MAX_DOCUMENT_BYTES
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_DOCUMENT_BYTES


_DOCUMENTS_SUBDIR = "documents"

_MAX_STEM_CHARS = 100


class DocumentError(Exception):
    """Base for document-storage refusals."""


class UnsupportedDocumentType(DocumentError):
    pass


class DocumentTooLarge(DocumentError):
    pass


class UnknownEntity(DocumentError):
    """The encounter / diagnosis / immunization to attach to doesn't exist."""


def sanitize_document_filename(raw: str) -> str:
    """Derive a safe on-disk name from an untrusted client filename.

    Each document lives in its own numbered directory, so this does not have
    to guarantee uniqueness — only that the name can't escape that directory
    or hide from a file browser. ASCII only, because the name goes back out in
    a ``Content-Disposition`` header.
    """
    return safe_filename(
        raw, ascii_only=True, max_stem=_MAX_STEM_CHARS, fallback="document.bin",
    )


def relative_document_path(document_id: int, filename: str) -> str:
    """The ``stored_path`` for a document — relative to ``uploads_dir``."""
    return f"{_DOCUMENTS_SUBDIR}/{int(document_id)}/{filename}"


def resolve_document_path(ctx: HealthContext, doc: Document) -> Path:
    """Absolute path of a document's bytes, containment-checked.

    Raises :class:`ValueError` when the stored path resolves outside
    ``uploads_dir`` — the second line of defence behind sanitisation, and
    the one that matters for a row written before this code existed or by
    something other than :func:`store_document`.
    """
    uploads_root = Path(ctx.uploads_dir).resolve()
    candidate = (Path(ctx.uploads_dir) / doc.stored_path).resolve()
    try:
        candidate.relative_to(uploads_root)
    except ValueError:
        raise ValueError("invalid source path")
    return candidate


# Leading bytes that identify a format unambiguously. Consulted only when the
# declared type and the filename both fail to name one — an email attachment
# saved without an extension is the common case, and refusing a perfectly good
# scan for want of a ".pdf" is a poor answer.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)


def _sniff_mime(raw: bytes) -> str | None:
    for prefix, mime in _MAGIC:
        if raw.startswith(prefix):
            return mime
    # RIFF....WEBP
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    # ....ftypheic / ftypheix / ftypmif1 (HEIF brands)
    if raw[4:8] == b"ftyp" and raw[8:12] in (
        b"heic", b"heix", b"hevc", b"mif1", b"msf1",
    ):
        return "image/heic"
    return None


def _resolve_mime(mime: str | None, filename: str, raw: bytes = b"") -> str:
    import mimetypes

    if mime:
        # Strip any "; charset=..." parameter the client tacked on.
        mime = mime.split(";")[0].strip().lower()
    if not mime or mime == "application/octet-stream":
        guessed = mimetypes.guess_type(filename)[0]
        if guessed:
            mime = guessed.lower()
    if (not mime or mime == "application/octet-stream") and raw:
        sniffed = _sniff_mime(raw)
        if sniffed:
            mime = sniffed
    return mime or "application/octet-stream"


def uploads_walk(ctx: HealthContext, *parts: str) -> tuple[Path, list[str]]:
    """Where a write under ``uploads_dir`` starts, and the components below it.

    The uploads tree is in the user's workspace, which their tasks can write,
    so a link planted at ``health`` or ``uploads`` would send a write
    anywhere (ISSUE-723). The walk therefore starts at the parent of
    ``workspace_root`` and every component from the bot directory down is
    opened without following a link: the bot directory was checked when the
    context was resolved, and a task can still swap it for a link afterwards.
    An ``uploads_dir`` configured outside the workspace is its own root.

    With the configured user root known (``resolve_for_user`` sets it), the
    walk starts there instead (ISSUE-781). The bot directory may be a link to
    elsewhere in the user's tree, and the context resolves through it, so the
    parent of the resolved workspace can be a directory the user writes; the
    walk opens its anchor following links, and only the user root may be.
    An ``uploads_dir`` outside that root is refused with :class:`ValueError`
    rather than anchored anywhere else.
    """
    uploads = Path(ctx.uploads_dir)
    workspace = Path(ctx.workspace_root)
    if ctx.user_root is not None:
        try:
            rel = uploads.relative_to(ctx.user_root)
        except ValueError:
            raise ValueError("uploads_dir is outside the user root") from None
        return Path(ctx.user_root), [*rel.parts, *parts]
    try:
        rel = uploads.relative_to(workspace)
    except ValueError:
        return uploads, list(parts)
    return workspace.parent, [workspace.name, *rel.parts, *parts]


def open_upload(ctx: HealthContext, rel: str) -> int | None:
    """A read-only fd on the regular file at ``uploads_dir/rel``, or None.

    For the download routes, which serve this fd rather than a path a second
    open would resolve again (ISSUE-781): every directory from the walk's
    anchor down is opened ``O_NOFOLLOW`` and the leaf is ``O_NOFOLLOW`` plus
    ``S_ISREG``. Raises :class:`ValueError` for a stored path that is not a
    plain relative one, or one the walk refused because a component is a
    link; None means missing or not a regular file. The caller closes the fd.
    """
    from istota.sandbox.attachment_source import open_regular_at  # noqa: PLC0415
    from istota.skills._loader import open_overlay_dir  # noqa: PLC0415

    parts = (rel or "").split("/")
    if rel.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise ValueError("invalid source path")
    *dirs, leaf = parts
    root, walk = uploads_walk(ctx, *dirs)
    dir_fd = open_overlay_dir(root, *walk)
    fd = None
    if dir_fd is not None:
        try:
            fd = open_regular_at(dir_fd, leaf)
        finally:
            os.close(dir_fd)
    if fd is None and _walk_meets_a_link(root, [*walk, leaf]):
        raise ValueError("invalid source path")
    return fd


def _walk_meets_a_link(root: Path, parts: list[str]) -> bool:
    """Whether a refused walk was refused at a link. Picks the status only."""
    current = root
    for part in parts:
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except OSError:
            return False
    return False


def remove_upload_dir(ctx: HealthContext, *parts: str) -> bool:
    """Remove the directory ``uploads_dir/<parts>`` and everything below it.

    The parent is reached by the no-follow walk and the tree is removed
    relative to that descriptor, so a link anywhere on the way deletes
    nothing outside the user's tree (ISSUE-781). ``shutil.rmtree`` with
    ``dir_fd`` refuses a link at the leaf itself. False when there was
    nothing to remove or the removal failed.
    """
    *dirs, name = parts
    try:
        fd = _open_document_dir(ctx, Path(*dirs) if dirs else Path(), create=False)
    except (OSError, ValueError):
        return False
    if fd is None:
        return False
    try:
        shutil.rmtree(name, dir_fd=fd)
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning(
            "health_upload_dir_remove_failed path=%s error=%s",
            "/".join(parts), getattr(e, "errno", None) or type(e).__name__,
        )
        return False
    finally:
        os.close(fd)
    return True


def _clear_document_dir_at(dir_fd: int) -> None:
    """Empty a document's numbered directory without removing it."""
    for name in os.listdir(dir_fd):
        try:
            if stat.S_ISDIR(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                continue
            os.unlink(name, dir_fd=dir_fd)
        except OSError as e:
            logger.warning(
                "health_document_dir_clear_failed name=%s error=%s", name, e,
            )


def _open_document_dir(ctx: HealthContext, rel_dir: Path, *, create: bool) -> int | None:
    """A descriptor on ``uploads_dir/rel_dir``, reached with no link followed."""
    from istota.skills._loader import open_overlay_dir  # noqa: PLC0415
    from istota.workspace.writes import open_or_create_dirs  # noqa: PLC0415

    root, walk = uploads_walk(ctx, *rel_dir.parts)
    if create:
        return open_or_create_dirs(root, walk)
    return open_overlay_dir(root, *walk, raise_mount_errors=True)


def _write_bytes(ctx: HealthContext, rel: str, raw: bytes, *, clear: bool = False) -> None:
    """Write ``uploads_dir/rel`` via a staging sibling + rename, so a torn
    write is never visible under the final name.

    The staging name used to be a fixed ``.{name}.part`` shared by every
    writer, with nothing removing it on a failure: two uploads of one document
    interleaved into one file and published a mixture. ``replace_at`` mints it
    per call and unlinks it on any failure. The directory is opened from the
    workspace root with no link followed, and both the clear and the write go
    through that one descriptor.
    """
    rel_path = Path(rel)
    fd = _open_document_dir(ctx, rel_path.parent, create=True)
    try:
        if clear:
            _clear_document_dir_at(fd)
        replace_at(fd, rel_path.name, raw)
    finally:
        os.close(fd)


def _heal_missing_bytes(ctx: HealthContext, doc: Document, raw: bytes) -> None:
    """Re-materialise a deduped document whose file has gone missing.

    Without this, once a document's bytes are lost (a manual workspace edit, a
    failed sync) re-uploading the identical file is a silent no-op: the hash
    matches, the broken row comes back, and `/documents/{id}/file` 404s
    forever. The upload "succeeds" and fixes nothing.
    """
    try:
        path = resolve_document_path(ctx, doc)
    except ValueError:
        return
    if path.is_file():
        return
    try:
        _write_bytes(ctx, doc.stored_path, raw)
    except (OSError, ValueError) as e:
        logger.error(
            "health_document_heal_failed id=%d path=%s error=%s",
            doc.id, path, e,
        )
        return
    logger.info("health_document_healed id=%d path=%s", doc.id, path)


def store_document(
    conn: sqlite3.Connection,
    ctx: HealthContext,
    *,
    raw: bytes,
    filename: str,
    mime: str | None,
    source: str = "manual",
    ocr_text: str | None = None,
    notes: str | None = None,
    max_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
) -> tuple[Document, bool]:
    """Write bytes + insert a row. Returns ``(document, created)``.

    ``created`` is False when a document with identical bytes was already
    stored: the existing row is returned and nothing new is written. That is
    the normal case, not an error — attaching one discharge summary to both
    the encounter and the condition it produced should cost one file (D4).
    The dedup hit still *touches* the row, restarting its orphan clock: a
    re-uploaded old scan is being actively used again and must survive its
    own review screen.

    The bytes are written before the caller commits (no caller commits
    mid-function), so a failure after this returns leaves a blob on disk with
    no row. `sweep_orphan_documents` reclaims those directories as well as
    orphaned rows — without that second pass they would be invisible forever,
    since every DB-driven enumeration starts from a row that doesn't exist.
    """
    if not raw:
        raise DocumentError("empty upload")

    resolved_mime = _resolve_mime(mime, filename, raw)
    if resolved_mime not in _ALLOWED_DOCUMENT_MIMES:
        raise UnsupportedDocumentType(
            f"unsupported document type: {resolved_mime}",
        )
    if max_bytes and len(raw) > max_bytes:
        raise DocumentTooLarge(f"document exceeds {max_bytes} bytes")

    content_hash = hashlib.sha256(raw).hexdigest()
    existing = health_db.find_document_by_hash(conn, content_hash)
    if existing is not None:
        health_db.touch_document(conn, existing.id)
        _heal_missing_bytes(ctx, existing, raw)
        return existing, False

    safe_name = sanitize_document_filename(filename)
    try:
        doc_id = health_db.insert_document(
            conn,
            filename=safe_name,
            original_filename=filename or None,
            mime=resolved_mime,
            byte_size=len(raw),
            content_hash=content_hash,
            # Placeholder: the real path needs the id the insert just minted.
            stored_path=relative_document_path(0, safe_name),
            ocr_text=ocr_text,
            source=source,
            notes=notes,
        )
    except sqlite3.IntegrityError:
        # Lost a race against a concurrent attach of the same bytes — the
        # UNIQUE index on content_hash fired. Resolve to the same outcome as
        # a plain duplicate upload.
        duplicate = health_db.find_document_by_hash(conn, content_hash)
        if duplicate is None:
            raise
        return duplicate, False

    rel = relative_document_path(doc_id, safe_name)
    conn.execute(
        "UPDATE documents SET stored_path = ? WHERE id = ?", (rel, doc_id),
    )

    # A rolled-back predecessor can leave a stale file under a reused id
    # (AUTOINCREMENT hands the number back). Clear the directory first, or
    # the leftover shares the dir with the live document and blocks the
    # rmdir in delete_document_fully.
    _write_bytes(ctx, rel, raw, clear=True)

    doc = health_db.get_document(conn, doc_id)
    if doc is None:  # pragma: no cover — the insert just succeeded
        raise DocumentError("document vanished after insert")
    return doc, True


def attach_document(
    conn: sqlite3.Connection,
    ctx: HealthContext,
    *,
    raw: bytes,
    filename: str,
    mime: str | None,
    entity_type: str,
    entity_id: int,
    source: str = "manual",
    ocr_text: str | None = None,
    notes: str | None = None,
    max_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
) -> tuple[Document, bool]:
    """:func:`store_document` + ``link_document`` in one call.

    Both the entity type and the entity's existence are checked **before**
    anything is stored. A link to a record that isn't there is worse than an
    error: `entity_id` is polymorphic with no FK, so the document would be
    invisible on every page *and* permanently exempt from the orphan sweep,
    because it does have a link. Validating first also means a bad reference
    costs no bytes on disk.
    """
    if not health_db.entity_exists(conn, entity_type, entity_id):
        raise UnknownEntity(f"{entity_type} not found")
    doc, created = store_document(
        conn, ctx, raw=raw, filename=filename, mime=mime, source=source,
        ocr_text=ocr_text, notes=notes, max_bytes=max_bytes,
    )
    health_db.link_document(conn, doc.id, entity_type, entity_id)
    return doc, created


def delete_document_fully(
    conn: sqlite3.Connection, ctx: HealthContext, document_id: int,
) -> bool:
    """Remove links, row, bytes, and the now-empty numbered directory."""
    doc = health_db.get_document(conn, document_id)
    if doc is None:
        return False
    try:
        path = resolve_document_path(ctx, doc)
    except ValueError:
        # A row whose stored_path escapes uploads_dir: drop the row but
        # never touch the file it points at.
        logger.error(
            "health_document_path_invalid id=%d stored_path=%s",
            doc.id, doc.stored_path,
        )
        path = None
    health_db.delete_document(conn, document_id)
    if path is not None:
        rel = Path(doc.stored_path)
        try:
            fd = _open_document_dir(ctx, rel.parent, create=False)
            if fd is not None:
                try:
                    try:
                        os.unlink(rel.name, dir_fd=fd)
                    except FileNotFoundError:
                        pass
                    if rel.parent.parts == (_DOCUMENTS_SUBDIR, str(doc.id)):
                        # Only ever the document's own numbered directory.
                        # Clear it first: a rolled-back predecessor under a
                        # reused id can have left a stale sibling, and rmdir
                        # would silently fail on it.
                        _clear_document_dir_at(fd)
                finally:
                    os.close(fd)
                if rel.parent.parts == (_DOCUMENTS_SUBDIR, str(doc.id)):
                    parent_fd = _open_document_dir(ctx, Path(_DOCUMENTS_SUBDIR), create=False)
                    if parent_fd is not None:
                        try:
                            os.rmdir(str(doc.id), dir_fd=parent_fd)
                        except OSError:
                            pass
                        finally:
                            os.close(parent_fd)
        except (OSError, ValueError) as e:
            logger.error(
                "health_document_unlink_failed id=%d path=%s error=%s",
                doc.id, path, e,
            )
    return True


def sweep_orphan_documents(
    conn: sqlite3.Connection,
    ctx: HealthContext,
    *,
    older_than_hours: float = 24.0,
) -> int:
    """Delete documents that have had no links for longer than the window.

    Two passes, because a document can be orphaned in either direction. The
    row pass drops linkless rows (and their bytes); the directory pass drops
    bytes with no row — `store_document` writes the file before the caller
    commits, so any rollback after it returns strands a blob that no
    DB-driven enumeration would ever reach.

    Returns the count of deleted *rows*; stranded directories are logged.
    """
    ids = health_db.orphan_document_ids(conn, older_than_hours=older_than_hours)
    swept = 0
    for did in ids:
        try:
            if delete_document_fully(conn, ctx, did):
                swept += 1
        except sqlite3.Error as e:
            logger.error("health_document_sweep_failed id=%d error=%s", did, e)
    if swept:
        logger.info("health_document_sweep swept=%d", swept)
    _sweep_stranded_dirs(conn, ctx, older_than_hours=older_than_hours)
    return swept


def _sweep_stranded_dirs(
    conn: sqlite3.Connection,
    ctx: HealthContext,
    *,
    older_than_hours: float,
) -> int:
    """Remove `{uploads_dir}/documents/<id>/` directories with no row.

    Age-gated on directory mtime for the same reason the row pass is gated:
    `store_document` creates the directory before its row is committed, so a
    document being written *right now* legitimately looks stranded.
    """
    try:
        fd = _open_document_dir(ctx, Path(_DOCUMENTS_SUBDIR), create=False)
    except (OSError, ValueError):
        fd = None
    if fd is None:
        return 0
    cutoff = time.time() - older_than_hours * 3600.0
    stale: list[str] = []
    try:
        with os.scandir(fd) as scan:
            for entry in scan:
                if not entry.name.isdigit() or not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                        continue
                except OSError:
                    continue
                stale.append(entry.name)
    except OSError as e:
        logger.warning("health_document_dir_scan_failed error=%s", e)
        os.close(fd)
        return 0
    removed = 0
    try:
        for name in stale:
            if health_db.get_document(conn, int(name)) is not None:
                continue
            try:
                shutil.rmtree(name, dir_fd=fd)
            except OSError as e:
                logger.warning(
                    "health_document_dir_sweep_failed id=%s error=%s", name, e,
                )
                continue
            removed += 1
    finally:
        os.close(fd)
    if removed:
        logger.info("health_document_dir_sweep removed=%d", removed)
    return removed
