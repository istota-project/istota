"""Generic routes and abilities: `rest` and `abilities list|run`.

`rest` reaches what the dedicated verbs do not: plugin namespaces, menus,
revisions, autosaves. The route is relative to the site's ``/wp-json/`` and may
not name a scheme, a host, a dot segment, a query, a fragment or a
percent-escape, so it cannot leave the site; queries go in ``--query``, where
``_method`` and ``rest_route`` are refused for every method, since WordPress
would act on them in place of the method and route the user agreed to. The
``batch/v1`` route and deleting users or plugins (spec §5.4) are refused too.
Every method but ``GET`` is gated (spec §5.6), with the body shown in the
``would`` line: the skill cannot know what a plugin's route does. No `rest` call is
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
from .common import gate, lookup_hint, quoted, read_json_file, total_header
from .discovery import ABILITIES_NAMESPACE

_ROUTE_SEGMENT_RE = re.compile(r"\A[A-Za-z0-9._~!$&'()*+,;=:@-]+\Z")

#: Route segments the skill never reaches: application passwords are a
#: credential path the model has no reason to touch (spec §5.4).
_REFUSED_SEGMENTS = frozenset({"application-passwords"})
#: Collections whose items `rest DELETE` may not remove (spec §5.4).
_UNDELETABLE = frozenset({"users", "plugins"})
#: Query keys WordPress reads ahead of the request itself: `_method` replaces
#: the HTTP method, and `rest_route` replaces the route in the path, which
#: would put every route check above out of play.
_REFUSED_QUERY_KEYS = ("_method", "rest_route")
#: How much of a body or an input a `would` line shows before it says so.
_SHOWN_BODY_CHARS = 600

REST_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
MAX_BODY_BYTES = 8 * 1024 * 1024

#: ``namespace/name``, as the Abilities API registers one.
_ABILITY_RE = re.compile(r"\A[a-z0-9-]{1,64}/[a-z0-9-]{1,128}\Z")
#: Ability namespaces that edit field group, field, post type, taxonomy and
#: options-page *definitions* (Secure Custom Fields registers ~50). The skill
#: never edits definitions (spec §3.4, decision 12): they are changed in
#: wp-admin and pulled into a repo, so only a read of one runs.
DEFINITION_NAMESPACES = frozenset({"scf", "acf"})


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
    # One batch request runs many routes, none of which this check would see.
    if segments and segments[0].lower() == "batch":
        raise WordPressError("The batch route is not reachable through this skill; send "
                             "each request on its own.", "validation_error")
    return "/".join(segments)


def check_write_route(method: str, route: str) -> None:
    """Spec §5.4: deleting a user or a plugin is left to wp-admin, `rest` included."""
    if method != "DELETE":
        return
    parts = [s.lower() for s in route.split("/")]
    if parts[:2] == ["wp", "v2"] and len(parts) > 3 and parts[2] in _UNDELETABLE:
        raise WordPressError(f"Deleting {parts[2]} is not offered; do it in wp-admin.",
                             "validation_error")


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
        # A GET carrying `_method=DELETE` would be a delete, and a confirmed
        # POST carrying `rest_route=` would go to a route the user never saw.
        name = _php_name(key).lower()
        if any(name.startswith(refused) for refused in _REFUSED_QUERY_KEYS):
            raise WordPressError(
                "--query may not override the HTTP method or the route (_method, rest_route).",
                "validation_error",
            )
        out.append((key.strip(), value))
    return out


def prepare_rest(args) -> None:
    """Check the route, the query and the body before the vault fetch is spent."""
    route = check_route(args.route)
    check_write_route(args.method, route)
    _query(args.query)
    args.body = None
    if args.body_file:
        if args.method == "GET":
            raise WordPressError("A GET takes no --body-file; use --query.", "validation_error")
        args.body = read_json_file(args.body_file, "--body-file", MAX_BODY_BYTES)


def shown_json(value) -> str:
    """A model-written JSON value for a `would` line, whole up to a bound."""
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= _SHOWN_BODY_CHARS:
        return text
    return (f"{text[:_SHOWN_BODY_CHARS]}... ({len(text)} characters, the first "
            f"{_SHOWN_BODY_CHARS} shown)")


def _describe_rest(method: str, route: str, query: list, body) -> str:
    parts = [f"send {method} {route}"]
    if query:
        parts.append("with query " + "&".join(f"{k}={v}" for k, v in query))
    if body is not None:
        parts.append(f"with the body {shown_json(body)}")
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


def annotations(ability: dict) -> dict:
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
        notes = annotations(ability)
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

    Booleans go as ``1``/``0``, not ``true``/``false``: schema validation
    accepts either, but nothing turns the string back into a boolean, and
    PHP reads ``"false"`` as true. Numbers go as their digits. A null or an
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
        return [(prefix, "1" if value else "0")]
    return [(prefix, str(value))]


def fetch_ability(ctx, name: str) -> dict:
    """The ability as the site registers it, read before anything runs it."""
    try:
        ability, _ = ctx.client.get(f"{ABILITIES_NAMESPACE}/abilities/{name}", base=ctx.base)
    except WordPressError as exc:
        raise _no_abilities(exc) from None
    if not isinstance(ability, dict):
        raise WordPressError("The site answered with something that is not an ability.",
                             "bad_response")
    return ability


def _object_schema(schema) -> bool:
    if not isinstance(schema, dict):
        return False
    kind = schema.get("type")
    return kind == "object" or (isinstance(kind, list) and "object" in kind)


def run_ability(args, ctx, name: str, ability: dict, value, *,
                described: str | None = None, always_gate: bool = False,
                hint: str | None = None):
    """Run `ability` with `value` as its input: ``(result, readonly, destructive)``.

    The ability's own annotations decide the gate and the method. `described`
    replaces the default `would` line, and `always_gate` gates whatever the
    site claims, for a caller that knows its ability writes.
    Never retried (spec §9.1), readonly included: the annotation is the
    site's own claim.
    """
    notes = annotations(ability)
    readonly = notes.get("readonly") is True
    destructive = notes.get("destructive") is True
    # Refused ahead of the gate, so no --confirmed reaches it.
    if name.split("/", 1)[0] in DEFINITION_NAMESPACES and (destructive or not readonly):
        raise WordPressError(
            f"{name} changes a field group, field, post type, taxonomy or options-page "
            f"definition. The skill never edits definitions; make the change in wp-admin.",
            "definition_edit_refused",
        )
    # An ability that calls itself both is gated: the annotation is the
    # site's own claim, and the cautious half wins.
    if always_gate or not readonly or destructive:
        if described is None:
            kind = "destructive ability" if destructive else "ability"
            given = f" with the input {shown_json(value)}" if value is not None else ""
            described = (f"run the {kind} {name} "
                         f"({fence(ability.get('label')) or 'no label'}){given}")
        gate(args, ctx, [described])
    route = f"{ABILITIES_NAMESPACE}/abilities/{name}/run"
    params = json_body = None
    # Core validates a missing input as null, which an object schema with no
    # `default` refuses ("input is not of type object"), so none given there
    # means an empty object: `input=` on a query (core reads it as one), `{}`
    # in a body.
    empty_object = value is None and _object_schema(ability.get("input_schema"))
    # The method follows the annotations even under `always_gate`: core's run
    # controller refuses any other.
    if readonly or (destructive and notes.get("idempotent") is True):
        method = "GET" if readonly else "DELETE"
        params = [("input", "")] if empty_object else (php_query("input", value) or None)
    else:
        method = "POST"
        if value is not None:
            json_body = {"input": value}
        else:
            json_body = {"input": {}} if empty_object else {}
    # Core refuses bad input before the ability runs, so that answer is never
    # ambiguous. Bad output comes after it ran, which only a read can shrug off.
    definite = {"ability_invalid_input"}
    if readonly and not destructive:
        definite.add("ability_invalid_output")
    try:
        result, _ = ctx.client.request(method, route, params=params, json=json_body,
                                       base=ctx.base, idempotent=False,
                                       definite_codes=frozenset(definite))
    except WordPressError as exc:
        if hint is not None:
            lookup_hint(exc, hint)
        raise
    return result, readonly, destructive


def cmd_abilities_run(args) -> dict:
    ctx = args.wp
    ability = fetch_ability(ctx, args.name)
    result, readonly, destructive = run_ability(args, ctx, args.name, ability, args.input)
    return {
        "status": "ok",
        **ctx.envelope(),
        "ability": args.name,
        "readonly": readonly,
        "destructive": destructive,
        "result": fence_tree(result),
    }
