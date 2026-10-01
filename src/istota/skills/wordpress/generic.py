"""Generic routes and abilities: `rest GET` and `abilities list` (stage 1).

`rest` reaches what the dedicated verbs do not: plugin namespaces, menus,
revisions, autosaves. The route is relative to the site's ``/wp-json/`` and may
not name a scheme, a host, a dot segment, a query, a fragment or a
percent-escape, so it cannot leave the site; queries go in ``--query``. Writes
through `rest` arrive with the confirmation gate in stage 4. The body comes back
with every string fenced, since the skill cannot know which of a plugin's fields
are the site's words.
"""

from __future__ import annotations

import re

from .client import WordPressError, fence, fence_tree, selector
from .common import total_header
from .discovery import ABILITIES_NAMESPACE

_ROUTE_SEGMENT_RE = re.compile(r"\A[A-Za-z0-9._~!$&'()*+,;=:@-]+\Z")

#: Route segments the skill never reaches: application passwords are a
#: credential path the model has no reason to list (spec §5.4).
_REFUSED_SEGMENTS = frozenset({"application-passwords"})


def check_route(route: str) -> str:
    """A route that stays under ``/wp-json/`` on the same site, or a refusal."""
    value = route.strip()
    if "://" in value or value.startswith("//") or "\\" in value:
        raise WordPressError("The route may not name a scheme or a host.", "validation_error")
    if "?" in value or "#" in value:
        raise WordPressError("Put query parameters in --query, not in the route.",
                             "validation_error")
    if "%" in value:
        raise WordPressError("The route may not be percent-encoded.", "validation_error")
    segments = [s for s in value.strip("/").split("/") if s]
    for segment in segments:
        if segment in (".", "..") or not _ROUTE_SEGMENT_RE.fullmatch(segment):
            raise WordPressError(f"Route segment {segment!r} is not allowed.",
                                 "validation_error")
        if segment.lower() in _REFUSED_SEGMENTS:
            raise WordPressError("Application passwords are not reachable through this skill.",
                                 "validation_error")
    return "/".join(segments)


def _php_name(key: str) -> str:
    """A query key as PHP files it in ``$_GET``: leading spaces dropped, and
    space, ``.`` and ``[`` turned into ``_``. WordPress reads ``_method`` from
    there, so ``.method`` reaches it as ``_method`` too."""
    name = key.lstrip(" ")
    for char in (" ", ".", "["):
        name = name.replace(char, "_")
    return name


def _query(pairs: list[str] | None) -> list[tuple[str, str]]:
    out = []
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise WordPressError(f"--query takes K=V, got {pair!r}.", "validation_error")
        # WordPress honours `_method` on any request, so a GET carrying
        # `_method=DELETE` would be a delete. `rest` reads only, for now.
        if _php_name(key).lower().startswith("_method"):
            raise WordPressError("--query may not override the HTTP method (_method).",
                                 "validation_error")
        out.append((key.strip(), value))
    return out


def prepare_rest(args) -> None:
    """Check the route and the query before the vault fetch is spent."""
    check_route(args.route)
    _query(args.query)


def cmd_rest(args) -> dict:
    ctx = args.wp
    route = check_route(args.route)
    # Never retried (spec §9.1): the skill cannot know what a plugin's route
    # does, even on GET.
    body, headers = ctx.client.request(args.method, route, params=_query(args.query) or None,
                                       base=ctx.base, idempotent=False)
    return {
        "status": "ok",
        **ctx.envelope(),
        "method": args.method,
        "route": route,
        "total": total_header(headers, "X-WP-Total"),
        "body": fence_tree(body),
    }


def _annotations(ability: dict) -> dict:
    meta = ability.get("meta") if isinstance(ability.get("meta"), dict) else {}
    found = ability.get("annotations") or meta.get("annotations") or {}
    return found if isinstance(found, dict) else {}


def cmd_abilities_list(args) -> dict:
    ctx = args.wp
    params: dict = {"per_page": 100}
    if args.category:
        params["category"] = args.category
    try:
        items, _ = ctx.client.get(f"{ABILITIES_NAMESPACE}/abilities", params=params,
                                  base=ctx.base)
    except WordPressError as exc:
        if exc.reason == "unknown_route":
            raise WordPressError(
                "This site has no Abilities API (WordPress 6.9 or later).",
                "unknown_route",
            ) from None
        raise
    rows = []
    for ability in items or []:
        if not isinstance(ability, dict):
            continue
        notes = _annotations(ability)
        rows.append({
            "name": selector(ability.get("name")),
            "category": selector(ability.get("category")),
            "label": fence(ability.get("label")),
            "description": fence(ability.get("description")),
            "readonly": notes.get("readonly") is True,
            "destructive": notes.get("destructive") is True,
        })
    return {"status": "ok", **ctx.envelope(), "count": len(rows), "items": rows}
