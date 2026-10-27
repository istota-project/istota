"""Click CLI for the briefings module content model (blocks/sources/archive).

Emits JSON to stdout. A refusal is ``{"status": "error", "reason": <code>,
"error": <text>}`` at exit 1 (:func:`_fail`). Reason codes:

- ``not_found``: the id or name does not exist for this user.
- ``invalid_argument``: a flag value failed parsing (bad JSON, bad id list,
  unknown kind, conflicting flags).
- ``invalid_definition``: ``save_block_definition``'s validator refused the
  result; the message is the validator's.
- ``unavailable``: the tree was invoked without the context it needs.

``blocks list`` for an unknown briefing still answers ``status: not_found`` at
exit 0, now with ``reason: not_found``, for one release. The :class:`BriefingsContext` is injected via Click's
``obj`` (no env marshaling) — the operator CLI (``istota briefings``) and the
skill facade (``istota-skill briefings``) both resolve the context the istota
way and invoke this tree in-process via ``CliRunner``. Mirrors
:mod:`istota.money.cli` / :mod:`istota.feeds.cli`.
"""

from __future__ import annotations

import json
import sys
from typing import NoReturn

import click

from istota.briefings import db as bdb
from istota.briefings.models import SOURCE_KINDS, BriefingsContext


def _emit(payload) -> None:
    click.echo(json.dumps(payload))


def _fail(reason: str, message: str, **extra) -> NoReturn:
    _emit({"status": "error", "reason": reason, "error": message, **extra})
    # `sys.exit`, not `click.exceptions.Exit`: both callers invoke this tree with
    # `standalone_mode=False`, where Click returns an `Exit` code instead of
    # raising it and `CliRunner` then reports exit 0.
    sys.exit(1)


def _ctx(click_ctx) -> BriefingsContext:
    obj = click_ctx.obj
    if not isinstance(obj, BriefingsContext):
        _fail("unavailable", "no briefings context injected")
    return obj


def _json_object(flag: str, raw: str) -> dict:
    try:
        value = json.loads(raw)
    except ValueError as e:
        _fail("invalid_argument", f"invalid {flag} JSON: {e}")
    if not isinstance(value, dict):
        _fail("invalid_argument", f"{flag} must be a JSON object")
    return value


def _merge_options(stored: dict, patch: dict) -> dict:
    merged = {**stored, **patch}
    if isinstance(stored.get("presentation"), dict) and isinstance(patch.get("presentation"), dict):
        merged["presentation"] = {**stored["presentation"], **patch["presentation"]}
    return merged


def _save_block(bctx: BriefingsContext, payload: dict) -> None:
    with bdb.connect(bctx.db_path) as conn:
        try:
            block = bdb.save_block_definition(conn, payload=payload)
        except LookupError:
            _fail("not_found", "block not found")
        except ValueError as e:
            _fail("invalid_definition", str(e))
        conn.commit()
    _emit({"status": "ok", "block": _block_dict(block)})


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


def _available_briefing_names(
    bctx: BriefingsContext, conn,
) -> list[str]:
    names = set(bdb.list_briefing_names(conn))
    names.update(bctx.configured_briefing_names)
    return sorted(names, key=lambda name: (name.casefold(), name))


@click.group()
@click.pass_context
def cli(ctx):
    """Briefings content management."""


@cli.command("list")
@click.pass_context
def briefings_list(ctx):
    """List briefing names and their content status."""
    bctx = _ctx(ctx)
    with bdb.connect(bctx.db_path) as conn:
        summaries_by_name = {
            row["name"]: row for row in bdb.list_briefing_summaries(conn)
        }
        available = _available_briefing_names(bctx, conn)
        for name in available:
            if name in summaries_by_name:
                continue
            latest = bdb.latest_archived(conn, briefing_name=name)
            summaries_by_name[name] = {
                "name": name,
                "block_count": 0,
                "last_generated_at": latest.generated_at if latest else None,
            }
    summaries = [summaries_by_name[name] for name in available]
    _emit({"status": "ok", "briefings": summaries})


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
        available = _available_briefing_names(bctx, conn)
    if briefing not in available:
        _emit({
            "status": "not_found",
            "reason": "not_found",
            "error": f"no briefing named '{briefing}'",
            "available": available,
        })
        return
    _emit({"status": "ok", "blocks": [_block_dict(b) for b in rows]})


