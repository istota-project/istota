"""Administration: `users list|get|create|update`, `settings get|update`,
`plugins list|activate|deactivate|install`.

All core routes. The account's role bounds what answers: settings and users
need an administrator, and on a multisite network ``/wp/v2/plugins`` needs a
super admin. A 403 is `permission_denied`, which is information, not a defect.

Every write here has a site-wide effect, so every one is gated (spec §5.7):
the handler reads what it is about to change, names it in the ``would`` line
with the site's own words fenced, and sends nothing but ``GET`` until the user
has agreed. A write that would leave the site as it already is (a plugin
already in the asked state, or already installed) answers without the gate and
sends nothing, which is also what makes a retry after ``outcome_unknown`` safe.

Deleting users and plugins, and anything touching application passwords, is
not offered (spec §5.4).
"""

from __future__ import annotations

import json
import re
import secrets

from .client import (
    UPLOAD_TIMEOUT,
    WordPressError,
    fence,
    fence_tree,
    raw_text,
    selector,
    selectors,
)
from .common import gate, limit_arg, lookup, lookup_hint, quoted, total_header


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


# --------------------------------------------------------------------------- #
# users create | update
# --------------------------------------------------------------------------- #

#: A role slug as WordPress stores one (``sanitize_key``).
_ROLE_RE = re.compile(r"\A[a-z0-9_-]{1,64}\Z")
#: Loose on purpose: WordPress runs ``is_email`` itself. This only keeps a
#: value that is plainly not an address from spending a vault fetch.
_EMAIL_RE = re.compile(r"\A[^@\s]+@[^@\s]+\.[^@\s]+\Z")
#: Core REST requires a password on create. The skill never sends one anybody
#: knows: this many random bytes go once and are dropped, and the new user sets
#: their own through "Lost your password?" (spec §5.4: no password is set).
_UNKNOWN_PASSWORD_BYTES = 32

#: ``users update`` attribute -> REST field.
_USER_FIELDS = (("name", "name"), ("email", "email"), ("first_name", "first_name"),
                ("last_name", "last_name"))


def _check_role(role: str | None) -> None:
    if role is not None and not _ROLE_RE.fullmatch(role):
        raise WordPressError(f"--role takes a role slug such as editor, not {quoted(role)}.",
                             "validation_error")


def _check_email(email: str | None) -> None:
    if email is not None and not _EMAIL_RE.fullmatch(email.strip()):
        raise WordPressError(f"--email {quoted(email)} is not an email address.",
                             "validation_error")


def check_users_create(args) -> None:
    if not args.username.strip():
        raise WordPressError("--username is empty.", "validation_error")
    _check_email(args.email)
    _check_role(args.role)


def cmd_users_create(args) -> dict:
    """Create one user with a role. Sent once; never with a password anybody knows."""
    ctx = args.wp
    username = args.username.strip()
    email = args.email.strip()
    described = f"create user {quoted(username)} <{quoted(email)}> with role {args.role}"
    if args.name:
        described += f", display name {quoted(args.name)}"
    gate(args, ctx, [described + ", with no password anybody knows and no email sent; "
                     'the user sets a password through "Lost your password?"'])
    body = {
        "username": username,
        "email": email,
        "roles": [args.role],
        "password": secrets.token_urlsafe(_UNKNOWN_PASSWORD_BYTES),
    }
    if args.name:
        body["name"] = args.name
    hint = lookup(ctx, "users", "list", "--search", username)
    try:
        user, _ = ctx.client.request("POST", "wp/v2/users", json=body, base=ctx.base,
                                     idempotent=False)
    except WordPressError as exc:
        lookup_hint(exc, hint)
        raise
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise WordPressError("The site accepted the user but its answer names no id.",
                             "outcome_unknown", lookup=hint)
    return {"status": "ok", **ctx.envelope(), "created": True, "item": project_user(user)}


def check_users_update(args) -> None:
    check_user_id(args)
    _check_role(args.role)
    _check_email(args.email)
    if args.role is None and all(getattr(args, attr) is None for attr, _ in _USER_FIELDS):
        raise WordPressError("Name at least one field to change (--role, --name, --email, "
                             "--first-name, --last-name).", "validation_error")


