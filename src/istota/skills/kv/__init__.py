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


def _defer_write(operation: str, namespace: str, key: str, value: str | None = None):
    """Write a deferred KV operation for the scheduler to process."""
    deferred_dir = os.environ.get("ISTOTA_DEFERRED_DIR", "")
    task_id = os.environ.get("ISTOTA_TASK_ID", "")
    if not deferred_dir or not task_id:
        return False

    from pathlib import Path

    path = Path(deferred_dir) / f"task_{task_id}_kv_ops.json"

    # Append to existing file if present
    ops = []
    if path.exists():
        try:
            ops = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            ops = []

    entry = {"op": operation, "namespace": namespace, "key": key}
    if value is not None:
        entry["value"] = value
    ops.append(entry)

    path.write_text(json.dumps(ops))
    return True


def cmd_get(args):
    from istota import db

    user_id = _user_id()
    with _get_conn() as conn:
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

    # Try deferred write first (sandbox mode)
    if _defer_write("set", args.namespace, args.key, args.value):
        print(json.dumps({"status": "ok", "deferred": True}))
        return

    # Direct write (outside sandbox)
    from istota import db

    user_id = _user_id()
    with _get_conn() as conn:
        db.kv_set(conn, user_id, args.namespace, args.key, args.value)
    print(json.dumps({"status": "ok"}))


def cmd_delete(args):
    # Try deferred write first (sandbox mode)
    if _defer_write("delete", args.namespace, args.key):
        print(json.dumps({"status": "ok", "deferred": True}))
        return

    # Direct write (outside sandbox)
    from istota import db

    user_id = _user_id()
    with _get_conn() as conn:
        deleted = db.kv_delete(conn, user_id, args.namespace, args.key)
    print(json.dumps({"status": "ok", "deleted": deleted}))


def cmd_list(args):
    from istota import db

    user_id = _user_id()
    with _get_conn() as conn:
        entries = db.kv_list(conn, user_id, args.namespace)
    for entry in entries:
        try:
            entry["value"] = json.loads(entry["value"])
        except json.JSONDecodeError:
            pass
    print(json.dumps({"status": "ok", "count": len(entries), "entries": entries}))


def cmd_namespaces(args):
    from istota import db

    user_id = _user_id()
    with _get_conn() as conn:
        namespaces = db.kv_namespaces(conn, user_id)
    print(json.dumps({"status": "ok", "namespaces": namespaces}))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.kv",
        description="Key-value store for persistent runtime state",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_get = sub.add_parser("get", help="Get a value")
    p_get.add_argument("namespace")
    p_get.add_argument("key")

    p_set = sub.add_parser("set", help="Set a value (JSON)")
    p_set.add_argument("namespace")
    p_set.add_argument("key")
    p_set.add_argument("value", help="JSON value")

    p_del = sub.add_parser("delete", help="Delete a key")
    p_del.add_argument("namespace")
    p_del.add_argument("key")

    p_list = sub.add_parser("list", help="List keys in a namespace")
    p_list.add_argument("namespace")

    sub.add_parser("namespaces", help="List all namespaces")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {
        "get": cmd_get,
        "set": cmd_set,
        "delete": cmd_delete,
        "list": cmd_list,
        "namespaces": cmd_namespaces,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