@blocks.command("add")
@click.option("--briefing", required=True)
@click.option("--title", required=True)
@click.option("--directive", default=None)
@click.option("--render-mode", "render_mode", default="synthesis")
@click.option("--options", default=None, help="JSON options object")
@click.pass_context
def blocks_add(ctx, briefing, title, directive, render_mode, options):
    bctx = _ctx(ctx)
    payload = {
        "briefing_name": briefing, "title": title,
        "directive": directive, "render_mode": render_mode,
        "options": _json_object("--options", options) if options is not None else {},
    }
    _save_block(bctx, payload)


@blocks.command("set")
@click.option("--id", "block_id", type=int, required=True)
@click.option("--title", default=None)
@click.option("--directive", default=None)
@click.option("--render-mode", "render_mode", default=None)
@click.option("--options", default=None, help="JSON options object (replaces the stored one)")
@click.option(
    "--merge-options", "merge_options", default=None,
    help="JSON object merged over the stored options, presentation merged too",
)
@click.pass_context
def blocks_set(ctx, block_id, title, directive, render_mode, options, merge_options):
    bctx = _ctx(ctx)
    if options is not None and merge_options is not None:
        _fail("invalid_argument", "--options and --merge-options are mutually exclusive")
    payload: dict = {"id": block_id}
    if title is not None:
        payload["title"] = title
    if directive is not None:
        payload["directive"] = directive
    if render_mode is not None:
        payload["render_mode"] = render_mode
    if options is not None:
        payload["options"] = _json_object("--options", options)
    if merge_options is not None:
        patch = _json_object("--merge-options", merge_options)
        with bdb.connect(bctx.db_path) as conn:
            stored = bdb.get_block(conn, block_id, with_sources=False)
        if stored is None:
            _fail("not_found", "block not found")
        payload["options"] = _merge_options(stored.options or {}, patch)
    _save_block(bctx, payload)


@blocks.command("remove")
@click.option("--id", "block_id", type=int, required=True)
@click.pass_context
def blocks_remove(ctx, block_id):
    bctx = _ctx(ctx)
    with bdb.connect(bctx.db_path) as conn:
        if not bdb.get_block(conn, block_id, with_sources=False):
            _fail("not_found", "block not found")
        bdb.delete_block(conn, block_id)
        conn.commit()
    _emit({"status": "ok"})


@blocks.command("reorder")
@click.option("--briefing", required=True)
@click.option("--ids", required=True, help="Comma-separated block ids in the new order")
@click.pass_context
def blocks_reorder(ctx, briefing, ids):
    bctx = _ctx(ctx)
    try:
        ordered = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        _fail("invalid_argument", "--ids must be a comma-separated list of block ids")
    with bdb.connect(bctx.db_path) as conn:
        try:
            bdb.reorder_blocks(conn, briefing, ordered)
        except ValueError as e:
            _fail("invalid_argument", str(e))
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
        if not bdb.get_block(conn, block_id, with_sources=False):
            _fail("not_found", "block not found")
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
        _fail("invalid_argument", f"invalid kind '{kind}' (one of {', '.join(SOURCE_KINDS)})")
    cfg = {}
    if config_json:
        try:
            cfg = json.loads(config_json)
        except ValueError as e:
            _fail("invalid_argument", f"invalid --config JSON: {e}")
    with bdb.connect(bctx.db_path) as conn:
        if not bdb.get_block(conn, block_id, with_sources=False):
            _fail("not_found", "block not found")
        sid = bdb.add_source(conn, block_id=block_id, kind=kind, config=cfg)
        conn.commit()
    _emit({"status": "ok", "id": sid})


@sources.command("remove")
@click.option("--id", "source_id", type=int, required=True)
@click.pass_context
def sources_remove(ctx, source_id):
    bctx = _ctx(ctx)
    with bdb.connect(bctx.db_path) as conn:
        if bdb.get_source(conn, source_id) is None:
            _fail("not_found", "source not found")
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
        _fail("not_found", "archive entry not found")
    _emit({
        "status": "ok",
        "briefing": {
            "id": row.id, "briefing_name": row.briefing_name,
            "subject": row.subject or "", "body_md": row.body_md,
            "generated_at": row.generated_at, "delivered_to": row.delivered_to,
        },
    })