def cmd_users_update(args) -> dict:
    ctx = args.wp
    ident = check_user_id(args)
    current, _ = ctx.client.get(f"wp/v2/users/{ident}", params={"context": "edit"},
                                base=ctx.base)
    if not isinstance(current, dict):
        raise WordPressError("The site answered with something that is not a user.",
                             "bad_response")
    body: dict = {}
    changes = []
    if args.role is not None:
        body["roles"] = [args.role]
        was = ", ".join(str(r) for r in selectors(current.get("roles"))) or "none"
        changes.append(f"role {was} -> {args.role}")
    for attr, field in _USER_FIELDS:
        value = getattr(args, attr)
        if value is not None:
            body[field] = value.strip() if field == "email" else value
            changes.append(f"{field} -> {quoted(body[field])}")
    shown_id = current.get("id") if isinstance(current.get("id"), int) else ident
    label = f"user #{shown_id} {fence(current.get('name'))}".rstrip()
    gate(args, ctx, [f"update {label}: {'; '.join(changes)}"])
    user, _ = ctx.client.request("POST", f"wp/v2/users/{ident}", json=body, base=ctx.base,
                                 idempotent=True)
    if not isinstance(user, dict):
        raise WordPressError("The site answered with something that is not a user.",
                             "bad_response")
    return {"status": "ok", **ctx.envelope(), "item": project_user(user)}


# --------------------------------------------------------------------------- #
# settings update
# --------------------------------------------------------------------------- #

_SETTING_KEY_RE = re.compile(r"\A[A-Za-z0-9_]{1,64}\Z")
_SHOWN_VALUE_CHARS = 120


def parse_settings(pairs: list[str]) -> dict:
    """``KEY=JSON`` pairs as one object; a later key wins."""
    out: dict = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not _SETTING_KEY_RE.fullmatch(key):
            raise WordPressError(f"--set takes KEY=JSON with a setting name for KEY, not "
                                 f"{quoted(pair)}.", "validation_error")
        try:
            out[key] = json.loads(raw)
        except ValueError:
            raise WordPressError(
                f"--set {key}: the value is JSON, so a string is quoted ({key}='\"text\"').",
                "validation_error",
            ) from None
    return out


def check_settings_update(args) -> None:
    args.settings = parse_settings(args.set)


def _shown(value) -> str:
    text = json.dumps(value, ensure_ascii=False)
    if len(text) > _SHOWN_VALUE_CHARS:
        return text[:_SHOWN_VALUE_CHARS - 3] + "..."
    return text


def cmd_settings_update(args) -> dict:
    """Write site settings, named whole in the `would` line, and read them back."""
    ctx = args.wp
    wanted = args.settings
    current, _ = ctx.client.get("wp/v2/settings", base=ctx.base)
    current = current if isinstance(current, dict) else {}
    changes = []
    for key, value in wanted.items():
        if key in current:
            was = current[key]
            # The current value is the site's words, whatever its type; the
            # new one is the model's.
            was_text = fence(was if isinstance(was, str) else _shown(was))
            changes.append(f"{key} from {was_text} to {_shown(value)}")
        else:
            changes.append(f"{key} (a setting this site does not report) to {_shown(value)}")
    gate(args, ctx, ["change site settings: " + "; ".join(changes)])
    after, _ = ctx.client.request("POST", "wp/v2/settings", json=wanted, base=ctx.base,
                                  idempotent=True)
    after = after if isinstance(after, dict) else {}
    return {
        "status": "ok",
        **ctx.envelope(),
        "settings": fence_tree({k: after[k] for k in wanted if k in after}),
        # WordPress ignores a setting not registered for REST without a word.
        "readback": {
            "dropped": sorted(k for k in wanted if k not in after),
            "changed": sorted(k for k in wanted if k in after and after[k] != wanted[k]),
        },
    }


# --------------------------------------------------------------------------- #
# plugins activate | deactivate | install
# --------------------------------------------------------------------------- #

#: ``dir/file`` as ``/wp/v2/plugins/<plugin>`` takes it: no dots, at most one
#: slash, matching core's route pattern ``[^.\/]+(?:\/[^.\/]+)?``.
_PLUGIN_RE = re.compile(r"\A[A-Za-z0-9_-]{1,100}(?:/[A-Za-z0-9_-]{1,100})?\Z")
#: A WordPress.org directory slug.
_SLUG_RE = re.compile(r"\A[a-z0-9-]{1,100}\Z")
#: How a route that will not network-activate answers: a definite refusal that
#: is neither the account's permission nor a missing route (spec §14.3).
_NETWORK_REFUSALS = frozenset({"validation_error", "request_refused"})


def plugin_id(value: str) -> str:
    """The plugin as the route takes it; ``akismet/akismet.php`` is accepted."""
    text = value.strip()
    if text.endswith(".php"):
        text = text[:-4]
    if not _PLUGIN_RE.fullmatch(text):
        raise WordPressError(
            f"--plugin takes the plugin as `plugins list` names it (dir/file), not "
            f"{quoted(value)}.",
            "validation_error",
        )
    return text


