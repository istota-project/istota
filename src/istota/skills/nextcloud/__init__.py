"""Nextcloud sharing CLI for Claude Code.

Usage:
    python -m istota.skills.nextcloud share list [--path /path]
    python -m istota.skills.nextcloud share create --path /path --type user --with bob [--permissions 31]
    python -m istota.skills.nextcloud share create --path /path --type link [--password X] [--expire YYYY-MM-DD] [--label X]
    python -m istota.skills.nextcloud share delete SHARE_ID
    python -m istota.skills.nextcloud share search QUERY [--item-type file]

Env vars: NC_URL, NC_USER, NC_PASS
"""

import argparse
import json
import os
import sys

from istota.config import Config, NextcloudConfig
from istota.nextcloud_client import (
    ocs_create_public_link,
    ocs_create_share,
    ocs_delete_share,
    ocs_list_shares,
    ocs_search_sharees,
)

_SHARE_TYPE_MAP = {"user": 0, "link": 3, "email": 4}


def _config_from_env() -> Config:
    url = os.environ.get("NC_URL", "")
    user = os.environ.get("NC_USER", "")
    password = os.environ.get("NC_PASS", "")
    if not url or not user or not password:
        print(json.dumps({"error": "NC_URL, NC_USER, NC_PASS env vars required"}), file=sys.stderr)
        sys.exit(1)
    return Config(nextcloud=NextcloudConfig(url=url, username=user, app_password=password))


def _output(data):
    print(json.dumps(data, indent=2, default=str))


def cmd_share_list(args):
    config = _config_from_env()
    shares = ocs_list_shares(config, path=args.path)
    if shares is None:
        print(json.dumps({"error": "Failed to list shares"}), file=sys.stderr)
        sys.exit(1)
    _output(shares)


def cmd_share_create(args):
    config = _config_from_env()
    share_type = _SHARE_TYPE_MAP.get(args.type)
    if share_type is None:
        print(json.dumps({"error": f"Unknown share type: {args.type}. Use: user, link, email"}), file=sys.stderr)
        sys.exit(1)

    if share_type == 3:
        result = ocs_create_public_link(
            config,
            path=args.path,
            permissions=args.permissions or 1,
            password=args.password,
            expire_date=args.expire,
            label=args.label,
        )
    else:
        if not getattr(args, "with_user", None):
            print(json.dumps({"error": "--with is required for user/email shares"}), file=sys.stderr)
            sys.exit(1)
        result = ocs_create_share(
            config,
            path=args.path,
            share_type=share_type,
            share_with=args.with_user,
            permissions=args.permissions,
            password=args.password,
            expire_date=args.expire,
            label=args.label,
        )

    if result is None:
        print(json.dumps({"error": "Failed to create share"}), file=sys.stderr)
        sys.exit(1)
    _output(result)


def cmd_share_delete(args):
    config = _config_from_env()
    ok = ocs_delete_share(config, args.share_id)
    if not ok:
        print(json.dumps({"error": f"Failed to delete share {args.share_id}"}), file=sys.stderr)
        sys.exit(1)
    _output({"status": "deleted", "share_id": args.share_id})


def cmd_share_search(args):
    config = _config_from_env()
    result = ocs_search_sharees(config, args.query, item_type=args.item_type)
    if result is None:
        print(json.dumps({"error": "Failed to search sharees"}), file=sys.stderr)
        sys.exit(1)
    _output(result)


def build_parser():
    parser = argparse.ArgumentParser(description="Nextcloud sharing CLI")
    sub = parser.add_subparsers(dest="group")

    share = sub.add_parser("share", help="Share operations")
    share_sub = share.add_subparsers(dest="command")

    # share list
    p_list = share_sub.add_parser("list", help="List shares")
    p_list.add_argument("--path", default=None, help="Filter by Nextcloud path")

    # share create
    p_create = share_sub.add_parser("create", help="Create a share")
    p_create.add_argument("--path", required=True, help="Nextcloud file/folder path")
    p_create.add_argument("--type", required=True, choices=["user", "link", "email"], help="Share type")
    p_create.add_argument("--with", dest="with_user", help="Username (user) or email (email share)")
    p_create.add_argument("--permissions", type=int, default=None, help="Permission bitmask (1=read, 31=all)")
    p_create.add_argument("--password", default=None, help="Password protection")
    p_create.add_argument("--expire", default=None, help="Expiry date (YYYY-MM-DD)")
    p_create.add_argument("--label", default=None, help="Label for public links")

    # share delete
    p_delete = share_sub.add_parser("delete", help="Delete a share")
    p_delete.add_argument("share_id", type=int, help="Share ID to delete")

    # share search
    p_search = share_sub.add_parser("search", help="Search for sharees")
    p_search.add_argument("query", help="Search query (username or display name)")
    p_search.add_argument("--item-type", default="file", help="Item type (default: file)")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.group != "share":
        parser.print_help()
        sys.exit(1)

    if args.command == "list":
        cmd_share_list(args)
    elif args.command == "create":
        cmd_share_create(args)
    elif args.command == "delete":
        cmd_share_delete(args)
    elif args.command == "search":
        cmd_share_search(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
