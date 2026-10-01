"""Reading content: `list`, `get` and `terms list`.

Reads use ``context=edit``, so ``title``, ``content`` and ``excerpt`` come back
as ``raw`` — the block markup an editor saved — and ACF as its stored shape.
The default ``view`` context returns rendered HTML with the block comments
gone, and an agent that edited that and wrote it back would destroy the post's
Gutenberg markup.

Every string the site authored is fenced; ids, slugs, statuses, dates and field
names are not, since the model has to echo them back exactly.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from istota.skill_host_paths import write_resolved

from .client import WordPressError, fence, fence_tree, raw_text
from .discovery import taxonomy_fields, taxonomy_route, type_route

MAX_LIMIT = 100
LIST_STATUSES = ("any", "publish", "draft", "pending", "private", "future")

#: An ACF flexible-content row names its layout here; the value is a selector.
_ACF_SELECTORS = frozenset({"acf_fc_layout"})

#: Top-level fields `get` can return, for `--fields`.
GET_FIELDS = (
    "type", "slug", "post_status", "date", "date_gmt", "modified", "author", "parent",
    "menu_order", "link", "title", "content", "excerpt", "featured_media", "template",
    "format", "sticky", "comment_status", "ping_status", "password_protected",
    "terms", "meta", "acf",
)


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _terms_of(item: dict, term_fields: list[str]) -> dict:
    return {
        field: item[field]
        for field in term_fields
        if isinstance(item.get(field), list)
    }


def project_post(item: dict, term_fields: list[str], *, full: bool) -> dict:
    """One post as the model sees it: selectors bare, the site's words fenced."""
    out = {
        "id": item.get("id"),
        "type": item.get("type"),
        "slug": item.get("slug"),
        "post_status": item.get("status"),
        "date": item.get("date"),
        "modified": item.get("modified"),
        "author": item.get("author"),
        "link": fence(item.get("link")),
        "title": fence(raw_text(item.get("title"))),
        "featured_media": item.get("featured_media"),
        "terms": _terms_of(item, term_fields),
    }
    if "parent" in item:
        out["parent"] = item.get("parent")
    if not full:
        return out
    out.update({
        "date_gmt": item.get("date_gmt"),
        "menu_order": item.get("menu_order"),
        "content": fence(raw_text(item.get("content"))),
        "excerpt": fence(raw_text(item.get("excerpt"))),
        "template": item.get("template"),
        "format": item.get("format"),
        "sticky": item.get("sticky"),
        "comment_status": item.get("comment_status"),
        "ping_status": item.get("ping_status"),
        # The password itself is not echoed: a protected post's readers are
        # the site's business, and nothing in a read needs the value.
        "password_protected": bool(item.get("password")),
        "meta": fence_tree(item.get("meta")) if item.get("meta") else {},
        "acf": fence_tree(item.get("acf"), keep=_ACF_SELECTORS) if "acf" in item else None,
    })
    return out


def limit_arg(value: int | None, default: int) -> int:
    if value is None:
        return default
    if value < 1 or value > MAX_LIMIT:
        raise WordPressError(f"--limit must be between 1 and {MAX_LIMIT}.", "validation_error")
    return value


def _term_ids(ctx, taxonomy: str, names: str) -> list[int]:
    """Comma-separated names or ids to term ids. A name that matches nothing is an error."""
    ids: list[int] = []
    route = None
    for raw in names.split(","):
        name = raw.strip()
        if not name:
            continue
        number = _int_or_none(name)
        if number is not None:
            ids.append(number)
            continue
        route = route or taxonomy_route(ctx, taxonomy)
        found, _ = ctx.client.get(route, params={"search": name, "per_page": MAX_LIMIT},
                                  base=ctx.base)
        wanted = name.casefold()
        match = [
            t.get("id") for t in found or []
            if isinstance(t, dict) and (
                html.unescape(str(t.get("name", ""))).casefold() == wanted
                or str(t.get("slug", "")).casefold() == wanted
            )
        ]
        if not match:
            raise WordPressError(
                f"No {taxonomy} term named {fence(name)}.", "unknown_term",
            )
        ids.append(match[0])
    return ids


