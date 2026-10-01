"""Administration reads: `users list|get`, `settings get`, `plugins list`.

All core routes. The account's role bounds what answers: settings and users
need an administrator, and on a multisite network ``/wp/v2/plugins`` needs a
super admin. A 403 is `permission_denied`, which is information, not a defect.
"""

from __future__ import annotations

import re

from .client import WordPressError, fence, fence_tree, raw_text, selector, selectors
from .content import limit_arg, total_header


def _true_keys(value) -> list:
    if isinstance(value, dict):
        return selectors(sorted(str(k) for k, v in value.items() if v))
    return []


def project_user(user: dict) -> dict:
    return {
        "id": user.get("id"),
        "username": selector(user.get("username")),
        "slug": user.get("slug"),
        "name": fence(user.get("name")),
        "email": fence(user.get("email")),
        "roles": selectors(user.get("roles")),
        "registered_date": user.get("registered_date"),
    }


def cmd_users_list(args) -> dict:
    ctx = args.wp
    params: dict = {"context": "edit", "per_page": limit_arg(args.limit, 20), "page": args.page}
    if args.role:
        params["roles"] = args.role
    if args.search:
        params["search"] = args.search
    items, headers = ctx.client.get("wp/v2/users", params=params, base=ctx.base)
    rows = [project_user(u) for u in items or [] if isinstance(u, dict)]
    return {
        "status": "ok",
        **ctx.envelope(),
        "count": len(rows),
        "total": total_header(headers, "X-WP-Total"),
        "items": rows,
    }


def check_user_id(args) -> str:
    ident = args.id.strip().lower()
    if ident != "me" and not re.fullmatch(r"[0-9]{1,12}", ident):
        raise WordPressError("--id takes a user id or `me`.", "validation_error")
    return ident


def cmd_users_get(args) -> dict:
    ctx = args.wp
    ident = check_user_id(args)
    user, _ = ctx.client.get(f"wp/v2/users/{ident}", params={"context": "edit"}, base=ctx.base)
    if not isinstance(user, dict):
        raise WordPressError("The site answered with something that is not a user.",
                             "bad_response")
    item = project_user(user)
    item["capabilities"] = _true_keys(user.get("capabilities"))
    return {"status": "ok", **ctx.envelope(), "item": item}


def cmd_settings_get(args) -> dict:
    ctx = args.wp
    settings, _ = ctx.client.get("wp/v2/settings", base=ctx.base)
    return {"status": "ok", **ctx.envelope(), "settings": fence_tree(settings or {})}


def project_plugin(plugin: dict) -> dict:
    return {
        "plugin": selector(plugin.get("plugin")),
        # Not `status`: that key is the envelope's own.
        "plugin_status": plugin.get("status"),
        "name": fence(plugin.get("name")),
        "version": selector(plugin.get("version")),
        "network_only": plugin.get("network_only"),
        "requires_wp": selector(plugin.get("requires_wp")),
        "requires_php": selector(plugin.get("requires_php")),
        "author": fence(raw_text(plugin.get("author"))),
        "description": fence(raw_text(plugin.get("description"))),
    }


def cmd_plugins_list(args) -> dict:
    ctx = args.wp
    items, _ = ctx.client.get("wp/v2/plugins", params={"context": "edit"}, base=ctx.base)
    rows = [project_plugin(p) for p in items or [] if isinstance(p, dict)]
    return {"status": "ok", **ctx.envelope(), "count": len(rows), "items": rows}
