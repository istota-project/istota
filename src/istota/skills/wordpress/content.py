"""Content: `list`, `get` and `terms list`, and the writes `create`, `update`,
`delete` and `publish`.

Reads use ``context=edit``, so ``title``, ``content`` and ``excerpt`` come back
as ``raw`` — the block markup an editor saved — and ACF as its stored shape.
The default ``view`` context returns rendered HTML with the block comments
gone, and an agent that edited that and wrote it back would destroy the post's
Gutenberg markup.

Every string the site authored is fenced; ids, slugs, statuses, dates and field
names are not, since the model has to echo them back exactly.

**Writes.** Three rules hold for every write verb (spec §5.2, §5.7, §6.3, §9.1):

- *The gate.* Anything public or hard to undo refuses without ``--confirmed``,
  with ``reason: confirmation_required`` and a description of exactly what
  would happen, and sends nothing but ``GET`` first. All the gated actions of
  one call are collected into one description, so the agreement the user gives
  covers everything ``--confirmed`` will then do. Editing a post that is
  already public is gated too: on a live site the edit is the publication.
- *One send.* A create (a post or a term) or a delete is never retried, and an
  ambiguous ending is ``outcome_unknown`` with the lookup to run. An update to
  an existing id is retried once, since sending it twice leaves one state.
- *Read-back.* Every write is followed by a ``context=edit`` read of the item,
  and each field that was sent and did not land as sent is named under
  ``readback.dropped`` or ``readback.changed``: kses filtering, unregistered
  meta, a suffixed slug. The write still succeeded.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from istota.skill_host_paths import write_resolved

from . import acf, media
from .client import WordPressError, fence, fence_tree, raw_text, selectors
from .common import (  # noqa: F401 (MAX_LIMIT, limit_arg, total_header re-exported)
    MAX_LIMIT,
    int_or_none,
    limit_arg,
    lookup,
    read_text_file,
    total_header,
)
from .discovery import routes, taxonomy_fields, taxonomy_route, type_route

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


def _term_ids(ctx, taxonomy: str, names: str | list[str], *,
              missing: list[str] | None = None) -> list[int]:
    """Names or ids (comma-separated, or a list) to term ids.

    A name that matches nothing is an error, unless `missing` is given, in
    which case it is appended there and the caller decides.
    """
    ids: list[int] = []
    route = None
    for raw in names.split(",") if isinstance(names, str) else names:
        name = raw.strip()
        if not name:
            continue
        number = int_or_none(name)
        if number is not None:
            ids.append(number)
            continue
        route = route or taxonomy_route(ctx, taxonomy)
        match = _find_term(ctx, route, name)
        if match is None and missing is not None:
            missing.append(name)
            continue
        if match is None:
            raise WordPressError(
                f"No {taxonomy} term named {fence(name)}.", "unknown_term",
            )
        ids.append(match)
    return ids


def _find_term(ctx, route: str, name: str) -> int | None:
    """The id of the term whose name or slug is `name`, case-insensitively."""
    found, _ = ctx.client.get(route, params={"search": name, "per_page": MAX_LIMIT},
                              base=ctx.base)
    wanted = name.casefold()
    for term in found or []:
        if isinstance(term, dict) and (
            html.unescape(str(term.get("name", ""))).casefold() == wanted
            or str(term.get("slug", "")).casefold() == wanted
        ):
            return term.get("id")
    return None


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


def check_terms_create(args) -> None:
    if not args.name.strip():
        raise WordPressError("--name is empty.", "validation_error")
    if args.parent is not None and args.parent < 0:
        raise WordPressError("--parent is a term id.", "validation_error")


def cmd_terms_create(args) -> dict:
    """Create one term, unless one of that name or slug exists.

    Gated like ``--create-terms`` (spec §5.7): a term is public as soon as it
    exists, and an explicit verb must not be the way around that gate. A term
    already there is returned with ``"created": false`` and no gate, which also
    makes a retry after ``outcome_unknown`` safe.
    """
    ctx = args.wp
    route = taxonomy_route(ctx, args.taxonomy)
    name = args.name.strip()
    existing = _find_term(ctx, route, name)
    if existing is None and args.slug:
        existing = _find_term(ctx, route, args.slug)
    if existing is not None:
        return {"status": "ok", **ctx.envelope(), "taxonomy": args.taxonomy,
                "created": False, "id": existing}
    gate(args, ctx, _terms_actions({args.taxonomy: [name]}))
    body: dict = {"name": name}
    if args.parent is not None:
        body["parent"] = args.parent
    if args.slug:
        body["slug"] = args.slug
    term = post_term(ctx, route, args.taxonomy, body)
    return {"status": "ok", **ctx.envelope(), "taxonomy": args.taxonomy,
            "created": True, "item": project_term(term)}


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #

CREATE_STATUSES = ("draft", "pending")
UPDATE_STATUSES = ("draft", "pending", "publish", "future", "private")
#: Statuses a reader outside the editing team can see, or will on a date.
LIVE_STATUSES = frozenset({"publish", "future", "private"})
MAX_CONTENT_BYTES = 8 * 1024 * 1024
MAX_META_BYTES = 1024 * 1024
_TEXT_FIELDS = ("title", "content", "excerpt")
_TITLE_IN_DESCRIPTION = 80


def parse_date(value: str) -> tuple[str, str]:
    """``("date", local)`` for a naive time, ``("date_gmt", utc)`` for one with an offset.

    WordPress keeps ``date`` in the site's own timezone and ``date_gmt`` in UTC,
    so an offset is converted to UTC rather than guessed into site time.
    """
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        raise WordPressError(
            f"--date takes ISO 8601, such as 2026-10-01T09:00 or 2026-10-01T09:00+02:00; "
            f"got {value!r}.",
            "validation_error",
        ) from None
    if moment.tzinfo is None:
        return "date", moment.replace(microsecond=0).isoformat()
    utc = moment.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)
    return "date_gmt", utc.isoformat()


def parse_terms(pairs: list[str] | None) -> dict[str, list[str]]:
    """``--terms TAXONOMY=a,b`` flags to ``{taxonomy: [names or ids]}``.

    The same taxonomy named twice is merged. An empty list (``category=``)
    clears the post's terms in that taxonomy.
    """
    out: dict[str, list[str]] = {}
    for pair in pairs or []:
        taxonomy, sep, names = pair.partition("=")
        taxonomy = taxonomy.strip()
        if not sep or not taxonomy:
            raise WordPressError(f"--terms takes TAXONOMY=name1,name2; got {pair!r}.",
                                 "validation_error")
        wanted = out.setdefault(taxonomy, [])
        for name in names.split(","):
            name = name.strip()
            if name and name not in wanted:
                wanted.append(name)
    return out


def _check_fields(args) -> None:
    """The local half of a create or update: every flag parsed, every file read.

    Runs before the vault fetch, and leaves what it read on `args`.
    """
    args.date_field = parse_date(args.date) if args.date else None
    args.term_names = parse_terms(args.terms)
    args.body_text = None
    if args.content_file:
        args.body_text = read_text_file(args.content_file, "--content-file", MAX_CONTENT_BYTES)
    elif args.content is not None:
        args.body_text = args.content
    args.meta = None
    if args.meta_file:
        try:
            meta = json.loads(read_text_file(args.meta_file, "--meta-file", MAX_META_BYTES))
        except ValueError:
            raise WordPressError("--meta-file is not JSON.", "validation_error") from None
        if not isinstance(meta, dict):
            raise WordPressError("--meta-file must hold a JSON object of meta keys.",
                                 "validation_error")
        args.meta = meta
    if args.featured_media_id is not None and args.featured_media_id < 0:
        raise WordPressError("--featured-media-id is an attachment id, or 0 for none.",
                             "validation_error")
    # Last, so a flag that fails above has opened no file.
    acf.check(args, media.upload_cap(args.config))


def check_create(args) -> None:
    if args.if_absent and not args.slug:
        raise WordPressError("--if-absent needs --slug: the slug is how a retry finds the post.",
                             "validation_error")
    _check_fields(args)


def check_update(args) -> None:
    _check_fields(args)
    if args.status == "future" and not args.date_field:
        raise WordPressError("--status future needs --date: the time to publish at.",
                             "validation_error")
    if (not build_payload(args) and not args.term_names and not args.acf_values
            and args.featured_slot is None):
        raise WordPressError("Nothing to update: pass at least one field to change.",
                             "validation_error")


def check_publish(args) -> None:
    args.date_field = parse_date(args.date) if args.date else None


def build_payload(args) -> dict:
    """The post fields the flags name, without terms."""
    payload: dict = {}
    for key, value in (
        ("title", args.title),
        ("content", args.body_text),
        ("excerpt", args.excerpt),
        ("slug", args.slug),
        ("status", args.status),
        ("password", args.password),
        ("featured_media", args.featured_media_id),
        ("meta", args.meta),
    ):
        if value is not None:
            payload[key] = value
    if args.date_field:
        key, value = args.date_field
        payload[key] = value
    return payload


def _where(ctx) -> str:
    return f"{ctx.record.name}/{ctx.blog}" if ctx.blog else ctx.record.name


def _label(item: dict, type_slug: str, post_id: int) -> str:
    title = raw_text(item.get("title"))
    if len(title) > _TITLE_IN_DESCRIPTION:
        title = title[:_TITLE_IN_DESCRIPTION] + "..."
    return f'"{fence(title)}" ({type_slug} #{post_id})'


def _when(args) -> str:
    """The date clause of a publish or schedule, so the user sees when it goes live.

    WordPress publishes at once a post scheduled for a time already past, so
    the clause says that rather than letting "future" read as "not yet".
    """
    if not args.date_field:
        return ", now"
    key, value = args.date_field
    zone = " UTC" if key == "date_gmt" else " site time"
    return f", dated {value}{zone} (at once if that time has passed)"


def gate(args, ctx, actions: list[str]) -> None:
    """Refuse unless confirmed, naming every gated action of this call."""
    if not actions or args.confirmed:
        return
    would = [f"would {action} on {_where(ctx)}" for action in actions]
    raise WordPressError(
        "Not done: this needs the user's agreement first. " + "; ".join(would) + ". "
        "Show the user exactly this, and pass --confirmed only after they agree in "
        "the conversation.",
        "confirmation_required",
        would=would,
    )


def _current(ctx, route: str, post_id: int) -> dict:
    item, _ = ctx.client.get(f"{route}/{post_id}", params={"context": "edit"}, base=ctx.base)
    if not isinstance(item, dict):
        raise WordPressError("The site answered with something that is not a post.",
                             "bad_response")
    return item


def _find_by_slug(ctx, route: str, slug: str) -> dict | None:
    found, _ = ctx.client.get(
        route, params={"slug": slug, "status": "any", "context": "edit"}, base=ctx.base,
    )
    wanted = {slug, wp_slug(slug)}
    for item in found or []:
        if isinstance(item, dict) and item.get("slug") in wanted:
            return item
    return None


def wp_slug(text: str) -> str:
    """The slug WordPress stores for `text`, as ``sanitize_title`` makes it.

    An approximation of core's rule (lowercase, non-ASCII percent-encoded,
    anything else but letters, digits, ``_`` and ``-`` dropped, spaces and dots
    to hyphens), good enough that ``--if-absent`` recognises its own earlier
    post when the slug it was given was not already clean.
    """
    value = text.strip().lower()
    value = "".join(c if ord(c) < 128 else quote(c, safe="").lower() for c in value)
    value = value.replace(".", "-")
    value = re.sub(r"[^%a-z0-9 _-]", "", value)
    value = re.sub(r"\s+", "-", value)
    return re.sub(r"-+", "-", value).strip("-")


def resolve_terms(ctx, type_slug: str, term_names: dict[str, list[str]], *,
                  create: bool) -> tuple[dict[str, list[int]], dict[str, list[str]]]:
    """``(ids found per taxonomy, names missing per taxonomy)``, by ``GET`` only.

    A missing name is an error unless `create`, since a typo must not become a
    category. Ids are accepted as they are.
    """
    table = routes(ctx)
    allowed = table["types"].get(type_slug, {}).get("taxonomies", [])
    ids: dict[str, list[int]] = {}
    missing: dict[str, list[str]] = {}
    for taxonomy, names in term_names.items():
        taxonomy_route(ctx, taxonomy)
        if taxonomy not in allowed:
            raise WordPressError(
                f"Taxonomy {taxonomy!r} does not apply to type {type_slug!r}.",
                "validation_error",
            )
        lost: list[str] = []
        ids[taxonomy] = _term_ids(ctx, taxonomy, names, missing=lost)
        if lost:
            missing[taxonomy] = lost
    if missing and not create:
        named = "; ".join(f"{t}: {', '.join(fence(n) for n in m)}" for t, m in missing.items())
        raise WordPressError(
            f"No such terms ({named}). Check the spelling with `terms list`, or pass "
            f"--create-terms to create them, which needs the user's agreement.",
            "unknown_term",
        )
    return ids, missing


def _terms_actions(missing: dict[str, list[str]]) -> list[str]:
    return [
        f"create {taxonomy} terms {', '.join(fence(n) for n in names)}"
        for taxonomy, names in missing.items()
    ]


def create_terms(ctx, missing: dict[str, list[str]]) -> dict[str, list[int]]:
    """Create each missing term once. A failure part-way names what was created."""
    created: dict[str, list[int]] = {}
    for taxonomy, names in missing.items():
        route = taxonomy_route(ctx, taxonomy)
        for name in names:
            try:
                term = post_term(ctx, route, taxonomy, {"name": name})
            except WordPressError as exc:
                if created:
                    exc.extra["created_terms"] = created
                raise
            created.setdefault(taxonomy, []).append(term["id"])
    return created


def post_term(ctx, route: str, taxonomy: str, body: dict) -> dict:
    """Create one term, sent once: the term WordPress answered, with an int id."""
    hint = lookup(ctx, "terms", "list", "--taxonomy", taxonomy, "--search", body["name"])
    try:
        term, _ = ctx.client.request("POST", route, json=body, base=ctx.base,
                                     idempotent=False)
        if not isinstance(term, dict) or not isinstance(term.get("id"), int):
            raise WordPressError(
                "The site accepted the term and returned no id; it was probably "
                "created. Look it up before trying again.",
                "outcome_unknown",
            )
    except WordPressError as exc:
        _lookup_hint(exc, hint)
        raise
    return term


def _apply_terms(ctx, payload: dict, ids: dict, created: dict) -> None:
    taxonomies = routes(ctx)["taxonomies"]
    for taxonomy in set(ids) | set(created):
        field = taxonomies[taxonomy]["rest_base"]
        payload[field] = ids.get(taxonomy, []) + created.get(taxonomy, [])


def _same_moment(got, sent: str) -> bool:
    try:
        return datetime.fromisoformat(str(got)) == datetime.fromisoformat(sent)
    except ValueError:
        return False


def readback(sent: dict, after: dict, term_fields: list[str]) -> dict:
    """What did not land as sent: ``{"dropped": [...], "changed": [...], "notes": [...]}``."""
    dropped: list[str] = []
    changed: list[str] = []
    for key, value in sent.items():
        if key == "meta":
            got = after.get("meta") if isinstance(after.get("meta"), dict) else {}
            for meta_key, meta_value in value.items():
                if meta_key not in got:
                    dropped.append(f"meta.{meta_key}")
                elif got[meta_key] != meta_value:
                    changed.append(f"meta.{meta_key}")
            continue
        if key == "acf":
            got = after.get("acf") if isinstance(after.get("acf"), dict) else {}
            for name, field_value in value.items():
                if name not in got:
                    dropped.append(f"acf.{name}")
                elif not acf.same(field_value, got[name]):
                    changed.append(f"acf.{name}")
            continue
        if key not in after:
            dropped.append(key)
            continue
        got = after.get(key)
        if key in _TEXT_FIELDS:
            same = raw_text(got) == value
        elif key in ("date", "date_gmt"):
            same = _same_moment(got, value)
        elif key in term_fields:
            same = isinstance(got, list) and sorted(got) == sorted(value)
        elif key == "slug":
            # Sanitising is not a change worth reporting; a suffix is.
            same = got in (value, wp_slug(value))
        else:
            same = got == value
        if not same:
            changed.append(key)
    notes = []
    if any(k in _TEXT_FIELDS for k in changed):
        notes.append("WordPress saved different text than was sent. An account without "
                     "the unfiltered_html capability has markup filtered; `get` shows "
                     "what was saved.")
    if any(k.startswith("meta.") for k in dropped):
        notes.append("WordPress ignores a meta key that is not registered with "
                     "show_in_rest.")
    if any(k.startswith("acf.") for k in dropped + changed):
        notes.append("An ACF field did not store as sent: WordPress may have rejected "
                     "or reformatted the value. `get --fields acf` shows what was saved.")
    if "slug" in changed:
        notes.append("WordPress gave the post a different slug; it adds a suffix when "
                     "the slug is taken.")
    if "status" in changed:
        notes.append("The saved status differs from the one sent; a publish dated in "
                     "the future is saved as future.")
    return {"dropped": selectors(dropped), "changed": selectors(changed), "notes": notes}


def _written(ctx, route: str, type_slug: str, post_id: int, written, payload: dict) -> dict:
    """The result of a post write: the item as re-read, and what did not land."""
    term_fields = taxonomy_fields(ctx, type_slug)
    try:
        after = _current(ctx, route, post_id)
    except WordPressError as exc:
        item = written if isinstance(written, dict) else {"id": post_id}
        return {
            "item": project_post(item, term_fields, full=False),
            "readback": {"available": False, "reason": exc.reason},
        }
    return {
        "item": project_post(after, term_fields, full=False),
        "readback": readback(payload, after, term_fields),
    }


def _lookup_hint(exc: WordPressError, hint: str) -> None:
    if exc.reason == "outcome_unknown":
        exc.extra["lookup"] = hint


def _post_id(item, verb: str) -> int:
    post_id = item.get("id") if isinstance(item, dict) else None
    if not isinstance(post_id, int):
        raise WordPressError(
            f"The site accepted the {verb} and returned no post id; it probably "
            f"applied. Look it up before trying again.",
            "outcome_unknown",
        )
    return post_id


def _uploaded(report: list[dict]) -> list[dict]:
    return [{"path": row["path"], "id": row["id"]} for row in report]


def _prepare_write(ctx, args, payload: dict, ids: dict, missing: dict) -> tuple[list, dict]:
    """After the gate and before the post write: the uploads, then any term
    creates, then the payload filled with what they returned.

    Every upload of the call happens before the post is written (spec §6.2),
    so a failed upload leaves no post pointing at a missing attachment. A
    failure from here on names what was already stored.
    """
    upload_ids, report = acf.run_uploads(ctx, args.uploads)
    try:
        created = create_terms(ctx, missing)
    except WordPressError as exc:
        if report:
            exc.extra["uploaded"] = _uploaded(report)
        raise
    _apply_terms(ctx, payload, ids, created)
    if args.featured_slot is not None:
        payload["featured_media"] = upload_ids[args.featured_slot.index]
    if args.acf_values:
        payload["acf"] = acf.fill(args.acf_values, upload_ids)
    return report, created


def _write_failed(exc: WordPressError, report: list, created: dict) -> None:
    if created:
        exc.extra["created_terms"] = created
    if report:
        exc.extra["uploaded"] = _uploaded(report)


def _extras(out: dict, report: list, created: dict) -> dict:
    if report:
        out["uploads"] = report
    if created:
        out["created_terms"] = created
    return out


def cmd_create(args) -> dict:
    ctx = args.wp
    route = type_route(ctx, args.type)
    if args.if_absent:
        existing = _find_by_slug(ctx, route, args.slug)
        if existing is not None:
            return {
                "status": "ok",
                **ctx.envelope(),
                "created": False,
                "item": project_post(existing, taxonomy_fields(ctx, args.type), full=False),
            }
    payload = build_payload(args)
    ids, missing = resolve_terms(ctx, args.type, args.term_names, create=args.create_terms)
    acf.check_schema(ctx, args.type, args.acf_values)
    gate(args, ctx, _terms_actions(missing))
    report, created_terms = _prepare_write(ctx, args, payload, ids, missing)
    if args.slug:
        hint = lookup(ctx, "list", "--type", args.type, "--slug", args.slug, "--status", "any")
    else:
        hint = lookup(ctx, "list", "--type", args.type, "--search", args.title,
                      "--status", "any")
    try:
        item, _ = ctx.client.request("POST", route, json=payload, base=ctx.base,
                                     idempotent=False)
        post_id = _post_id(item, "create")
    except WordPressError as exc:
        _lookup_hint(exc, hint)
        _write_failed(exc, report, created_terms)
        raise
    out = {"status": "ok", **ctx.envelope(), "created": True,
           **_written(ctx, route, args.type, post_id, item, payload)}
    return _extras(out, report, created_terms)


def cmd_update(args) -> dict:
    ctx = args.wp
    route = type_route(ctx, args.type)
    current = _current(ctx, route, args.id)
    payload = build_payload(args)
    ids, missing = resolve_terms(ctx, args.type, args.term_names, create=args.create_terms)
    acf.check_schema(ctx, args.type, args.acf_values)
    label = _label(current, args.type, args.id)
    actions = []
    status_now = current.get("status")
    if status_now in LIVE_STATUSES:
        fields = set(payload) | set(args.term_names)
        if args.featured_slot is not None:
            fields.add("featured_media")
        fields |= {f"acf.{name}" for name in args.acf_values or {}}
        actions.append(f"change {', '.join(sorted(fields))} of {label}, which is live "
                       f"({status_now})")
    status_new = payload.get("status")
    if status_new in LIVE_STATUSES and status_new != status_now:
        actions.append(f"make {label} {status_new}{_when(args)}")
    actions += _terms_actions(missing)
    gate(args, ctx, actions)
    report, created_terms = _prepare_write(ctx, args, payload, ids, missing)
    try:
        item, _ = ctx.client.request("POST", f"{route}/{args.id}", json=payload,
                                     base=ctx.base, idempotent=True)
    except WordPressError as exc:
        _write_failed(exc, report, created_terms)
        raise
    out = {"status": "ok", **ctx.envelope(), "updated": True,
           **_written(ctx, route, args.type, args.id, item, payload)}
    return _extras(out, report, created_terms)


def cmd_publish(args) -> dict:
    ctx = args.wp
    route = type_route(ctx, args.type)
    current = _current(ctx, route, args.id)
    payload: dict = {"status": "publish"}
    if args.date_field:
        key, value = args.date_field
        payload[key] = value
    gate(args, ctx, [f"publish {_label(current, args.type, args.id)}{_when(args)}"])
    item, _ = ctx.client.request("POST", f"{route}/{args.id}", json=payload,
                                 base=ctx.base, idempotent=True)
    return {"status": "ok", **ctx.envelope(), "published": True,
            **_written(ctx, route, args.type, args.id, item, payload)}


def cmd_delete(args) -> dict:
    """Trash a post, or with ``--force --confirmed`` delete it for good."""
    ctx = args.wp
    route = type_route(ctx, args.type)
    if args.force:
        current = _current(ctx, route, args.id)
        gate(args, ctx, [f"permanently delete {_label(current, args.type, args.id)}, "
                         f"skipping the trash"])
    try:
        ctx.client.request("DELETE", f"{route}/{args.id}",
                           params={"force": "true"} if args.force else None,
                           base=ctx.base, idempotent=False)
    except WordPressError as exc:
        if exc.extra.get("wp_code") == "rest_trash_not_supported":
            raise WordPressError(
                f"Type {args.type!r} has no trash, so it can only be deleted for good: "
                f"--force --confirmed, after the user agrees.",
                "request_refused", **exc.extra,
            ) from None
        _lookup_hint(exc, lookup(ctx, "get", "--id", str(args.id), "--type", args.type))
        raise
    return {"status": "ok", **ctx.envelope(), "id": args.id, "type": args.type,
            "trashed": not args.force, "deleted": bool(args.force)}
