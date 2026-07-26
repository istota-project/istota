"""Nextcloud control-plane CLI: capabilities, users, groups, shares.

Usage:
    python -m istota.skills.nextcloud capabilities [--raw] [--check a,b]
    python -m istota.skills.nextcloud user whoami
    python -m istota.skills.nextcloud user search QUERY [--limit N] [--types users,groups]
    python -m istota.skills.nextcloud share list [--path /path]
    ...

All output is JSON on stdout. A failure prints a structured envelope carrying
the HTTP status, the OCS status code and the server's own message, and exits 1.

Env vars: NC_URL, NC_USER, NC_PASS. ISTOTA_USER_ID scopes file and share paths
to the calling user's workspace.
"""

import argparse
import json
import os
import sys

from istota.config import Config, NextcloudConfig, load_admin_users
from istota.nextcloud import (
    OcsError,
    PathScopeError,
    capabilities as caps_mod,
    resolve_scoped_path,
    shares as shares_mod,
    users as users_mod,
)
from istota.nextcloud_client import (
    ocs_create_public_link,
    ocs_create_share,
    ocs_delete_share,
    ocs_list_shares,
    ocs_search_sharees,
)

_SHARE_TYPE_MAP = shares_mod.SHARE_TYPES
_DEFAULT_EXPIRE_DAYS = 14


def _config_from_env() -> Config:
    url = os.environ.get("NC_URL", "")
    user = os.environ.get("NC_USER", "")
    password = os.environ.get("NC_PASS", "")
    if not url or not user or not password:
        print(json.dumps({"error": "NC_URL, NC_USER, NC_PASS env vars required"}), file=sys.stderr)
        sys.exit(1)
    config = Config(nextcloud=NextcloudConfig(url=url, username=user, app_password=password))
    # Path scoping consults Config.is_admin, whose "empty file means everyone"
    # back-compat rule is the same one the sandbox and skill gates use.
    try:
        config.admin_users = load_admin_users()
    except Exception:
        config.admin_users = set()
    return config


def _caller() -> str:
    return os.environ.get("ISTOTA_USER_ID", "")


def _scoped(config: Config, path: str) -> str:
    """Normalize a caller-supplied path and confine it to their workspace."""
    user_id = _caller()
    return resolve_scoped_path(path, user_id, is_admin=config.is_admin(user_id))


