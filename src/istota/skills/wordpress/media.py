"""Media: `media list` (stage 1). Uploads and metadata writes come in stage 3."""

from __future__ import annotations

from .client import fence, raw_text
from .content import limit_arg, total_header


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
    items, headers = ctx.client.get("wp/v2/media", params=params, base=ctx.base)
    rows = [project_media(i) for i in items or [] if isinstance(i, dict)]
    return {
        "status": "ok",
        **ctx.envelope(),
        "count": len(rows),
        "total": total_header(headers, "X-WP-Total"),
        "items": rows,
    }
