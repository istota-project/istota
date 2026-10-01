"""Media: `media list`, `media upload` and `media update`, and the uploads a
post write makes for ``--featured-image`` and ACF ``$upload`` markers.

**Where the bytes come from.** Every file sent is named by a path the `EGRESS`
rule admits: an argv path through its stamp, a path inside an ACF value
through `_hostpath.egress_roots()` and `memory_refusal` (`acf.py`). It is
opened ``O_NOFOLLOW`` in the precheck, before the vault fetch, its size checked
on the descriptor against ``[wordpress] max_upload_mb``, and its first bytes
sniffed. The descriptor is held and the upload reads through it, so a path
swapped afterwards changes nothing.

**What it is sent as.** An image goes as the type `image_sniff.sniff_decodable`
reads from its bytes, and a file named like an image whose bytes are not one
is refused. Anything else goes as ``application/octet-stream``: WordPress
decides the type from the file name against its own allowed list, and refuses
a type it does not allow with ``rest_upload_sideload_error`` before it stores
anything, which the client reads as a refusal rather than an ambiguous end.
Core REST publishes no list of allowed types to check first; spec §5.3 assumed
one.

**One send.** An upload is never retried, and an ambiguous end is
``outcome_unknown`` with a ``media list --search`` lookup. Title, alt text and
caption go with the upload, and any the answer did not keep are set by a
follow-up ``POST /media/{id}`` (spec §14.4 is not settled). A follow-up that
fails does not undo the upload; it is reported as ``metadata_error``.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from istota.image_sniff import sniff_decodable

from .client import UPLOAD_TIMEOUT, WordPressError, fence, raw_text
from .common import limit_arg, lookup, total_header

MEDIA_ROUTE = "wp/v2/media"
#: Files one invocation may upload, between the argv and every ACF marker.
MAX_UPLOADS = 20
_HEAD_BYTES = 64
#: Extensions `sniff_decodable` recognises; a file named so must sniff as one.
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".jpe", ".gif", ".webp", ".heic", ".heif"})
#: Answered with a 5xx before WordPress stores anything (a disallowed type).
_DEFINITE_CODES = frozenset({"rest_upload_sideload_error"})
_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_META_FIELDS = ("title", "alt_text", "caption")


def project_media(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "slug": item.get("slug"),
        "date": item.get("date"),
        "post": item.get("post"),
        "media_type": item.get("media_type"),
        "mime_type": item.get("mime_type"),
        "title": fence(raw_text(item.get("title"))),
        "alt_text": fence(item.get("alt_text")),
        "caption": fence(raw_text(item.get("caption"))),
        "source_url": fence(item.get("source_url")),
    }


def cmd_media_list(args) -> dict:
    ctx = args.wp
    params: dict = {"context": "edit", "per_page": limit_arg(args.limit, 20), "page": args.page}
    if args.search:
        params["search"] = args.search
    if args.mime:
        # A slash makes it a full MIME type; otherwise core's media_type
        # (image, video, audio, text, application).
        params["mime_type" if "/" in args.mime else "media_type"] = args.mime
    items, headers = ctx.client.get(MEDIA_ROUTE, params=params, base=ctx.base)
    rows = [project_media(i) for i in items or [] if isinstance(i, dict)]
    return {
        "status": "ok",
        **ctx.envelope(),
        "count": len(rows),
        "total": total_header(headers, "X-WP-Total"),
        "items": rows,
    }


# --------------------------------------------------------------------------- #
# Uploads
# --------------------------------------------------------------------------- #


@dataclass
class UploadSource:
    """One file to upload, open, checked and sniffed, with its metadata."""

    path: str
    fd: int
    size: int
    mime: str
    filename: str
    meta: dict = field(default_factory=dict)

    def read(self) -> bytes:
        """The whole file, through the descriptor opened at check time."""
        chunks: list[bytes] = []
        offset = 0
        while offset <= self.size:
            chunk = os.pread(self.fd, min(1024 * 1024, self.size + 1 - offset), offset)
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
        data = b"".join(chunks)
        if len(data) != self.size:
            raise WordPressError(f"{self.filename} changed size while it was read; "
                                 f"nothing was sent.", "validation_error")
        return data

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def upload_cap(config) -> int:
    """``[wordpress] max_upload_mb`` in bytes, never below one MiB."""
    try:
        megabytes = int(config.wordpress.max_upload_mb)
    except (AttributeError, TypeError, ValueError):
        megabytes = 25
    return max(1, megabytes) * 1024 * 1024


def safe_filename(path: str) -> str:
    """The name sent in ``Content-Disposition``: no quote, semicolon or control
    character can reach WordPress's header parser, and the extension, which is
    how WordPress decides the type, is kept."""
    name = Path(path).name
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    stem = _FILENAME_UNSAFE.sub("_", stem).strip("._") or "upload"
    suffix = _FILENAME_UNSAFE.sub("", suffix)
    return f"{stem}.{suffix}" if suffix else stem


def open_upload(path: str, label: str, cap: int, meta: dict | None = None) -> UploadSource:
    """Open one already-resolved path for upload, or refuse it, sending nothing."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise WordPressError(f"Could not open {label}: {exc.strerror}.",
                             "validation_error") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise WordPressError(f"{label} is not a regular file.", "validation_error")
        if info.st_size == 0:
            raise WordPressError(f"{label} is empty.", "validation_error")
        if info.st_size > cap:
            raise WordPressError(
                f"{label} is {info.st_size // (1024 * 1024)} MiB, over the "
                f"{cap // (1024 * 1024)} MiB upload limit ([wordpress] max_upload_mb).",
                "validation_error",
            )
        head = os.pread(fd, _HEAD_BYTES, 0)
        mime = sniff_decodable(head)
        if mime is None and Path(path).suffix.lower() in _IMAGE_SUFFIXES:
            raise WordPressError(
                f"{label} is named like an image but its bytes are not a PNG, JPEG, "
                f"GIF, WebP or HEIF image.",
                "validation_error",
            )
    except BaseException:
        os.close(fd)
        raise
    return UploadSource(path=path, fd=fd, size=info.st_size,
                        mime=mime or "application/octet-stream",
                        filename=safe_filename(path), meta=dict(meta or {}))


