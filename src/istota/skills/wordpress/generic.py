"""Generic routes and abilities: `rest` and `abilities list|run`.

`rest` reaches what the dedicated verbs do not: plugin namespaces, menus,
revisions, autosaves. The route is relative to the site's ``/wp-json/`` and may
not name a scheme, a host, a dot segment, a query, a fragment or a
percent-escape, so it cannot leave the site; queries go in ``--query``, where
``_method`` is refused for every method, since WordPress would act on it in
place of the method the user agreed to. Every method but ``GET`` is gated
(spec §5.6): the skill cannot know what a plugin's route does. No `rest` call is
retried, ``GET`` included, for the same reason. The body comes back with every
string fenced, since the skill cannot know which of a plugin's fields are the
site's words.

`abilities run` runs one Abilities API ability (WordPress 6.9+). The ability is
read first, and its own annotations decide two things: the gate (only an
ability marked ``readonly`` runs without ``--confirmed``) and the method the
run endpoint expects (``GET`` for readonly, ``DELETE`` for destructive and
idempotent, ``POST`` otherwise, as core's run controller checks).
"""

from __future__ import annotations

import json
import re

from .client import WordPressError, fence, fence_tree, selector
from .common import gate, quoted, read_json_file, total_header
from .discovery import ABILITIES_NAMESPACE

_ROUTE_SEGMENT_RE = re.compile(r"\A[A-Za-z0-9._~!$&'()*+,;=:@-]+\Z")

#: Route segments the skill never reaches: application passwords are a
#: credential path the model has no reason to touch (spec §5.4).
_REFUSED_SEGMENTS = frozenset({"application-passwords"})

REST_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
MAX_BODY_BYTES = 8 * 1024 * 1024

#: ``namespace/name``, as the Abilities API registers one.
_ABILITY_RE = re.compile(r"\A[a-z0-9-]{1,64}/[a-z0-9-]{1,128}\Z")


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
        # `_method=DELETE` would be a delete, and a confirmed POST could be one.
        if _php_name(key).lower().startswith("_method"):
            raise WordPressError("--query may not override the HTTP method (_method).",
                                 "validation_error")
        out.append((key.strip(), value))
    return out


def prepare_rest(args) -> None:
    """Check the route, the query and the body before the vault fetch is spent."""
    check_route(args.route)
    _query(args.query)
    args.body = None
    if args.body_file:
        if args.method == "GET":
            raise WordPressError("A GET takes no --body-file; use --query.", "validation_error")
        args.body = read_json_file(args.body_file, "--body-file", MAX_BODY_BYTES)


def _describe_rest(method: str, route: str, query: list, body) -> str:
    parts = [f"send {method} {route}"]
    if query:
        parts.append("with query " + "&".join(f"{k}={v}" for k, v in query))
    if isinstance(body, dict):
        parts.append("with a body setting " + (", ".join(sorted(map(str, body))) or "nothing"))
    elif body is not None:
        parts.append(f"with a {len(json.dumps(body))}-byte JSON body")
    return " ".join(parts) + ", a route whose effect the skill cannot check"


def cmd_rest(args) -> dict:
    ctx = args.wp
    route = check_route(args.route)
    query = _query(args.query)
    if args.method != "GET":
        gate(args, ctx, [_describe_rest(args.method, route, query, args.body)])
    # Never retried (spec §9.1): the skill cannot know what a plugin's route
    # does, even on GET.
    body, headers = ctx.client.request(args.method, route, params=query or None,
                                       json=args.body, base=ctx.base, idempotent=False)
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


def _no_abilities(exc: WordPressError) -> WordPressError:
    if exc.reason == "unknown_route" and exc.extra.get("wp_code") == "rest_no_route":
        return WordPressError("This site has no Abilities API (WordPress 6.9 or later).",
                              "unknown_route")
    return exc


def cmd_abilities_list(args) -> dict:
    ctx = args.wp
    params: dict = {"per_page": 100}
    if args.category:
        params["category"] = args.category
    try:
        items, _ = ctx.client.get(f"{ABILITIES_NAMESPACE}/abilities", params=params,
                                  base=ctx.base)
    except WordPressError as exc:
        raise _no_abilities(exc) from None
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


def check_ability(args) -> None:
    if not _ABILITY_RE.fullmatch(args.name):
        raise WordPressError(f"An ability is named namespace/name, not {quoted(args.name)}.",
                             "validation_error")
    args.input = None
    if args.input_file:
        args.input = read_json_file(args.input_file, "--input-file", MAX_BODY_BYTES)


def php_query(prefix: str, value) -> list[tuple[str, str]]:
    """`value` as PHP's bracket syntax, which is how a GET carries ``input``.

    Booleans go as ``true``/``false`` and numbers as their digits; the
    ability's schema validation accepts both for those types. A null or an
    empty list or object has no spelling there and is left out.
    """
    if isinstance(value, dict):
        out = []
        for key, item in value.items():
            out += php_query(f"{prefix}[{key}]", item)
        return out
    if isinstance(value, list):
        out = []
        for index, item in enumerate(value):
            out += php_query(f"{prefix}[{index}]", item)
        return out
    if value is None:
        return []
    if isinstance(value, bool):
        return [(prefix, "true" if value else "false")]
    return [(prefix, str(value))]


def cmd_abilities_run(args) -> dict:
    ctx = args.wp
    try:
        ability, _ = ctx.client.get(f"{ABILITIES_NAMESPACE}/abilities/{args.name}",
                                    base=ctx.base)
    except WordPressError as exc:
        raise _no_abilities(exc) from None
    if not isinstance(ability, dict):
        raise WordPressError("The site answered with something that is not an ability.",
                             "bad_response")
    notes = _annotations(ability)
    readonly = notes.get("readonly") is True
    destructive = notes.get("destructive") is True
    if not readonly:
        kind = "destructive ability" if destructive else "ability"
        given = " with the given input" if args.input is not None else ""
        gate(args, ctx, [f"run the {kind} {args.name} "
                         f"({fence(ability.get('label')) or 'no label'}){given}"])
    route = f"{ABILITIES_NAMESPACE}/abilities/{args.name}/run"
    params = json_body = None
    if readonly:
        method = "GET"
        params = php_query("input", args.input) or None
    elif destructive and notes.get("idempotent") is True:
        method = "DELETE"
        params = php_query("input", args.input) or None
    else:
        method = "POST"
        json_body = {"input": args.input} if args.input is not None else {}
    # Never retried (spec §9.1), readonly included: the annotation is the
    # site's own claim.
    result, _ = ctx.client.request(method, route, params=params, json=json_body,
                                   base=ctx.base, idempotent=False)
    return {
        "status": "ok",
        **ctx.envelope(),
        "ability": args.name,
        "readonly": readonly,
        "destructive": destructive,
        "result": fence_tree(result),
    }