def total_header(headers, name: str) -> int | None:
    return _int_or_none(headers.get(name)) if headers is not None else None


def cmd_list(args) -> dict:
    ctx = args.wp
    route = type_route(ctx, args.type)
    params: dict = {
        "context": "edit",
        "status": args.status,
        "per_page": limit_arg(args.limit, 20),
        "page": args.page,
    }
    if args.search:
        params["search"] = args.search
    if args.slug:
        params["slug"] = args.slug
    if args.category:
        params["categories"] = ",".join(str(i) for i in _term_ids(ctx, "category", args.category))
    if args.tag:
        params["tags"] = ",".join(str(i) for i in _term_ids(ctx, "post_tag", args.tag))
    items, headers = ctx.client.get(route, params=params, base=ctx.base)
    term_fields = taxonomy_fields(ctx, args.type)
    rows = [project_post(i, term_fields, full=False) for i in items or [] if isinstance(i, dict)]
    return {
        "status": "ok",
        **ctx.envelope(),
        "type": args.type,
        "page": args.page,
        "count": len(rows),
        "total": total_header(headers, "X-WP-Total"),
        "total_pages": total_header(headers, "X-WP-TotalPages"),
        "items": rows,
    }


def wanted_fields(fields: str | None) -> list[str]:
    wanted = [f.strip() for f in (fields or "").split(",") if f.strip()]
    unknown = sorted(set(wanted) - set(GET_FIELDS))
    if unknown:
        raise WordPressError(
            f"Unknown --fields {', '.join(unknown)}; choose from {', '.join(GET_FIELDS)}.",
            "validation_error",
        )
    return wanted


def _narrow(item: dict, fields: str | None) -> dict:
    wanted = wanted_fields(fields)
    if not wanted:
        return item
    return {"id": item.get("id"), **{f: item.get(f) for f in wanted}}


def check_paging(args) -> None:
    """The local checks a listing verb can fail before any fetch is spent."""
    limit_arg(args.limit, 1)
    if args.page < 1:
        raise WordPressError("--page starts at 1.", "validation_error")


def check_get(args) -> None:
    wanted_fields(args.fields)


def cmd_get(args) -> dict:
    ctx = args.wp
    route = type_route(ctx, args.type)
    item, _ = ctx.client.get(f"{route}/{args.id}", params={"context": "edit"}, base=ctx.base)
    if not isinstance(item, dict):
        raise WordPressError("The site answered with something that is not a post.",
                             "bad_response")
    projected = _narrow(project_post(item, taxonomy_fields(ctx, args.type), full=True),
                        args.fields)
    if args.output is None:
        return {"status": "ok", **ctx.envelope(), "item": projected}
    data = json.dumps(projected, indent=2, ensure_ascii=False).encode()
    write_resolved(Path(args.output), data)
    acf = projected.get("acf")
    return {
        "status": "ok",
        **ctx.envelope(),
        "item": {
            "id": projected.get("id"),
            "type": item.get("type"),
            "slug": item.get("slug"),
            "post_status": item.get("status"),
            "title": fence(raw_text(item.get("title"))),
        },
        "written_to": str(args.output),
        "bytes": len(data),
        "fields": sorted(projected),
        "acf_fields": sorted(acf) if isinstance(acf, dict) else None,
    }


def project_term(term: dict) -> dict:
    return {
        "id": term.get("id"),
        "taxonomy": term.get("taxonomy"),
        "slug": term.get("slug"),
        "name": fence(term.get("name")),
        "description": fence(term.get("description")),
        "parent": term.get("parent"),
        "count": term.get("count"),
    }


def cmd_terms_list(args) -> dict:
    ctx = args.wp
    route = taxonomy_route(ctx, args.taxonomy)
    params: dict = {"per_page": limit_arg(args.limit, MAX_LIMIT), "page": args.page}
    if args.search:
        params["search"] = args.search
    items, headers = ctx.client.get(route, params=params, base=ctx.base)
    rows = [project_term(t) for t in items or [] if isinstance(t, dict)]
    return {
        "status": "ok",
        **ctx.envelope(),
        "taxonomy": args.taxonomy,
        "count": len(rows),
        "total": total_header(headers, "X-WP-Total"),
        "items": rows,
    }