def _default_expire_days() -> int:
    raw = os.environ.get("NC_SHARE_DEFAULT_EXPIRE_DAYS", "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_EXPIRE_DAYS


def _confirmation_required(verb: str, subject: str, action_desc: str):
    """Default-refuse envelope for a destructive op lacking --confirmed."""
    return {
        "status": "error",
        "needs_confirmation": True,
        "error": (
            f"'{verb}' on {subject} is a destructive action that requires "
            f"confirmation. Ask the user to approve {action_desc}, then re-run "
            "with --confirmed."
        ),
    }


def _output(data):
    print(json.dumps(data, indent=2, default=str))


# --- capabilities ---


def cmd_capabilities(args):
    config = _config_from_env()
    payload = caps_mod.fetch_capabilities(config)

    if args.raw:
        return payload

    if args.check:
        names = [n.strip() for n in args.check.split(",") if n.strip()]
        checks = caps_mod.evaluate_checks(payload, names)
        missing = [n for n, ok in checks.items() if not ok]
        result = {"checks": checks, "missing": missing, "known": caps_mod.known_feature_names()}
        if missing:
            result["status"] = "error"
            result["error"] = "Missing required capabilities: " + ", ".join(missing)
        else:
            result["status"] = "ok"
        return result

    account = {}
    try:
        account = caps_mod.fetch_account(config)
    except OcsError:
        # The summary is still useful without the account block (some managed
        # instances gate /cloud/user); don't fail the whole probe for it.
        pass
    return caps_mod.summarize(payload, account)


# --- user / group ---


def cmd_user_whoami(args):
    return users_mod.whoami(_config_from_env())


def cmd_user_search(args):
    types = [t.strip() for t in (args.types or "users,groups").split(",") if t.strip()]
    return users_mod.search(
        _config_from_env(),
        args.query,
        types=types,
        limit=args.limit,
        item_type=args.item_type,
    )


def cmd_user_get(args):
    return users_mod.get_user(_config_from_env(), args.uid)


def cmd_user_groups(args):
    config = _config_from_env()
    uid = args.uid or users_mod.whoami(config).get("id", "")
    return {"user": uid, "groups": users_mod.user_groups(config, uid)}


def cmd_group_list(args):
    return {"groups": users_mod.list_groups(_config_from_env(), args.search)}


def cmd_group_members(args):
    return {"group": args.gid, "members": users_mod.group_members(_config_from_env(), args.gid)}


# --- share ---


def cmd_share_list(args):
    config = _config_from_env()
    path = _scoped(config, args.path) if args.path else None

    if getattr(args, "reshares", False) or getattr(args, "subfiles", False) or getattr(
        args, "shared_with_me", False
    ):
        return shares_mod.list_shares(
            config,
            path=path,
            reshares=bool(getattr(args, "reshares", False)),
            subfiles=bool(getattr(args, "subfiles", False)),
            shared_with_me=bool(getattr(args, "shared_with_me", False)),
        )

    shares = ocs_list_shares(config, path=path)
    if shares is None:
        raise OcsError(
            "Failed to list shares", None, None, "/apps/files_sharing/api/v1/shares"
        )
    return shares


def cmd_share_get(args):
    return shares_mod.get_share(_config_from_env(), args.share_id)


def cmd_share_create(args):
    config = _config_from_env()
    share_type = _SHARE_TYPE_MAP.get(args.type)
    if share_type is None:
        raise ValueError(
            f"Unknown share type: {args.type}. Use one of: {', '.join(sorted(_SHARE_TYPE_MAP))}"
        )

    path = _scoped(config, args.path)
    extras = {
        "note": getattr(args, "note", None),
        "send_mail": True if getattr(args, "send_mail", False) else None,
        "attributes": getattr(args, "attributes", None),
    }
    has_extras = any(v is not None for v in extras.values())

    if share_type != shares_mod.LINK_SHARE_TYPE and not getattr(args, "with_user", None):
        raise ValueError("--with is required for user, group, email, federated and talk shares")

    # The legacy path stays on the historical wrappers so its call shape is
    # unchanged; anything using the new fields goes through shares.create_share.
    if has_extras:
        return shares_mod.create_share(
            config,
            path=path,
            share_type=share_type,
            share_with=getattr(args, "with_user", None),
            permissions=args.permissions if args.permissions is not None
            else (1 if share_type == shares_mod.LINK_SHARE_TYPE else None),
            password=args.password,
            expire_date=args.expire,
            label=args.label,
            **extras,
        )

    if share_type == shares_mod.LINK_SHARE_TYPE:
        result = ocs_create_public_link(
            config,
            path=path,
            permissions=args.permissions or 1,
            password=args.password,
            expire_date=args.expire,
            label=args.label,
        )
    else:
        result = ocs_create_share(
            config,
            path=path,
            share_type=share_type,
            share_with=args.with_user,
            permissions=args.permissions,
            password=args.password,
            expire_date=args.expire,
            label=args.label,
        )

    if result is None:
        raise OcsError(
            "Failed to create share", None, None, "/apps/files_sharing/api/v1/shares"
        )
    return result


def cmd_share_update(args):
    return shares_mod.update_share(
        _config_from_env(),
        args.share_id,
        permissions=args.permissions,
        password=args.password,
        expire_date=args.expire,
        note=args.note,
        label=args.label,
    )


def cmd_share_link(args):
    config = _config_from_env()
    path = _scoped(config, args.path)

    password = args.password
    if getattr(args, "password_generate", False):
        password = shares_mod.generate_password()

    days = args.days if args.days is not None else _default_expire_days()

    # Only consult the server when an expiry is actually being requested.
    server_limit = None
    if days > 0:
        try:
            server_limit = caps_mod.public_link_expiry_limit(
                caps_mod.fetch_capabilities(config)
            )
        except OcsError:
            server_limit = None

    return shares_mod.create_link(
        config,
        path=path,
        days=days,
        password=password,
        permissions=args.permissions if args.permissions is not None else 1,
        label=args.label,
        note=args.note,
        file_name=args.file,
        server_expiry_limit=server_limit,
    )


def cmd_share_revoke(args):
    config = _config_from_env()

    if args.path:
        # A path revoke can remove several links at once, so it defaults to refusing.
        if not args.confirmed:
            return _confirmation_required(
                "share revoke --path",
                args.path,
                "revoking every public link on that path",
            )
        return shares_mod.revoke(config, path=_scoped(config, args.path))

    if args.token:
        return shares_mod.revoke(config, token=args.token)

    if args.share_id is not None:
        return shares_mod.revoke(config, share_id=args.share_id)

    raise ValueError("Pass a share id, --token TOKEN, or --path PATH")


def cmd_share_delete(args):
    config = _config_from_env()
    if not ocs_delete_share(config, args.share_id):
        raise OcsError(
            f"Failed to delete share {args.share_id}",
            None,
            None,
            f"/apps/files_sharing/api/v1/shares/{args.share_id}",
        )
    return {"status": "deleted", "share_id": args.share_id}


def cmd_share_search(args):
    config = _config_from_env()
    result = ocs_search_sharees(config, args.query, item_type=args.item_type)
    if result is None:
        raise OcsError(
            "Failed to search sharees", None, None, "/apps/files_sharing/api/v1/sharees"
        )
    return result


# --- parser ---


def build_parser():
    parser = argparse.ArgumentParser(description="Nextcloud control-plane CLI")
    sub = parser.add_subparsers(dest="group")

    # capabilities
    p_caps = sub.add_parser("capabilities", help="What this Nextcloud server supports")
    p_caps.add_argument("--raw", action="store_true", help="Full capabilities payload")
    p_caps.add_argument(
        "--check",
        default=None,
        help="Comma list of dotted feature names; exits non-zero if any is missing",
    )

    # user
    user = sub.add_parser("user", help="User lookup")
    user_sub = user.add_subparsers(dest="command")

    user_sub.add_parser("whoami", help="The account these credentials authenticate as")

    p_usearch = user_sub.add_parser("search", help="Autocomplete search (works as a regular user)")
    p_usearch.add_argument("query", help="Search term")
    p_usearch.add_argument("--limit", type=int, default=25, help="Max results (default: 25)")
    p_usearch.add_argument(
        "--types",
        default="users,groups",
        help=f"Comma list from: {', '.join(sorted(users_mod.SHARE_TYPES))}",
    )
    p_usearch.add_argument("--item-type", default="file", help="Item type (default: file)")

    p_uget = user_sub.add_parser("get", help="User record (needs admin rights)")
    p_uget.add_argument("uid", help="Nextcloud user id")

    p_ugroups = user_sub.add_parser("groups", help="Groups a user belongs to (needs admin rights)")
    p_ugroups.add_argument("uid", nargs="?", default=None, help="User id (default: the bot)")

    # group
    group = sub.add_parser("group", help="Group lookup (needs admin rights)")
    group_sub = group.add_subparsers(dest="command")

    p_glist = group_sub.add_parser("list", help="List groups")
    p_glist.add_argument("--search", default=None, help="Filter by substring")

    p_gmembers = group_sub.add_parser("members", help="List a group's members")
    p_gmembers.add_argument("gid", help="Group id")

    # share
    share = sub.add_parser("share", help="Share operations")
    share_sub = share.add_subparsers(dest="command")

    p_list = share_sub.add_parser("list", help="List shares")
    p_list.add_argument("--path", default=None, help="Filter by Nextcloud path")
    p_list.add_argument("--reshares", action="store_true", help="Include reshares")
    p_list.add_argument("--subfiles", action="store_true", help="Shares inside the given folder")
    p_list.add_argument(
        "--shared-with-me", action="store_true", help="Shares others made with this account"
    )

    p_get = share_sub.add_parser("get", help="Show one share")
    p_get.add_argument("share_id", type=int, help="Share ID")

    p_create = share_sub.add_parser("create", help="Create a share")
    p_create.add_argument("--path", required=True, help="Nextcloud file/folder path")
    p_create.add_argument(
        "--type", required=True, choices=sorted(_SHARE_TYPE_MAP), help="Share type"
    )
    p_create.add_argument("--with", dest="with_user", help="Username, group, email or room token")
    p_create.add_argument("--permissions", type=int, default=None, help="Bitmask (1=read, 31=all)")
    p_create.add_argument("--password", default=None, help="Password protection")
    p_create.add_argument("--expire", default=None, help="Expiry date (YYYY-MM-DD)")
    p_create.add_argument("--label", default=None, help="Label for public links")
    p_create.add_argument("--note", default=None, help="Note shown to the recipient")
    p_create.add_argument("--send-mail", action="store_true", help="Email the recipient")
    p_create.add_argument("--attributes", default=None, help="Share attributes (JSON)")

    p_update = share_sub.add_parser("update", help="Change an existing share")
    p_update.add_argument("share_id", type=int, help="Share ID")
    p_update.add_argument("--permissions", type=int, default=None, help="Bitmask")
    p_update.add_argument("--password", default=None, help="Set a password")
    p_update.add_argument("--expire", default=None, help="Expiry date (YYYY-MM-DD)")
    p_update.add_argument("--note", default=None, help="Note shown to the recipient")
    p_update.add_argument("--label", default=None, help="Label for public links")

    p_link = share_sub.add_parser(
        "link", help="Create a public download link with sensible defaults"
    )
    p_link.add_argument("path", help="Nextcloud file/folder path")
    p_link.add_argument(
        "--days", type=int, default=None,
        help="Expire after N days (0 = never; default from config)",
    )
    p_link.add_argument("--password", default=None, help="Protect with this password")
    p_link.add_argument(
        "--password-generate", action="store_true", help="Generate and report a password"
    )
    p_link.add_argument("--permissions", type=int, default=None, help="Bitmask (default: 1, read)")
    p_link.add_argument("--label", default=None, help="Label for the link")
    p_link.add_argument("--note", default=None, help="Note shown to the recipient")
    p_link.add_argument(
        "--file", default=None,
        help="When sharing a folder, name one file for the direct-download URL",
    )

    p_revoke = share_sub.add_parser("revoke", help="Revoke a share by id, token or path")
    p_revoke.add_argument("share_id", type=int, nargs="?", default=None, help="Share ID")
    p_revoke.add_argument("--token", default=None, help="Public-link token")
    p_revoke.add_argument("--path", default=None, help="Revoke every public link on this path")
    p_revoke.add_argument(
        "--confirmed", action="store_true", help="Required for --path (removes several at once)"
    )

    p_delete = share_sub.add_parser("delete", help="Delete a share")
    p_delete.add_argument("share_id", type=int, help="Share ID to delete")

    p_search = share_sub.add_parser("search", help="Search for sharees")
    p_search.add_argument("query", help="Search query (username or display name)")
    p_search.add_argument("--item-type", default="file", help="Item type (default: file)")

    return parser


_COMMANDS = {
    ("capabilities", None): cmd_capabilities,
    ("user", "whoami"): cmd_user_whoami,
    ("user", "search"): cmd_user_search,
    ("user", "get"): cmd_user_get,
    ("user", "groups"): cmd_user_groups,
    ("group", "list"): cmd_group_list,
    ("group", "members"): cmd_group_members,
    ("share", "list"): cmd_share_list,
    ("share", "get"): cmd_share_get,
    ("share", "create"): cmd_share_create,
    ("share", "update"): cmd_share_update,
    ("share", "link"): cmd_share_link,
    ("share", "revoke"): cmd_share_revoke,
    ("share", "delete"): cmd_share_delete,
    ("share", "search"): cmd_share_search,
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    group = getattr(args, "group", None)
    command = getattr(args, "command", None)
    handler = _COMMANDS.get((group, command))
    if handler is None:
        parser.print_help()
        sys.exit(1)

    try:
        result = handler(args)
    except OcsError as e:
        _output(e.to_envelope())
        sys.exit(1)
    except PathScopeError as e:
        _output({"status": "error", "error": str(e)})
        sys.exit(1)
    except Exception as e:
        _output({"status": "error", "error": str(e)})
        sys.exit(1)

    _output(result)

    # A returned error envelope (not a raised exception) still fails the task —
    # matches the module-skill facade convention the scheduler detects.
    if isinstance(result, dict) and result.get("status") == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