def _kept(item: dict, key: str, wanted: str) -> bool:
    got = item.get(key)
    return (raw_text(got) if key != "alt_text" else (got if isinstance(got, str) else "")) == wanted


def upload(ctx, source: UploadSource) -> dict:
    """Send one file. The attachment as WordPress answered, after any follow-up.

    The result carries ``metadata_error`` when the follow-up failed and
    ``metadata_kept`` naming what the create alone kept, for §14.4.
    """
    data = source.read()
    meta = {k: v for k, v in source.meta.items() if k in _META_FIELDS and v is not None}
    stem = source.filename.rsplit(".", 1)[0]
    hint = lookup(ctx, "media", "list", "--search", meta.get("title") or stem)
    try:
        item, _ = ctx.client.request(
            "POST", MEDIA_ROUTE, params=meta or None, content=data,
            headers={"Content-Type": source.mime,
                     "Content-Disposition": f'attachment; filename="{source.filename}"'},
            base=ctx.base, idempotent=False, timeout=UPLOAD_TIMEOUT,
            definite_codes=_DEFINITE_CODES,
        )
        media_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(media_id, int):
            raise WordPressError(
                "The site accepted the upload and returned no attachment id; it was "
                "probably stored. Look it up before uploading again.",
                "outcome_unknown",
            )
    except WordPressError as exc:
        if exc.reason == "outcome_unknown":
            exc.extra["lookup"] = hint
        elif exc.extra.get("wp_code") in _DEFINITE_CODES:
            exc.reason = "request_refused"
        raise
    missing = {k: v for k, v in meta.items() if not _kept(item, k, v)}
    result = dict(item)
    result["metadata_kept"] = sorted(k for k in meta if k not in missing)
    if missing:
        try:
            after, _ = ctx.client.request("POST", f"{MEDIA_ROUTE}/{media_id}", json=missing,
                                          base=ctx.base, idempotent=True)
            if isinstance(after, dict):
                result.update(after)
        except WordPressError as exc:
            result["metadata_error"] = exc.reason
    return result


def upload_all(ctx, sources: list[UploadSource]) -> dict[int, dict]:
    """Upload every source, in order: ``{index: attachment}``.

    A failure part-way raises with the uploads already made listed under
    ``uploaded``, so nothing that follows (the post write) runs and the agent
    can reuse or delete what was stored.
    """
    done: dict[int, dict] = {}
    for index, source in enumerate(sources):
        try:
            done[index] = upload(ctx, source)
        except WordPressError as exc:
            if done:
                exc.extra["uploaded"] = [
                    {"path": sources[i].path, "id": item.get("id")} for i, item in done.items()
                ]
            exc.extra.setdefault("failed_path", source.path)
            raise
    return done


def upload_report(source: UploadSource, item: dict) -> dict:
    out = {
        "path": source.path,
        "id": item.get("id"),
        "mime_type": item.get("mime_type"),
        "source_url": fence(item.get("source_url")),
        "metadata_kept": item.get("metadata_kept", []),
    }
    if "metadata_error" in item:
        out["metadata_error"] = item["metadata_error"]
    return out


# --------------------------------------------------------------------------- #
# The verbs
# --------------------------------------------------------------------------- #


def _meta_args(args) -> dict:
    return {"title": args.title, "alt_text": args.alt, "caption": args.caption}


def check_upload(args) -> None:
    source = open_upload(args.file, "--file", upload_cap(args.config), _meta_args(args))
    args.closers.append(source.close)
    args.upload_source = source


def cmd_media_upload(args) -> dict:
    ctx = args.wp
    source = args.upload_source
    item = upload(ctx, source)
    report = upload_report(source, item)
    return {"status": "ok", **ctx.envelope(), "uploaded": True,
            "item": project_media(item), **{k: report[k] for k in report
                                            if k in ("metadata_kept", "metadata_error")}}


def check_media_update(args) -> None:
    if not any(v is not None for v in _meta_args(args).values()):
        raise WordPressError("Nothing to update: pass --title, --alt or --caption.",
                             "validation_error")


def cmd_media_update(args) -> dict:
    ctx = args.wp
    payload = {k: v for k, v in _meta_args(args).items() if v is not None}
    item, _ = ctx.client.request("POST", f"{MEDIA_ROUTE}/{args.id}", json=payload,
                                 base=ctx.base, idempotent=True)
    try:
        after, _ = ctx.client.get(f"{MEDIA_ROUTE}/{args.id}", params={"context": "edit"},
                                  base=ctx.base)
    except WordPressError as exc:
        return {"status": "ok", **ctx.envelope(), "updated": True,
                "item": project_media(item if isinstance(item, dict) else {"id": args.id}),
                "readback": {"available": False, "reason": exc.reason}}
    after = after if isinstance(after, dict) else {}
    changed = sorted(k for k, v in payload.items() if not _kept(after, k, v))
    return {"status": "ok", **ctx.envelope(), "updated": True, "item": project_media(after),
            "readback": {"dropped": [], "changed": changed, "notes": (
                ["WordPress saved different text than was sent; markup may have been "
                 "filtered."] if changed else [])}}