def check_plugin_status(args) -> None:
    args.plugin = plugin_id(args.plugin)


def _plugin_status(args, new_status: str) -> dict:
    ctx = args.wp
    path = f"wp/v2/plugins/{args.plugin}"
    current, _ = ctx.client.get(path, params={"context": "edit"}, base=ctx.base)
    if not isinstance(current, dict):
        raise WordPressError("The site answered with something that is not a plugin.",
                             "bad_response")
    was = current.get("status")
    if new_status == "inactive":
        # Core deactivates network-wide whenever the plugin is network-active,
        # so that reach has to have been asked for, and shown.
        if was == "network-active" and not args.network:
            raise WordPressError(
                f"{args.plugin} is network-active, so deactivating it acts on every site "
                f"of the network. Pass --network to ask for that.",
                "validation_error",
            )
        if args.network and was not in ("network-active", "inactive"):
            raise WordPressError(
                f"{args.plugin} is not network-active ({selector(was)}); deactivate it "
                f"without --network.",
                "validation_error",
            )
    # Activating a network-active plugin on one site changes nothing in core.
    if was == new_status or (new_status == "active" and was == "network-active"):
        return {"status": "ok", **ctx.envelope(), "changed": False,
                "item": project_plugin(current)}
    verb = "deactivate" if new_status == "inactive" else "activate"
    reach = " network-wide" if args.network else ""
    name = fence(current.get("name"))
    named = f" ({name})" if name else ""
    gate(args, ctx, [f"{verb} plugin {args.plugin}{named}{reach}"])
    try:
        after, _ = ctx.client.request("POST", path, json={"status": new_status}, base=ctx.base,
                                      idempotent=True)
    except WordPressError as exc:
        if new_status == "network-active" and exc.reason in _NETWORK_REFUSALS:
            raise WordPressError(
                f"The site refused to network-activate {args.plugin} through core REST: "
                f"{exc} Either the site is not a multisite network, or core REST cannot "
                f"network-activate there; on a network, use the network admin instead.",
                "unsupported_on_multisite", **exc.extra,
            ) from None
        raise
    if not isinstance(after, dict):
        raise WordPressError("The site answered with something that is not a plugin.",
                             "bad_response")
    return {"status": "ok", **ctx.envelope(), "changed": True, "item": project_plugin(after)}


def cmd_plugins_activate(args) -> dict:
    return _plugin_status(args, "network-active" if args.network else "active")


def cmd_plugins_deactivate(args) -> dict:
    return _plugin_status(args, "inactive")


def check_plugin_install(args) -> None:
    if not _SLUG_RE.fullmatch(args.slug.strip()):
        raise WordPressError(f"--slug takes a WordPress.org plugin slug such as hello-dolly, "
                             f"not {quoted(args.slug)}.", "validation_error")
    if args.network and not args.activate:
        raise WordPressError("--network says how to activate; pass it with --activate.",
                             "validation_error")


def cmd_plugins_install(args) -> dict:
    """Install from the WordPress.org directory. Sent once.

    A plugin already installed from that directory answers ``installed: false``
    with no gate and no write, so running it again after ``outcome_unknown`` is
    the lookup.
    """
    ctx = args.wp
    slug = args.slug.strip()
    installed, _ = ctx.client.get("wp/v2/plugins", params={"context": "edit"}, base=ctx.base)
    for plugin in installed or []:
        if isinstance(plugin, dict) and str(plugin.get("plugin", "")).split("/", 1)[0] == slug:
            return {"status": "ok", **ctx.envelope(), "installed": False,
                    "item": project_plugin(plugin)}
    status, then = "inactive", ""
    if args.activate:
        status = "network-active" if args.network else "active"
        then = " and activate it network-wide" if args.network else " and activate it"
    gate(args, ctx, [f"install plugin {slug} from the WordPress.org directory{then}"])
    hint = lookup(ctx, "plugins", "list")
    try:
        plugin, _ = ctx.client.request("POST", "wp/v2/plugins",
                                       json={"slug": slug, "status": status}, base=ctx.base,
                                       idempotent=False, timeout=UPLOAD_TIMEOUT)
    except WordPressError as exc:
        lookup_hint(exc, hint)
        raise
    if not isinstance(plugin, dict):
        raise WordPressError("The site installed something but its answer is not a plugin.",
                             "outcome_unknown", lookup=hint)
    return {"status": "ok", **ctx.envelope(), "installed": True, "item": project_plugin(plugin)}
