"""Key-value store skill CLI.

Provides get/set/list/delete/namespaces commands against the istota_kv table.
Reads hit the DB directly; writes are deferred when ISTOTA_DEFERRED_DIR is set
(sandbox mode).
"""

import argparse
import json
import os
import sys


def _get_conn():
    from istota import db

    db_path = os.environ.get("ISTOTA_DB_PATH", "")
    if not db_path:
        print(json.dumps({"status": "error", "error": "ISTOTA_DB_PATH not set"}))
        sys.exit(1)
    return db.get_db(db_path)


def _user_id():
    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        print(json.dumps({"status": "error", "error": "ISTOTA_USER_ID not set"}))
        sys.exit(1)
    return user_id


def _defer_op(entry: dict) -> bool:
    """Write a deferred KV operation for the scheduler to process.

    Returns True if the op was queued for deferred processing, False if no
    deferred dir is configured (caller should fall back to direct write).
    """
    deferred_dir = os.environ.get("ISTOTA_DEFERRED_DIR", "")
    task_id = os.environ.get("ISTOTA_TASK_ID", "")
    if not deferred_dir or not task_id:
        return False

    from pathlib import Path

    path = Path(deferred_dir) / f"task_{task_id}_kv_ops.json"

    ops = []
    if path.exists():
        try:
            ops = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            ops = []

    ops.append(entry)
    path.write_text(json.dumps(ops))
    return True


def _defer_write(
    operation: str, namespace: str, key: str, value: str | None = None,
    scope: str | None = None,
):
    entry: dict = {"op": operation, "namespace": namespace, "key": key}
    if value is not None:
        entry["value"] = value
    if scope:
        entry["scope"] = scope
    return _defer_op(entry)


def _load_config():
    """Load config for the direct-write admin gate.

    Reads ISTOTA_CONFIG_PATH (already set for skill subprocesses, like the email
    skill's scoping path). Returns None if config can't be loaded — the caller
    then fails closed on the shared-write gate.
    """
    try:
        from istota.config import load_config

        cfg_path = os.environ.get("ISTOTA_CONFIG_PATH", "")
        from pathlib import Path

        return load_config(Path(cfg_path) if cfg_path else None)
    except Exception:  # noqa: BLE001
        return None


def _shared_write_denied() -> bool:
    """Direct-write gate: print the error envelope + return True when the
    caller may not write shared content. Fail-closed if config can't load."""
    config = _load_config()
    user_id = _user_id()
    if config is not None and config.is_shared_kv_writer(user_id):
        return False
    print(json.dumps({
        "status": "error", "error": "shared KV writes require admin",
    }))
    return True


def _load_set(conn, user_id: str, namespace: str, key: str) -> tuple[list | None, bool]:
    """Load a set-shaped value. Returns (members, exists).

    - If the key doesn't exist: ([], False).
    - If it exists and is a JSON array: (members, True).
    - If it exists but isn't an array: prints error JSON and exits.
    """
    from istota import db

    row = db.kv_get(conn, user_id, namespace, key)
    if row is None:
        return [], False
    try:
        parsed = json.loads(row["value"])
    except json.JSONDecodeError:
        print(json.dumps({"status": "error", "error": f"value at {namespace}/{key} is not valid JSON"}))
        sys.exit(1)
    if not isinstance(parsed, list):
        print(json.dumps({"status": "error", "error": f"value at {namespace}/{key} is not a JSON array"}))
        sys.exit(1)
    return parsed, True


def cmd_get(args):
    from istota import db

    shared = getattr(args, "shared", False)
    user_id = None if shared else _user_id()
    with _get_conn() as conn:
        if shared:
            result = db.shared_kv_get(conn, args.namespace, args.key)
        else:
            result = db.kv_get(conn, user_id, args.namespace, args.key)
    if result is None:
        print(json.dumps({"status": "not_found"}))
    else:
        try:
            value = json.loads(result["value"])
        except json.JSONDecodeError:
            value = result["value"]
        print(json.dumps({"status": "ok", "value": value}))


def cmd_set(args):
    try:
        json.loads(args.value)
    except json.JSONDecodeError:
        print(json.dumps({"status": "error", "error": "invalid JSON value"}))
        sys.exit(1)

    shared = getattr(args, "shared", False)

    # Try deferred write first (sandbox mode). The shared-write gate is applied
    # at apply time against task.user_id (the trusted identity), so the deferred
    # op just carries scope:"shared" — it is not authorized here.
    if _defer_write("set", args.namespace, args.key, args.value,
                    scope="shared" if shared else None):
        print(json.dumps({"status": "ok", "deferred": True}))
        return

    # Direct write (outside sandbox)
    from istota import db

    if shared:
        if _shared_write_denied():
            sys.exit(1)
        with _get_conn() as conn:
            db.shared_kv_set(conn, args.namespace, args.key, args.value, _user_id())
        print(json.dumps({"status": "ok"}))
        return

    user_id = _user_id()
    with _get_conn() as conn:
        db.kv_set(conn, user_id, args.namespace, args.key, args.value)
    print(json.dumps({"status": "ok"}))


