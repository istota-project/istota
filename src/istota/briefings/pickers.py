"""What the block editor offers to pick for a source, without the web layer.

The picker routes in :mod:`istota.briefings.routes` are one-line delegates to
these functions; the ``briefings`` skill's ``options`` verb and
:mod:`istota.briefings.source_checks` call them directly, so all three read one
answer. Each takes the istota config and the user id explicitly.
"""

from __future__ import annotations

import os
from pathlib import Path

from istota.briefings.sources.browse import BROWSE_PRESETS
from istota.briefings.sources.kv import SHARED_BLOCK_NAMESPACE


def browse_presets() -> list[dict]:
    return [
        {"key": k, "name": v["name"], "url": v["url"]}
        for k, v in BROWSE_PRESETS.items()
    ]


def feed_options(app_config, user_id: str) -> dict:
    """The user's Feeds subscriptions and categories for the RSS source.

    Soft-degrades to empty lists when the Feeds module is off or unavailable.
    """
    subscriptions: list[dict] = []
    categories: list[dict] = []
    try:
        from istota import feeds
        from istota.feeds import db as feeds_db

        fctx = feeds.resolve_for_user(user_id, app_config)
        with feeds_db.connect(fctx.db_path) as conn:
            for f in feeds_db.list_feeds(conn):
                subscriptions.append({
                    "kind": "subscription", "value": f.id,
                    "label": f.title or f.url,
                })
            for c in feeds_db.list_categories(conn):
                categories.append({
                    "kind": "category", "value": c.id, "label": c.title,
                })
    except Exception:  # noqa: BLE001
        pass
    return {
        "available": bool(subscriptions or categories),
        "subscriptions": subscriptions,
        "categories": categories,
    }


def email_accounts(app_config, user_id: str) -> list[dict]:
    """The user's mail accounts a ``my_email`` source can read.

    Always the bot plus-address; Gmail too when Google Workspace is on, marked
    unavailable until the user grants Gmail access; then each of the user's
    enabled connected mail accounts as ``imap:<id>``.
    """
    from istota import db as fdb
    from istota.briefings.sources.my_email import list_accounts

    with fdb.get_db(app_config.db_path) as conn:
        return list_accounts(app_config, conn, user_id)


def shared_block_options(app_config) -> list[dict]:
    """Live pickable shared blocks for the per-user ``shared_block`` source.

    Every live ``shared_kv`` key in the shared-block namespace, tagged
    ``config`` (has a definition row) or ``custom`` (published by a job, no
    definition). A defined block with no content yet is listed too, so a user
    can reference it ahead of its first generation.
    """
    from istota import db as fdb

    options: dict[str, dict] = {}
    with fdb.get_db(app_config.db_path) as conn:
        defined = {r.name for r in fdb.list_shared_block_configs(conn)}
        for r in fdb.list_shared_block_configs(conn):
            options[r.name] = {
                "name": r.name, "updated_at": None, "has_content": False,
                "source": "config",
            }
        for row in fdb.shared_kv_list(conn, SHARED_BLOCK_NAMESPACE):
            key = row["key"]
            options[key] = {
                "name": key,
                "updated_at": row.get("updated_at"),
                "has_content": True,
                "source": "config" if key in defined else "custom",
            }
    return sorted(options.values(), key=lambda o: o["name"])


# File-path picker for todos / reminders / notes sources. The path a user
# types is resolved relative to their own /Users/<uid>/ folder (see
# sources.builtins._resolve_user_path); these let the editor verify a path
# exists and offer candidate text files.
_PATH_SUGGEST_EXTS = (".md", ".txt")
_PATH_SUGGEST_MAX = 50
_PATH_SUGGEST_DEPTH = 6
# Hard cap on candidate files inspected per request, so the query walk stays
# bounded even on a huge workspace where few paths match.
_PATH_SUGGEST_SCAN_MAX = 8000


def _walk_text_files(root: Path, query: str = "") -> list[str]:
    """Bounded walk of ``root`` for ``.md`` / ``.txt`` files matching ``query``.

    Returns paths relative to ``root`` (forward-slashed) — the same form the
    user types into the source path field. When ``query`` is given, only paths
    whose relative form contains it (case-insensitive substring) are returned,
    so a deep or late file still surfaces as the user types; matches whose
    basename hits the query rank ahead of directory-only hits. Bounded by
    depth, a total-scan cap, and the returned-count cap; hidden directories are
    skipped.
    """
    needle = query.strip().lower()
    root_str = str(root)
    scanned = 0
    name_hits: list[str] = []  # query matched the filename
    path_hits: list[str] = []  # query matched only a parent directory
    for dirpath, dirnames, filenames in os.walk(root_str):
        rel_dir = os.path.relpath(dirpath, root_str)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        kept = sorted(d for d in dirnames if not d.startswith("."))
        dirnames[:] = [] if depth >= _PATH_SUGGEST_DEPTH else kept
        for fn in sorted(filenames):
            if not fn.lower().endswith(_PATH_SUGGEST_EXTS):
                continue
            scanned += 1
            rel = os.path.relpath(os.path.join(dirpath, fn), root_str).replace(os.sep, "/")
            if not needle:
                name_hits.append(rel)
            elif needle in fn.lower():
                name_hits.append(rel)
            elif needle in rel.lower():
                path_hits.append(rel)
            if len(name_hits) >= _PATH_SUGGEST_MAX or scanned >= _PATH_SUGGEST_SCAN_MAX:
                return (name_hits + path_hits)[:_PATH_SUGGEST_MAX]
    return (name_hits + path_hits)[:_PATH_SUGGEST_MAX]


def path_suggestions(app_config, user_id: str, q: str = "") -> list[str]:
    """Candidate text-file paths under the user's folder, relative to it.

    ``q`` filters during the walk (a substring of the relative path), so a deep
    file still surfaces rather than being clipped by the bound. Empty without
    a workspace.
    """
    root = app_config.workspace_root(user_id) if app_config is not None else None
    return _walk_text_files(root, q) if root is not None and root.is_dir() else []


def path_check(app_config, user_id: str, path: str) -> dict:
    """Whether a todos/reminders/notes path resolves to an existing file.

    Resolves the candidate exactly as the resolver does (relative to the
    user's own folder). ``{ok: true, resolved}`` or ``{ok: false, error}``.
    """
    from istota.briefings.sources.builtins import _resolve_user_path
    from istota.skills.files import path_exists

    resolved = _resolve_user_path(user_id, path)
    if not resolved:
        return {"ok": False, "error": "Enter a file path."}
    exists = False
    try:
        if app_config is not None and app_config.has_workspace:
            # The resolver's own read (ISSUE-596), so this says yes exactly
            # when the briefing will read the file, and a symlink planted in
            # the user's tree cannot answer whether its target exists.
            from istota.storage import read_owner_text

            text, _reason, _definitive = read_owner_text(app_config, user_id, resolved)
            exists = text is not None
        elif app_config is not None:
            exists = path_exists(app_config, resolved)
    except Exception:  # noqa: BLE001
        exists = False
    if not exists:
        return {"ok": False, "error": "No file found at that path."}
    return {"ok": True, "resolved": resolved}
