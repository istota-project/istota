"""Click CLI for the native feeds module.

Operates against a single resolved :class:`FeedsContext`. The skill facade
builds the context up front (via :func:`istota.feeds.resolve_for_user`) and
injects it through ``CliRunner.invoke(obj=...)``; standalone invocation
falls back to ``synthesize_feeds_context`` rooted at ``$FEEDS_WORKSPACE``
or the current working directory.

Output is JSON on stdout, with ``status: ok | error``.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from istota.feeds import db as feeds_db
from istota.feeds._config_io import read_feeds_config, write_feeds_config
from istota.feeds.models import (
    DEFAULT_POLL_INTERVAL_MINUTES,
    FeedsContext,
    detect_source_type,
)
from istota.feeds.workspace import synthesize_feeds_context


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _output(result) -> None:
    click.echo(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if isinstance(result, dict) and result.get("status") == "error":
        sys.exit(1)


def _ok(**kwargs) -> dict:
    return {"status": "ok", **kwargs}


def _err(message: str, **kwargs) -> dict:
    return {"status": "error", "error": message, **kwargs}


# ---------------------------------------------------------------------------
# Context resolution
# ---------------------------------------------------------------------------


pass_ctx = click.make_pass_decorator(FeedsContext, ensure=False)


def _resolve_default_context(user_id: str | None) -> FeedsContext:
    """Build a FeedsContext for standalone CLI use (no skill-side injection)."""
    user_id = user_id or os.environ.get("FEEDS_USER", "") or "default"
    workspace = Path(os.environ.get("FEEDS_WORKSPACE", "")) or Path.cwd()
    if not workspace.is_absolute():
        workspace = workspace.resolve()
    tumblr_key = os.environ.get("TUMBLR_API_KEY", "")
    return synthesize_feeds_context(
        user_id, workspace, tumblr_api_key=tumblr_key,
    )


@click.group()
@click.option("--user", "-u", "user_key", help="User key (defaults to $FEEDS_USER)")
@click.pass_context
def cli(ctx: click.Context, user_key: str | None) -> None:
    """Native feeds — subscriptions, polling, OPML."""
    if isinstance(ctx.obj, FeedsContext):
        return
    fctx = _resolve_default_context(user_key)
    ctx.obj = fctx


# ---------------------------------------------------------------------------
# Sync FEEDS.toml -> SQLite (idempotent)
# ---------------------------------------------------------------------------


def _sync_config_to_db(ctx: FeedsContext) -> dict:
    """Push categories + feeds from FEEDS.toml into the feeds DB.

    Only adds/updates rows. Doesn't remove feeds that disappeared from the
    config — deleting subscriptions goes through ``feeds remove`` so we
    don't clobber a typo'd save. Returns a small summary dict.
    """
    cfg = read_feeds_config(ctx.config_path)
    feeds_db.init_db(ctx.db_path)

    cats_added = 0
    cats_updated = 0
    feeds_added = 0
    feeds_updated = 0
    with feeds_db.connect(ctx.db_path) as conn:
        slug_to_id: dict[str, int] = {}
        for c in cfg.get("categories") or []:
            slug = str(c.get("slug") or "").strip()
            title = str(c.get("title") or slug or "").strip()
            if not slug:
                continue
            existing = feeds_db.get_category_by_slug(conn, slug)
            cat_id = feeds_db.upsert_category(conn, slug, title)
            slug_to_id[slug] = cat_id
            if existing is None:
                cats_added += 1
            elif existing.title != title:
                cats_updated += 1

        default_interval = int(
            cfg.get("settings", {}).get("default_poll_interval_minutes")
            or DEFAULT_POLL_INTERVAL_MINUTES
        )

        for f in cfg.get("feeds") or []:
            url = str(f.get("url") or "").strip()
            if not url:
                continue
            title = f.get("title")
            cat_slug = f.get("category")
            cat_id = slug_to_id.get(cat_slug) if cat_slug else None
            if cat_slug and cat_id is None:
                cat_id = feeds_db.upsert_category(conn, cat_slug, cat_slug)
                slug_to_id[cat_slug] = cat_id
                cats_added += 1
            interval = int(f.get("poll_interval_minutes") or default_interval)
            existing = feeds_db.get_feed_by_url(conn, url)
            feeds_db.upsert_feed(
                conn,
                url=url,
                title=title,
                site_url=f.get("site_url"),
                source_type=detect_source_type(url),
                category_id=cat_id,
                poll_interval_minutes=interval,
            )
            if existing is None:
                feeds_added += 1
            else:
                feeds_updated += 1
        conn.commit()

    return {
        "categories_added": cats_added,
        "categories_updated": cats_updated,
        "feeds_added": feeds_added,
        "feeds_updated": feeds_updated,
    }


def _config_data(ctx: FeedsContext) -> dict:
    """Read FEEDS.toml or return the empty default."""
    return read_feeds_config(ctx.config_path)


def _save_config(ctx: FeedsContext, data: dict) -> None:
    write_feeds_config(ctx.config_path, data)


# ---------------------------------------------------------------------------
# list / categories / entries
# ---------------------------------------------------------------------------


@cli.command("list")
@pass_ctx
def cmd_list(ctx: FeedsContext) -> None:
    """List subscribed feeds (DB view, after syncing FEEDS.toml)."""
    _sync_config_to_db(ctx)
    with feeds_db.connect(ctx.db_path) as conn:
        cats = {c.id: c for c in feeds_db.list_categories(conn)}
        feeds = feeds_db.list_feeds(conn)
    rows = [
        {
            "id": f.id,
            "url": f.url,
            "title": f.title,
            "site_url": f.site_url,
            "source_type": f.source_type,
            "category": cats[f.category_id].title if f.category_id and f.category_id in cats else None,
            "category_slug": cats[f.category_id].slug if f.category_id and f.category_id in cats else None,
            "poll_interval_minutes": f.poll_interval_minutes,
            "last_fetched_at": f.last_fetched_at,
            "last_error": f.last_error,
            "error_count": f.error_count,
            "next_poll_at": f.next_poll_at,
        }
        for f in feeds
    ]
    _output({"status": "ok", "count": len(rows), "feeds": rows})


@cli.command("categories")
@pass_ctx
def cmd_categories(ctx: FeedsContext) -> None:
    """List categories from FEEDS.toml (synced into the DB)."""
    _sync_config_to_db(ctx)
    with feeds_db.connect(ctx.db_path) as conn:
        cats = feeds_db.list_categories(conn)
    rows = [{"id": c.id, "slug": c.slug, "title": c.title} for c in cats]
    _output({"status": "ok", "count": len(rows), "categories": rows})


@cli.command("entries")
@click.option("--status", type=click.Choice(["unread", "read", "removed"]), help="Filter by status")
@click.option("--feed-id", type=int, help="Filter by feed id")
@click.option("--category-id", type=int, help="Filter by category id")
@click.option("--category", help="Filter by category slug")
@click.option("--limit", type=int, default=25, show_default=True)
@click.option("--offset", type=int, default=0, show_default=True)
@click.option("--before", type=int, help="Only entries with published_at < this Unix ts")
@click.option("--order", type=click.Choice(["published_at", "created_at", "id"]), default="published_at", show_default=True)
@click.option("--direction", type=click.Choice(["asc", "desc"]), default="desc", show_default=True)
@pass_ctx
def cmd_entries(
    ctx: FeedsContext, status, feed_id, category_id, category, limit, offset,
    before, order, direction,
) -> None:
    """List entries with the same filter knobs as the HTTP API."""
    feeds_db.init_db(ctx.db_path)
    with feeds_db.connect(ctx.db_path) as conn:
        if category and category_id is None:
            cat = feeds_db.get_category_by_slug(conn, category)
            if cat is not None:
                category_id = cat.id
        entries = feeds_db.list_entries(
            conn,
            limit=limit,
            offset=offset,
            status=status,
            feed_id=feed_id,
            category_id=category_id,
            before_published_ts=before,
            order=order,
            direction=direction,
        )
        total = feeds_db.count_entries(
            conn, status=status, feed_id=feed_id, category_id=category_id,
        )

    rows = [
        {
            "id": e.id,
            "feed_id": e.feed_id,
            "guid": e.guid,
            "title": e.title,
            "url": e.url,
            "author": e.author,
            "image_urls": e.image_urls,
            "published_at": e.published_at,
            "fetched_at": e.fetched_at,
            "status": e.status,
        }
        for e in entries
    ]
    _output({"status": "ok", "total": total, "count": len(rows), "entries": rows})


# ---------------------------------------------------------------------------
# add / remove (mutate FEEDS.toml + sync)
# ---------------------------------------------------------------------------


@cli.command("add")
@click.option("--url", required=True, help="Feed URL or tumblr:/arena: identifier")
@click.option("--title", help="Display title")
@click.option("--category", help="Category slug (creates if missing)")
@click.option("--poll-interval-minutes", type=int, help="Override per-feed poll interval")
@pass_ctx
def cmd_add(ctx: FeedsContext, url, title, category, poll_interval_minutes) -> None:
    """Add a feed by writing FEEDS.toml then syncing into the DB."""
    cfg = _config_data(ctx)
    feeds = cfg.setdefault("feeds", [])
    cats = cfg.setdefault("categories", [])

    for f in feeds:
        if str(f.get("url")) == url:
            _output(_err(f"feed already exists: {url}"))
            return

    if category:
        if not any(str(c.get("slug")) == category for c in cats):
            cats.append({"slug": category, "title": category})

    new_feed: dict = {"url": url}
    if title:
        new_feed["title"] = title
    if category:
        new_feed["category"] = category
    if poll_interval_minutes is not None:
        new_feed["poll_interval_minutes"] = poll_interval_minutes
    feeds.append(new_feed)
    _save_config(ctx, cfg)

    summary = _sync_config_to_db(ctx)
    _output(_ok(feed=new_feed, sync=summary))


@cli.command("remove")
@click.option("--url", help="Feed URL to unsubscribe")
@click.option("--id", "feed_id", type=int, help="DB feed id")
@pass_ctx
def cmd_remove(ctx: FeedsContext, url, feed_id) -> None:
    """Unsubscribe by URL or DB id. Removes the row from FEEDS.toml and DB."""
    if not url and not feed_id:
        _output(_err("specify --url or --id"))
        return

    if feed_id and not url:
        feeds_db.init_db(ctx.db_path)
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT url FROM feeds WHERE id = ?", (feed_id,),
            ).fetchone()
            if row is None:
                _output(_err(f"no feed with id {feed_id}"))
                return
            url = row["url"]

    cfg = _config_data(ctx)
    before = len(cfg.get("feeds") or [])
    cfg["feeds"] = [f for f in cfg.get("feeds") or [] if str(f.get("url")) != url]
    removed_from_config = before - len(cfg["feeds"])
    _save_config(ctx, cfg)

    feeds_db.init_db(ctx.db_path)
    with feeds_db.connect(ctx.db_path) as conn:
        feeds_db.delete_feed(conn, url)
        conn.commit()

    _output(_ok(removed_url=url, removed_from_config=removed_from_config))


# ---------------------------------------------------------------------------
# refresh / poll / run-scheduled
# ---------------------------------------------------------------------------


@cli.command("refresh")
@click.option("--id", "feed_id", type=int, help="Feed id (omit to mark all due)")
@pass_ctx
def cmd_refresh(ctx: FeedsContext, feed_id) -> None:
    """Mark feeds due for the next poll cycle by clearing ``next_poll_at``."""
    feeds_db.init_db(ctx.db_path)
    with feeds_db.connect(ctx.db_path) as conn:
        if feed_id:
            cur = conn.execute(
                "UPDATE feeds SET next_poll_at = NULL WHERE id = ?", (feed_id,),
            )
        else:
            cur = conn.execute("UPDATE feeds SET next_poll_at = NULL")
        conn.commit()
    _output(_ok(reset_count=cur.rowcount))


@cli.command("poll")
@click.option("--limit", type=int, help="Cap how many feeds to poll this run")
@pass_ctx
def cmd_poll(ctx: FeedsContext, limit) -> None:
    """Poll every feed whose ``next_poll_at`` is in the past."""
    from istota.feeds.poller import poll_due_feeds

    _sync_config_to_db(ctx)
    api_key = ctx.tumblr_api_key or os.environ.get("TUMBLR_API_KEY", "")

    feeds_db.init_db(ctx.db_path)
    with feeds_db.connect(ctx.db_path) as conn:
        outcomes = poll_due_feeds(
            conn, tumblr_api_key=api_key, limit=limit,
            now=datetime.now(timezone.utc),
        )

    summary = []
    new_total = 0
    error_total = 0
    for feed, result, new_count in outcomes:
        new_total += new_count
        if result.error:
            error_total += 1
        summary.append({
            "feed_id": feed.id,
            "url": feed.url,
            "new_entries": new_count,
            "not_modified": result.not_modified,
            "error": result.error,
        })
    _output(_ok(
        polled=len(outcomes),
        new_entries=new_total,
        errors=error_total,
        feeds=summary,
    ))


@cli.command("run-scheduled")
@click.option("--limit", type=int, help="Cap how many feeds to poll this run")
@pass_ctx
def cmd_run_scheduled(ctx: FeedsContext, limit) -> None:
    """Periodic entry point used by the scheduler module-job."""
    cmd_poll.callback(ctx=ctx, limit=limit)


# ---------------------------------------------------------------------------
# OPML
# ---------------------------------------------------------------------------


@cli.command("import-opml")
@click.argument("opml_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--write-config/--no-write-config", default=True,
              help="After importing into the DB, regenerate FEEDS.toml from the DB")
@pass_ctx
def cmd_import_opml(ctx: FeedsContext, opml_path, write_config) -> None:
    """Import subscriptions from an OPML file (Miniflux export, etc.)."""
    from istota.feeds.opml import import_opml

    text = Path(opml_path).read_text()
    result = import_opml(ctx, text)

    if write_config:
        _dump_db_to_config(ctx)

    _output(_ok(
        feeds_added=result.feeds_added,
        feeds_updated=result.feeds_updated,
        feeds_skipped=result.feeds_skipped,
        categories_added=result.categories_added,
        rewritten_bridger_urls=result.rewritten_bridger_urls,
        wrote_config=write_config,
    ))


@cli.command("export-opml")
@click.option("--output", "-o", type=click.Path(dir_okay=False), help="Write to file (default: stdout)")
@pass_ctx
def cmd_export_opml(ctx: FeedsContext, output) -> None:
    """Export subscriptions as OPML 2.0."""
    from istota.feeds.opml import export_opml

    text = export_opml(ctx)
    if output:
        Path(output).write_text(text)
        _output(_ok(path=str(output), bytes=len(text)))
    else:
        # Raw OPML on stdout — caller asked for it; bypass JSON wrapping.
        click.echo(text)


def _dump_db_to_config(ctx: FeedsContext) -> None:
    """Project the DB (which OPML import populated) back to FEEDS.toml."""
    feeds_db.init_db(ctx.db_path)
    with feeds_db.connect(ctx.db_path) as conn:
        cats = feeds_db.list_categories(conn)
        feeds = feeds_db.list_feeds(conn)
    cat_by_id = {c.id: c for c in cats}
    data: dict = {
        "settings": {"default_poll_interval_minutes": DEFAULT_POLL_INTERVAL_MINUTES},
        "categories": [{"slug": c.slug, "title": c.title} for c in cats],
        "feeds": [],
    }
    for f in feeds:
        entry: dict = {"url": f.url}
        if f.title:
            entry["title"] = f.title
        if f.category_id and f.category_id in cat_by_id:
            entry["category"] = cat_by_id[f.category_id].slug
        if f.poll_interval_minutes != DEFAULT_POLL_INTERVAL_MINUTES:
            entry["poll_interval_minutes"] = f.poll_interval_minutes
        data["feeds"].append(entry)
    _save_config(ctx, data)


if __name__ == "__main__":
    cli()