def cmd_delete(args):
    shared = getattr(args, "shared", False)

    # Try deferred write first (sandbox mode).
    if _defer_write("delete", args.namespace, args.key,
                    scope="shared" if shared else None):
        print(json.dumps({"status": "ok", "deferred": True}))
        return

    # Direct write (outside sandbox)
    from istota import db

    if shared:
        if _shared_write_denied():
            sys.exit(1)
        with _get_conn() as conn:
            deleted = db.shared_kv_delete(conn, args.namespace, args.key)
        print(json.dumps({"status": "ok", "deleted": deleted}))
        return

    user_id = _user_id()
    with _get_conn() as conn:
        deleted = db.kv_delete(conn, user_id, args.namespace, args.key)
    print(json.dumps({"status": "ok", "deleted": deleted}))


def cmd_list(args):
    from istota import db

    shared = getattr(args, "shared", False)
    with _get_conn() as conn:
        if shared:
            entries = db.shared_kv_list(conn, args.namespace)
        else:
            entries = db.kv_list(conn, _user_id(), args.namespace)
    for entry in entries:
        try:
            entry["value"] = json.loads(entry["value"])
        except json.JSONDecodeError:
            pass
    print(json.dumps({"status": "ok", "count": len(entries), "entries": entries}))


def cmd_namespaces(args):
    from istota import db

    shared = getattr(args, "shared", False)
    with _get_conn() as conn:
        if shared:
            namespaces = db.shared_kv_namespaces(conn)
        else:
            namespaces = db.kv_namespaces(conn, _user_id())
    print(json.dumps({"status": "ok", "namespaces": namespaces}))


def cmd_shared_status(args):
    """Report whether this identity may write shared KV. Pure read.

    The gate is Config.is_shared_kv_writer, which is deliberately asymmetric to
    is_admin: a blank admins file authorizes NOBODY here (fail-closed). So this
    answers the deployment-specific question directly instead of the model
    inferring it from admin status or hunting through config/source.
    """
    user_id = _user_id()
    config = _load_config()
    can_write = bool(config is not None and config.is_shared_kv_writer(user_id))
    admins_configured = bool(config is not None and config.admin_users)
    print(json.dumps({
        "status": "ok",
        "user_id": user_id,
        "can_write_shared": can_write,
        "admins_configured": admins_configured,
    }))


def cmd_set_contains(args):
    user_id = _user_id()
    with _get_conn() as conn:
        members, _ = _load_set(conn, user_id, args.namespace, args.key)
    print(json.dumps({"status": "ok", "contains": args.member in members}))


def cmd_set_size(args):
    user_id = _user_id()
    with _get_conn() as conn:
        members, _ = _load_set(conn, user_id, args.namespace, args.key)
    print(json.dumps({"status": "ok", "size": len(members)}))


def cmd_set_members(args):
    user_id = _user_id()
    with _get_conn() as conn:
        members, _ = _load_set(conn, user_id, args.namespace, args.key)
    offset = max(0, args.offset)
    limit = max(0, args.limit)
    page = members[offset:offset + limit]
    print(json.dumps({
        "status": "ok",
        "total": len(members),
        "offset": offset,
        "members": page,
    }))


def cmd_set_add(args):
    from istota import db

    user_id = _user_id()
    # Read current state to validate set shape and report an `added` count
    # reflecting the read-time view (deferred apply may see a fresher state).
    with _get_conn() as conn:
        current, _ = _load_set(conn, user_id, args.namespace, args.key)
    existing = set(current)
    added = 0
    for m in args.members:
        if m not in existing:
            existing.add(m)
            added += 1

    if _defer_op({
        "op": "set-add",
        "namespace": args.namespace,
        "key": args.key,
        "members": list(args.members),
    }):
        print(json.dumps({"status": "ok", "added": added, "deferred": True}))
        return

    new_members = list(current)
    seen = set(current)
    for m in args.members:
        if m not in seen:
            new_members.append(m)
            seen.add(m)
    with _get_conn() as conn:
        db.kv_set(conn, user_id, args.namespace, args.key, json.dumps(new_members))
    print(json.dumps({"status": "ok", "added": added}))


