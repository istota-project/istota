"""One-shot importer: legacy ``feeds.toml`` → per-user SQLite.

Runs idempotently on first touch from the web routes, the CLI, and the
skill facade (via :func:`ensure_initialised`). After one successful run
all subsequent calls are no-ops gated on a ``schema_meta`` sentinel.

The DB has been the runtime source of truth since the start; the TOML
was just the human-editable seed. This module exists to migrate users
who already have a populated TOML on disk before the cut. The source
file is left in place so an operator can confirm the import landed
(via the ``feeds_legacy_toml_imported`` log line) and delete it.
"""

from __future__ import annotations

import logging
import sqlite3
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from istota.feeds import db as feeds_db
from istota.feeds.models import (
    FeedsContext,
    default_poll_interval_for,
    detect_source_type,
)


logger = logging.getLogger(__name__)


_SENTINEL_KEY = "feeds_legacy_toml_imported_at"
# Re-exported for tests; canonical home is db.py.
_DEFAULT_INTERVAL_SETTING_KEY = feeds_db._DEFAULT_INTERVAL_KEY


def _legacy_toml_candidates(ctx: FeedsContext) -> list[Path]:
    """Ordered list of paths to probe for a legacy ``feeds.toml``.

    Mirrors the search the (now-removed) workspace loader used to do,
    plus an explicit ``ctx.config_path`` if the field is still present
    on the context (commit-1 transition window).
    """
    seen: set[Path] = set()
    out: list[Path] = []

    explicit = getattr(ctx, "config_path", None)
    if explicit is not None:
        out.append(Path(explicit))
        seen.add(Path(explicit))

    # Default workspace layout: data_dir = {workspace}/feeds.
    data_dir_candidate = Path(ctx.data_dir) / "config" / "feeds.toml"
    if data_dir_candidate not in seen:
        out.append(data_dir_candidate)
        seen.add(data_dir_candidate)

    # Workspace-root config dir (a user can colocate feeds.toml with USER.md
    # / CRON.md). data_dir.parent reconstructs the workspace root for the
    # default layout.
    workspace_candidate = Path(ctx.data_dir).parent / "config" / "feeds.toml"
    if workspace_candidate not in seen:
        out.append(workspace_candidate)

    return out


def _find_legacy_toml(ctx: FeedsContext) -> Path | None:
    for candidate in _legacy_toml_candidates(ctx):
        if candidate.is_file():
            return candidate
    return None


