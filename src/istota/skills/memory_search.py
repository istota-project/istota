"""Memory search skill — search conversations and memory files.

CLI:
    python -m istota.skills.memory_search search "query" [--limit 10] [--source-type TYPE]
    python -m istota.skills.memory_search index conversation TASK_ID
    python -m istota.skills.memory_search index file PATH [--source-type TYPE]
    python -m istota.skills.memory_search reindex [--lookback-days 90]
    python -m istota.skills.memory_search stats

Env vars: ISTOTA_DB_PATH, ISTOTA_USER_ID, NEXTCLOUD_MOUNT_PATH
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path


def _get_conn() -> sqlite3.Connection:
    """Get DB connection from env var."""
    db_path = os.environ.get("ISTOTA_DB_PATH", "")
    if not db_path:
        print(json.dumps({"status": "error", "error": "ISTOTA_DB_PATH not set"}))
        sys.exit(1)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _get_user_id() -> str:
    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        print(json.dumps({"status": "error", "error": "ISTOTA_USER_ID not set"}))
        sys.exit(1)
    return user_id


def _get_channel_user_ids() -> list[str] | None:
    """Build include_user_ids from ISTOTA_CONVERSATION_TOKEN env var."""
    token = os.environ.get("ISTOTA_CONVERSATION_TOKEN", "")
    if token:
        return [f"channel:{token}"]
    return None


def cmd_search(args) -> dict:
    """Search memory chunks."""
    from istota.memory_search import search

    conn = _get_conn()
    user_id = _get_user_id()

    source_types = [args.source_type] if args.source_type else None
    include_user_ids = _get_channel_user_ids()
    results = search(conn, user_id, args.query, limit=args.limit, source_types=source_types, include_user_ids=include_user_ids)
    conn.close()

    return {
        "status": "ok",
        "query": args.query,
        "count": len(results),
        "results": [
            {
                "chunk_id": r.chunk_id,
                "content": r.content,
                "score": round(r.score, 6),
                "source_type": r.source_type,
                "source_id": r.source_id,
                "bm25_rank": r.bm25_rank,
                "vec_rank": r.vec_rank,
            }
            for r in results
        ],
    }


def cmd_index_conversation(args) -> dict:
    """Index a specific conversation by task ID."""
    from istota.memory_search import index_conversation

    conn = _get_conn()
    user_id = _get_user_id()

    row = conn.execute(
        "SELECT prompt, result FROM tasks WHERE id = ? AND user_id = ?",
        (args.task_id, user_id),
    ).fetchone()

    if not row:
        conn.close()
        return {"status": "error", "error": f"Task {args.task_id} not found for user {user_id}"}

    n = index_conversation(conn, user_id, args.task_id, row[0] or "", row[1] or "")
    conn.close()

    return {"status": "ok", "task_id": args.task_id, "chunks_inserted": n}


def cmd_index_file(args) -> dict:
    """Index a file."""
    from istota.memory_search import index_file

    conn = _get_conn()
    user_id = _get_user_id()

    path = Path(args.path)
    if not path.is_file():
        conn.close()
        return {"status": "error", "error": f"File not found: {path}"}

    content = path.read_text()
    source_type = args.source_type or "memory_file"
    n = index_file(conn, user_id, str(path), content, source_type)
    conn.close()

    return {"status": "ok", "path": str(path), "source_type": source_type, "chunks_inserted": n}


def cmd_reindex(args) -> dict:
    """Reindex all conversations and memory files."""
    from types import SimpleNamespace
    from istota.memory_search import reindex_all

    conn = _get_conn()
    user_id = _get_user_id()

    mount_path = os.environ.get("NEXTCLOUD_MOUNT_PATH", "")
    config = SimpleNamespace(nextcloud_mount_path=Path(mount_path) if mount_path else None)

    stats = reindex_all(conn, config, user_id, lookback_days=args.lookback_days)
    conn.close()

    return {"status": "ok", **stats}


def cmd_stats(args) -> dict:
    """Get memory search stats."""
    from istota.memory_search import get_stats

    conn = _get_conn()
    user_id = _get_user_id()

    include_user_ids = _get_channel_user_ids()
    stats = get_stats(conn, user_id, include_user_ids=include_user_ids)
    conn.close()

    return {"status": "ok", **stats}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.memory_search",
        description="Memory search skill",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search command
    search_p = sub.add_parser("search", help="Search memory chunks")
    search_p.add_argument("query", help="Search query")
    search_p.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")
    search_p.add_argument("--source-type", help="Filter by source type")

    # index command with subcommands
    index_p = sub.add_parser("index", help="Index content")
    index_sub = index_p.add_subparsers(dest="index_command", required=True)

    conv_p = index_sub.add_parser("conversation", help="Index a conversation by task ID")
    conv_p.add_argument("task_id", type=int, help="Task ID to index")

    file_p = index_sub.add_parser("file", help="Index a file")
    file_p.add_argument("path", help="File path")
    file_p.add_argument("--source-type", help="Source type (default: memory_file)")

    # reindex command
    reindex_p = sub.add_parser("reindex", help="Reindex all content")
    reindex_p.add_argument("--lookback-days", type=int, default=90, help="Days to look back (default: 90)")

    # stats command
    sub.add_parser("stats", help="Show memory search stats")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "search": cmd_search,
        "index": lambda a: cmd_index_conversation(a) if a.index_command == "conversation" else cmd_index_file(a),
        "reindex": cmd_reindex,
        "stats": cmd_stats,
    }

    try:
        result = commands[args.command](args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if result.get("status") == "error":
            sys.exit(1)
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