def cmd_set_remove(args):
    from istota import db

    user_id = _user_id()
    with _get_conn() as conn:
        current, _ = _load_set(conn, user_id, args.namespace, args.key)
    to_remove = set(args.members)
    removed = sum(1 for m in current if m in to_remove)

    if _defer_op({
        "op": "set-remove",
        "namespace": args.namespace,
        "key": args.key,
        "members": list(args.members),
    }):
        print(json.dumps({"status": "ok", "removed": removed, "deferred": True}))
        return

    new_members = [m for m in current if m not in to_remove]
    with _get_conn() as conn:
        db.kv_set(conn, user_id, args.namespace, args.key, json.dumps(new_members))
    print(json.dumps({"status": "ok", "removed": removed}))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.kv",
        description="Key-value store for persistent runtime state",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    _shared_help = "Use the cross-user shared_kv store (writes admin-only)"

    p_get = sub.add_parser("get", help="Get a value")
    p_get.add_argument("namespace")
    p_get.add_argument("key")
    p_get.add_argument("--shared", action="store_true", help=_shared_help)

    p_set = sub.add_parser("set", help="Set a value (JSON)")
    p_set.add_argument("namespace")
    p_set.add_argument("key")
    p_set.add_argument("value", help="JSON value")
    p_set.add_argument("--shared", action="store_true", help=_shared_help)

    p_del = sub.add_parser("delete", help="Delete a key")
    p_del.add_argument("namespace")
    p_del.add_argument("key")
    p_del.add_argument("--shared", action="store_true", help=_shared_help)

    p_list = sub.add_parser("list", help="List keys in a namespace")
    p_list.add_argument("namespace")
    p_list.add_argument("--shared", action="store_true", help=_shared_help)

    p_ns = sub.add_parser("namespaces", help="List all namespaces")
    p_ns.add_argument("--shared", action="store_true", help=_shared_help)

    sub.add_parser(
        "shared-status",
        help="Report whether you may write shared_kv (admin-only, deployment-specific)",
    )

    # Set-ops accept --shared only so we can reject it with a clean JSON error
    # (curated shared content is whole-value writes, not incremental set
    # membership). Without the flag argparse would exit 2 with a stderr message.
    p_contains = sub.add_parser(
        "set-contains",
        help="Check if a string member is in the JSON-array value at <ns>/<key>",
    )
    p_contains.add_argument("namespace")
    p_contains.add_argument("key")
    p_contains.add_argument("member")
    p_contains.add_argument("--shared", action="store_true", help=argparse.SUPPRESS)

    p_size = sub.add_parser(
        "set-size",
        help="Return the number of members in the JSON-array value at <ns>/<key>",
    )
    p_size.add_argument("namespace")
    p_size.add_argument("key")
    p_size.add_argument("--shared", action="store_true", help=argparse.SUPPRESS)

    p_members = sub.add_parser(
        "set-members",
        help="Return a paginated slice of members in the JSON-array value at <ns>/<key>",
    )
    p_members.add_argument("namespace")
    p_members.add_argument("key")
    p_members.add_argument("--limit", type=int, default=100)
    p_members.add_argument("--offset", type=int, default=0)
    p_members.add_argument("--shared", action="store_true", help=argparse.SUPPRESS)

    p_add = sub.add_parser(
        "set-add",
        help="Add one or more string members to the JSON-array value at <ns>/<key>",
    )
    p_add.add_argument("namespace")
    p_add.add_argument("key")
    p_add.add_argument("members", nargs="+")
    p_add.add_argument("--shared", action="store_true", help=argparse.SUPPRESS)

    p_remove = sub.add_parser(
        "set-remove",
        help="Remove one or more string members from the JSON-array value at <ns>/<key>",
    )
    p_remove.add_argument("namespace")
    p_remove.add_argument("key")
    p_remove.add_argument("members", nargs="+")
    p_remove.add_argument("--shared", action="store_true", help=argparse.SUPPRESS)

    return parser


_SET_OPS = frozenset({
    "set-contains", "set-size", "set-members", "set-add", "set-remove",
})


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in _SET_OPS and getattr(args, "shared", False):
        print(json.dumps({
            "status": "error",
            "error": "--shared is not supported for set-ops "
                     "(shared scope is whole-value only)",
        }))
        sys.exit(1)
    commands = {
        "get": cmd_get,
        "set": cmd_set,
        "delete": cmd_delete,
        "list": cmd_list,
        "namespaces": cmd_namespaces,
        "shared-status": cmd_shared_status,
        "set-contains": cmd_set_contains,
        "set-size": cmd_set_size,
        "set-members": cmd_set_members,
        "set-add": cmd_set_add,
        "set-remove": cmd_set_remove,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
