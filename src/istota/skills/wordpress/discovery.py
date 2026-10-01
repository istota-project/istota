"""`describe`, and the route table every other verb is routed through.

A type is reached at ``/{rest_namespace}/{rest_base}`` as ``/wp/v2/types``
reports it, never by ``slug + "s"`` and never assuming ``wp/v2``: a type's
``rest_base`` is whatever its registration says (``update`` may be
``updates``), and a plugin's type may live in its own namespace. The table comes from the cache that
`describe` fills, or from one ``/types`` plus ``/taxonomies`` pair on a miss.

The ACF field schema comes from core's ``OPTIONS`` schema for the type's
collection (``schema.properties.acf``), which carries the full JSON Schema with
no plugin; a type without that property has no field group with "Show in REST
API" switched on.
"""

from __future__ import annotations

import json
from pathlib import Path

from istota.skill_host_paths import write_resolved

from .client import WordPressError, fence, fence_keys, selector, selectors

ABILITIES_NAMESPACE = "wp-abilities/v1"
#: The abilities the istota-connector plugin registers (spec §3.3).
CONNECTOR_ABILITIES = ("istota/options-get", "istota/options-update", "istota/network-sites")

_SCHEMA_PROSE_KEYS = frozenset({"title", "description"})
ACF_NOTE = (
    "This type has no ACF fields over REST. Each ACF / SCF field group has a "
    "'Show in REST API' setting, off by default; switch it on in the field "
    "group's settings for its fields to appear here."
)


def _route(namespace: object, base: object) -> str | None:
    if not isinstance(base, str) or not base:
        return None
    ns = namespace if isinstance(namespace, str) and namespace else "wp/v2"
    return f"{ns.strip('/')}/{base.strip('/')}"


def _routes_from(types: object, taxonomies: object) -> dict:
    table = {"types": {}, "taxonomies": {}}
    for slug, item in (types or {}).items() if isinstance(types, dict) else ():
        if not isinstance(item, dict):
            continue
        route = _route(item.get("rest_namespace"), item.get("rest_base"))
        if route:
            table["types"][slug] = {
                "route": route,
                "taxonomies": [t for t in item.get("taxonomies") or [] if isinstance(t, str)],
            }
    for slug, item in (taxonomies or {}).items() if isinstance(taxonomies, dict) else ():
        if not isinstance(item, dict):
            continue
        route = _route(item.get("rest_namespace"), item.get("rest_base"))
        if route:
            table["taxonomies"][slug] = {
                "route": route,
                "rest_base": item.get("rest_base"),
            }
    return table


def routes(ctx) -> dict:
    """``{"types": {slug: {route, taxonomies}}, "taxonomies": {slug: {route, rest_base}}}``."""
    cached = ctx.cache.get("routes")
    if isinstance(cached, dict):
        return cached
    types, _ = ctx.client.get("wp/v2/types", base=ctx.base)
    taxonomies, _ = ctx.client.get("wp/v2/taxonomies", base=ctx.base)
    table = _routes_from(types, taxonomies)
    ctx.cache.put("routes", table)
    return table


def type_route(ctx, slug: str) -> str:
    entry = routes(ctx)["types"].get(slug)
    if entry is None:
        raise WordPressError(
            f"Post type {slug!r} is not REST-visible or does not exist; the skill "
            f"cannot tell the two apart. `describe` lists the types it can reach.",
            "unknown_type",
        )
    return entry["route"]


def taxonomy_route(ctx, slug: str) -> str:
    entry = routes(ctx)["taxonomies"].get(slug)
    if entry is None:
        raise WordPressError(
            f"Taxonomy {slug!r} is not REST-visible or does not exist. `describe` "
            f"lists the taxonomies it can reach.",
            "unknown_taxonomy",
        )
    return entry["route"]


def taxonomy_fields(ctx, type_slug: str) -> list[str]:
    """The payload keys (taxonomy ``rest_base`` values) a type's terms appear under."""
    table = routes(ctx)
    names = table["types"].get(type_slug, {}).get("taxonomies", [])
    return [
        table["taxonomies"][name]["rest_base"]
        for name in names
        if name in table["taxonomies"] and table["taxonomies"][name].get("rest_base")
    ]


def _true_keys(value: object) -> list:
    if isinstance(value, dict):
        return selectors(sorted(str(k) for k, v in value.items() if v))
    return []