def _parse_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        parsed = tomllib.load(fh)
    parsed.setdefault("settings", {})
    parsed.setdefault("categories", [])
    parsed.setdefault("feeds", [])
    return parsed


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def migrate_legacy_toml(ctx: FeedsContext) -> dict | None:
    """Import a legacy ``feeds.toml`` into the per-user feeds DB.

    Returns ``None`` when nothing was done — no toml found, migration
    already ran, or the DB already has feeds from another source. Returns
    a summary dict on a successful import.

    Idempotent and race-safe across processes:

    * A ``schema_meta`` sentinel row (``feeds_legacy_toml_imported_at``)
      records that the migration ran. Subsequent runs see the sentinel
      and bail.
    * The sentinel is inserted via plain ``INSERT`` (PK conflict aborts),
      so only one of N concurrent processes gets to run the upserts.
    * The DB-populated check is a defensive secondary gate so we don't
      stomp on rows added through the web UI / OPML import.

    On success the source ``feeds.toml`` is renamed to ``feeds.toml.imported``
    so an operator can confirm the import landed and `rm` it at leisure.
    """
    toml_path = _find_legacy_toml(ctx)

    feeds_db.init_db(ctx.db_path)

    # Pre-flight: has the migration already run, or does the DB have feeds
    # from another source? Both conditions short-circuit before we parse the
    # file.
    with feeds_db.connect(ctx.db_path) as conn:
        already = conn.execute(
            "SELECT 1 FROM schema_meta WHERE key = ?",
            (_SENTINEL_KEY,),
        ).fetchone()
        if already:
            if toml_path is not None:
                logger.warning(
                    "feeds_legacy_toml_present_but_already_imported path=%s "
                    "— file is no longer read; delete it",
                    toml_path,
                )
            return None
        if toml_path is None:
            return None
        has_feeds = conn.execute(
            "SELECT 1 FROM feeds LIMIT 1",
        ).fetchone()
        if has_feeds:
            logger.warning(
                "feeds_legacy_toml_present_but_db_populated path=%s "
                "— DB already has subscriptions; delete or merge manually",
                toml_path,
            )
            return None

    try:
        parsed = _parse_toml(toml_path)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "feeds_legacy_toml_unparseable path=%s error=%s", toml_path, e,
        )
        return None

    cats_added = 0
    feeds_added = 0
    with feeds_db.connect(ctx.db_path) as conn:
        # Atomic claim — only one concurrent process gets past this insert.
        try:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                (_SENTINEL_KEY, _iso_now()),
            )
        except sqlite3.IntegrityError:
            return None

        slug_to_id: dict[str, int] = {}
        for c in parsed.get("categories") or []:
            slug = str(c.get("slug") or "").strip()
            if not slug:
                continue
            raw_title = str(c.get("title") or "").strip()
            existing_cat = feeds_db.get_category_by_slug(conn, slug)
            if raw_title:
                # Caller provided a real title — upsert it.
                cat_id = feeds_db.upsert_category(conn, slug, raw_title)
            else:
                # No title in TOML; don't stomp an existing one.
                cat_id = feeds_db.ensure_category(conn, slug)
            slug_to_id[slug] = cat_id
            if existing_cat is None:
                cats_added += 1

        explicit_default_raw = parsed.get("settings", {}).get(
            "default_poll_interval_minutes"
        )
        explicit_default: int | None
        try:
            explicit_default = (
                int(explicit_default_raw) if explicit_default_raw else None
            )
        except (TypeError, ValueError):
            explicit_default = None

        for f in parsed.get("feeds") or []:
            url = str(f.get("url") or "").strip()
            if not url:
                continue
            cat_slug = f.get("category")
            cat_id = slug_to_id.get(cat_slug) if cat_slug else None
            if cat_slug and cat_id is None:
                existing_cat = feeds_db.get_category_by_slug(conn, cat_slug)
                cat_id = feeds_db.ensure_category(conn, cat_slug)
                slug_to_id[cat_slug] = cat_id
                if existing_cat is None:
                    cats_added += 1
            source_type = detect_source_type(url)
            per_feed = f.get("poll_interval_minutes")
            if per_feed:
                interval = int(per_feed)
            elif explicit_default is not None:
                interval = explicit_default
            else:
                interval = default_poll_interval_for(source_type)
            existing_feed = feeds_db.get_feed_by_url(conn, url)
            feeds_db.upsert_feed(
                conn,
                url=url,
                title=f.get("title"),
                site_url=f.get("site_url"),
                source_type=source_type,
                category_id=cat_id,
                poll_interval_minutes=interval,
            )
            if existing_feed is None:
                feeds_added += 1

        if explicit_default is not None:
            feeds_db.set_default_poll_interval(conn, explicit_default)

        conn.commit()

    logger.info(
        "feeds_legacy_toml_imported path=%s feeds=%d categories=%d "
        "— file no longer read; safe to delete",
        toml_path, feeds_added, cats_added,
    )
    return {
        "path": str(toml_path),
        "categories_added": cats_added,
        "feeds_added": feeds_added,
    }


def ensure_initialised(ctx: FeedsContext) -> None:
    """Wire up a feeds workspace for use.

    Creates the dirs, runs schema migrations on the SQLite, and (once)
    imports any legacy ``feeds.toml`` into the DB. Safe to call from
    every entry point — web routes, CLI subcommands, skill facade.
    """
    ctx.ensure_dirs()
    feeds_db.init_db(ctx.db_path)
    migrate_legacy_toml(ctx)
