"""Click CLI for the briefings module content model (blocks/sources/archive).

Emits JSON to stdout. The :class:`BriefingsContext` is injected via Click's
``obj`` (no env marshaling) — the operator CLI (``istota briefings``) and the
skill facade (``istota-skill briefings``) both resolve the context the istota
way and invoke this tree in-process via ``CliRunner``. Mirrors
:mod:`istota.money.cli` / :mod:`istota.feeds.cli`.
"""

from __future__ import annotations

import json

import click

from istota.briefings import db as bdb
from istota.briefings.models import SOURCE_KINDS, BriefingsContext


def _emit(payload) -> None:
    click.echo(json.dumps(payload))


def _ctx(click_ctx) -> BriefingsContext:
    obj = click_ctx.obj
    if not isinstance(obj, BriefingsContext):
        raise click.ClickException("no briefings context injected")
    return obj


def _block_dict(block) -> dict:
    return {
        "id": block.id,
        "briefing_name": block.briefing_name,
        "position": block.position,
        "title": block.title,
        "directive": block.directive or "",
        "render_mode": block.render_mode,
        "options": block.options or {},
        "sources": [_source_dict(s) for s in block.sources],
    }


def _source_dict(s) -> dict:
    return {
        "id": s.id,
        "position": s.position,
        "kind": s.kind,
        "config": s.config or {},
        "enabled": s.enabled,
    }


@click.group()
@click.pass_context
def cli(ctx):
    """Briefings content management."""


# -- blocks -------------------------------------------------------------------


@cli.group()
def blocks():
    """Manage briefing content blocks."""


@blocks.command("list")
@click.option("--briefing", required=True, help="Briefing name")
@click.pass_context
def blocks_list(ctx, briefing):
    bctx = _ctx(ctx)
    with bdb.connect(bctx.db_path) as conn:
        rows = bdb.list_blocks(conn, briefing)
    _emit({"status": "ok", "blocks": [_block_dict(b) for b in rows]})


@blocks.command("add")
@click.option("--briefing", required=True)
@click.option("--title", required=True)
@click.option("--directive", default=None)
@click.option("--render-mode", "render_mode", default="synthesis")
@click.pass_context
def blocks_add(ctx, briefing, title, directive, render_mode):
    bctx = _ctx(ctx)
    with bdb.connect(bctx.db_path) as conn:
        bid = bdb.add_block(
            conn, briefing_name=briefing, title=title,
            directive=directive, render_mode=render_mode,
        )
        conn.commit()
        block = bdb.get_block(conn, bid)
    _emit({"status": "ok", "block": _block_dict(block)})


@blocks.command("set")
@click.option("--id", "block_id", type=int, required=True)
@click.option("--title", default=None)
@click.option("--directive", default=None)
@click.option("--render-mode", "render_mode", default=None)
@click.option("--options", default=None, help="JSON options object")
@click.pass_context
def blocks_set(ctx, block_id, title, directive, render_mode, options):
    bctx = _ctx(ctx)
    opts = None
    if options is not None:
        try:
            opts = json.loads(options)
        except ValueError as e:
            raise click.ClickException(f"invalid --options JSON: {e}")
    with bdb.connect(bctx.db_path) as conn:
        if not bdb.get_block(conn, block_id, with_sources=False):
            raise click.ClickException("block not found")
        bdb.update_block(
            conn, block_id, title=title, directive=directive,
            render_mode=render_mode, options=opts,
        )
        conn.commit()
        block = bdb.get_block(conn, block_id)
    _emit({"status": "ok", "block": _block_dict(block)})


@blocks.command("remove")
@click.option("--id", "block_id", type=int, required=True)
@click.pass_context
def blocks_remove(ctx, block_id):
    bctx = _ctx(ctx)
    with bdb.connect(bctx.db_path) as conn:
        bdb.delete_block(conn, block_id)
        conn.commit()
    _emit({"status": "ok"})


@blocks.command("reorder")
@click.option("--briefing", required=True)
@click.option("--ids", required=True, help="Comma-separated block ids in the new order")
@click.pass_context
def blocks_reorder(ctx, briefing, ids):
    bctx = _ctx(ctx)
    ordered = [int(x) for x in ids.split(",") if x.strip()]
    with bdb.connect(bctx.db_path) as conn:
        bdb.reorder_blocks(conn, briefing, ordered)
        conn.commit()
    _emit({"status": "ok", "reordered": len(ordered)})


# -- sources ------------------------------------------------------------------


@cli.group()
def sources():
    """Manage a block's sources."""


@sources.command("list")
@click.option("--block", "block_id", type=int, required=True)
@click.pass_context
def sources_list(ctx, block_id):
    bctx = _ctx(ctx)
    with bdb.connect(bctx.db_path) as conn:
        rows = bdb.list_sources(conn, block_id)
    _emit({"status": "ok", "sources": [_source_dict(s) for s in rows]})


@sources.command("add")
@click.option("--block", "block_id", type=int, required=True)
@click.option("--kind", required=True)
@click.option("--config", "config_json", default=None, help="JSON config object")
@click.pass_context
def sources_add(ctx, block_id, kind, config_json):
    bctx = _ctx(ctx)
    if kind not in SOURCE_KINDS:
        raise click.ClickException(f"invalid kind '{kind}' (one of {', '.join(SOURCE_KINDS)})")
    cfg = {}
    if config_json:
        try:
            cfg = json.loads(config_json)
        except ValueError as e:
            raise click.ClickException(f"invalid --config JSON: {e}")
    with bdb.connect(bctx.db_path) as conn:
        if not bdb.get_block(conn, block_id, with_sources=False):
            raise click.ClickException("block not found")
        sid = bdb.add_source(conn, block_id=block_id, kind=kind, config=cfg)
        conn.commit()
    _emit({"status": "ok", "id": sid})


@sources.command("remove")
@click.option("--id", "source_id", type=int, required=True)
@click.pass_context
def sources_remove(ctx, source_id):
    bctx = _ctx(ctx)
    with bdb.connect(bctx.db_path) as conn:
        bdb.delete_source(conn, source_id)
        conn.commit()
    _emit({"status": "ok"})


# -- archive ------------------------------------------------------------------


@cli.group()
def archive():
    """Read the briefing archive."""


@archive.command("list")
@click.option("--briefing", default=None)
@click.option("--limit", type=int, default=20)
@click.pass_context
def archive_list(ctx, briefing, limit):
    bctx = _ctx(ctx)
    with bdb.connect(bctx.db_path) as conn:
        rows = bdb.list_archive(conn, briefing_name=briefing, limit=limit)
    _emit({
        "status": "ok",
        "items": [
            {
                "id": r.id, "briefing_name": r.briefing_name,
                "subject": r.subject or "", "generated_at": r.generated_at,
            }
            for r in rows
        ],
    })


@archive.command("show")
@click.option("--id", "archive_id", type=int, required=True)
@click.pass_context
def archive_show(ctx, archive_id):
    bctx = _ctx(ctx)
    with bdb.connect(bctx.db_path) as conn:
        row = bdb.get_archived(conn, archive_id)
    if not row:
        raise click.ClickException("archive entry not found")
    _emit({
        "status": "ok",
        "briefing": {
            "id": row.id, "briefing_name": row.briefing_name,
            "subject": row.subject or "", "body_md": row.body_md,
            "generated_at": row.generated_at, "delivered_to": row.delivered_to,
        },
    })