def _describe_site(ctx) -> dict:
    client, base = ctx.client, ctx.base
    index, _ = client.get("", base=base)
    index = index if isinstance(index, dict) else {}
    me, _ = client.get("wp/v2/users/me", params={"context": "edit"}, base=base)
    me = me if isinstance(me, dict) else {}
    types, _ = client.get("wp/v2/types", params={"context": "edit"}, base=base)
    taxonomies, _ = client.get("wp/v2/taxonomies", params={"context": "edit"}, base=base)
    namespaces = [n for n in index.get("namespaces") or [] if isinstance(n, str)]

    abilities: list[str] | None = None
    if ABILITIES_NAMESPACE in namespaces:
        try:
            listed, _ = client.get(f"{ABILITIES_NAMESPACE}/abilities",
                                   params={"per_page": 100}, base=base)
            abilities = sorted(
                a["name"] for a in listed or []
                if isinstance(a, dict) and isinstance(a.get("name"), str)
            )
        except WordPressError as exc:
            if exc.reason not in ("permission_denied", "unknown_route", "not_found"):
                raise

    ctx.cache.put("routes", _routes_from(types, taxonomies))

    type_rows = []
    for slug, item in sorted((types or {}).items() if isinstance(types, dict) else ()):
        if not isinstance(item, dict):
            continue
        type_rows.append({
            "slug": selector(slug),
            "name": fence(item.get("name")),
            "rest_namespace": selector(item.get("rest_namespace") or "wp/v2"),
            "rest_base": selector(item.get("rest_base")),
            "viewable": item.get("viewable"),
            "hierarchical": item.get("hierarchical"),
            "taxonomies": selectors(item.get("taxonomies")),
            "supports": _true_keys(item.get("supports")),
        })
    taxonomy_rows = []
    for slug, item in sorted((taxonomies or {}).items() if isinstance(taxonomies, dict) else ()):
        if not isinstance(item, dict):
            continue
        taxonomy_rows.append({
            "slug": selector(slug),
            "name": fence(item.get("name")),
            "rest_namespace": selector(item.get("rest_namespace") or "wp/v2"),
            "rest_base": selector(item.get("rest_base")),
            "hierarchical": item.get("hierarchical"),
            "types": selectors(item.get("types")),
        })
    return {
        "site_name": fence(index.get("name")),
        "site_description": fence(index.get("description")),
        "site_url": fence(index.get("url")),
        "namespaces": selectors(namespaces),
        "abilities_api": ABILITIES_NAMESPACE in namespaces,
        "abilities": None if abilities is None else selectors(abilities),
        "connector": (
            None if abilities is None
            else all(name in abilities for name in CONNECTOR_ABILITIES)
        ),
        "account": {
            "id": me.get("id"),
            "username": selector(me.get("username")),
            "name": fence(me.get("name")),
            "roles": selectors(me.get("roles")),
            "capabilities": _true_keys(me.get("capabilities")),
        },
        "types": type_rows,
        "taxonomies": taxonomy_rows,
    }


def _acf_summary(acf: dict) -> list[dict]:
    props = acf.get("properties") if isinstance(acf.get("properties"), dict) else {}
    return [
        {"name": name, "type": spec.get("type") if isinstance(spec, dict) else None}
        for name, spec in props.items()
    ]


def acf_schema(ctx, slug: str) -> dict | None:
    """The type's ACF JSON Schema (``schema.properties.acf``), or None if it has none.

    From the cache `describe` fills, or one ``OPTIONS`` on the collection.
    """
    route = type_route(ctx, slug)
    schema = ctx.cache.get(f"schema:{slug}")
    if not isinstance(schema, dict):
        answer, _ = ctx.client.request("OPTIONS", route, base=ctx.base)
        schema = answer.get("schema") if isinstance(answer, dict) else None
        schema = schema if isinstance(schema, dict) else {}
        ctx.cache.put(f"schema:{slug}", schema)
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    return props.get("acf") if isinstance(props.get("acf"), dict) else None



def _describe_type(ctx, slug: str, output) -> dict:
    route = type_route(ctx, slug)
    acf = acf_schema(ctx, slug)
    result: dict = {"slug": slug, "route": route}
    if acf is None:
        result["acf"] = None
        result["acf_note"] = ACF_NOTE
        return result
    fenced = fence_keys(acf, _SCHEMA_PROSE_KEYS)
    result["acf_fields"] = _acf_summary(acf)
    if output is not None:
        data = json.dumps(fenced, indent=2, ensure_ascii=False).encode()
        write_resolved(Path(output), data)
        result["acf_schema_written_to"] = str(output)
        result["acf_schema_bytes"] = len(data)
    else:
        result["acf_note"] = "Pass --output to write the full ACF JSON Schema to a file."
    return result


def cmd_describe(args) -> dict:
    ctx = args.wp
    if args.refresh:
        ctx.cache.drop()
    summary = ctx.cache.get("describe")
    if not isinstance(summary, dict):
        summary = _describe_site(ctx)
        ctx.cache.put("describe", summary)
    out = {"status": "ok", **ctx.envelope(), **summary}
    if args.type:
        out["type"] = _describe_type(ctx, args.type, args.output)
    return out
