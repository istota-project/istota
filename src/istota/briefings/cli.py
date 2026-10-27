"""Click CLI for the briefings module content model (blocks/sources/archive).

Emits JSON to stdout. A refusal is ``{"status": "error", "reason": <code>,
"error": <text>}`` at exit 1 (:func:`_fail`). Reason codes:

- ``not_found``: the id or name does not exist for this user.
- ``invalid_argument``: a flag value failed parsing (bad JSON, bad id list,
  unknown kind, conflicting flags).
- ``invalid_definition``: ``save_block_definition``'s validator refused the
  result; the message is the validator's.
- ``invalid_source``: :mod:`istota.briefings.source_checks` refused a source
  config that could never read anything; ``field`` names the config key.
- ``mail_account_refused``: a ``my_email`` account the user may not pick, with
  the web's message.
- ``unavailable``: the tree was invoked without the context it needs.
- ``host_path_refused``: ``--directive-file`` or ``blocks save --file`` named a
  path outside the task's roots; raised by the ``READ`` stamp and rendered by
  the skill facade. The operator CLI offers neither flag.

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
from istota.briefings import pickers
from istota.briefings.models import (
    BLOCK_TEMPLATES,
    RENDER_MODES,
    SOURCE_KINDS,
    STRUCTURED_KINDS,
    BriefingsContext,
    normalize_block_payload,
)
from istota.briefings.source_checks import SourceRefused, check_source
from istota.skills._hostpath import READ, ReadRefused, click_host_path, read_resolved_text

#: The cap `briefings_config shared-block set-prompt --prompt-file` reads under.
MAX_FILE_BYTES = 256 * 1024


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


def _json_list(flag: str, raw: str) -> list:
    try:
        value = json.loads(raw)
    except ValueError as e:
        _fail("invalid_argument", f"invalid {flag} JSON: {e}")
    if not isinstance(value, list):
        _fail("invalid_argument", f"{flag} must be a JSON list")
    return value


def _read_file(flag: str, path: str) -> str:
    """A file the `READ` stamp already resolved, read with the leaf unfollowed."""
    try:
        return read_resolved_text(path, flag=flag, max_bytes=MAX_FILE_BYTES)
    except ReadRefused as e:
        _fail("invalid_argument", str(e))


def _directive(directive: str | None, directive_file: str | None) -> str | None:
    if directive is not None and directive_file is not None:
        _fail("invalid_argument", "--directive and --directive-file are mutually exclusive")
    if directive_file is None:
        return directive
    text = _read_file("--directive-file", directive_file)
    if not text.strip():
        # `--directive ""` is the way to clear one; an empty file is more likely a mistake.
        _fail("invalid_argument", "--directive-file is empty; pass --directive '' to clear the directive")
    return text


def _check_block_sources(bctx: BriefingsContext, sources: list[dict], existing) -> None:
    """`check_source` over a normalized source list, before anything is written.

    A source kept by id with its kind and config unchanged is not re-judged, the
    rule `sources set` and the web's account check apply to an unchanged value.
    """
    stored_by_id = {s.id: s for s in existing.sources} if existing is not None else {}
    for source in sources:
        stored = stored_by_id.get(source.get("id"))
        if stored is not None and stored.kind != source["kind"]:
            stored = None
        if stored is not None and (stored.config or {}) == source["config"]:
            continue
        _check_source(bctx, source["kind"], source["config"], stored=stored.config if stored else None)


def _save_block(bctx: BriefingsContext, payload: dict) -> None:
    with bdb.connect(bctx.db_path) as conn:
        try:
            values = normalize_block_payload(payload)
        except ValueError as e:
            _fail("invalid_definition", str(e))
        if values.get("sources") is not None:
            existing = None
            if "id" in values:
                existing = bdb.get_block(conn, values["id"])
                if existing is None:
                    _fail("not_found", "block not found")
            _check_block_sources(bctx, values["sources"], existing)
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


@cli.command("kinds")
def briefings_kinds():
    """The source kinds, render modes and block templates the editor offers."""
    _emit({
        "status": "ok",
        "source_kinds": list(SOURCE_KINDS),
        "structured_kinds": list(STRUCTURED_KINDS),
        "render_modes": list(RENDER_MODES),
        "block_templates": BLOCK_TEMPLATES,
    })


_PATH_KINDS = ("todos", "reminders", "notes")


@cli.command("options")
@click.option("--kind", required=True, help="Source kind to list choices for")
@click.option(
    "--q", "query", default="",
    help="For todos/reminders/notes: a substring to filter by, or a path relative "
         "to the user's own folder, which is also checked",
)
@click.pass_context
def briefings_options(ctx, kind, query):
    """What the editor offers to pick for a source of this kind."""
    bctx = _ctx(ctx)
    if kind not in SOURCE_KINDS:
        _fail("invalid_argument", f"invalid kind '{kind}' (one of {', '.join(SOURCE_KINDS)})")
    if kind == "browse":
        _emit({"status": "ok", "kind": kind, "options": pickers.browse_presets()})
        return
    if kind not in ("rss", "my_email", "shared_block", *_PATH_KINDS):
        _emit({"status": "ok", "kind": kind, "options": None, "note": "no options for this kind"})
        return
    cfg = bctx.app_config
    if cfg is None:
        _fail("unavailable", "the istota config is unavailable")
    if kind == "rss":
        options = pickers.feed_options(cfg, bctx.user_id)
    elif kind == "my_email":
        options = pickers.email_accounts(cfg, bctx.user_id)
    elif kind == "shared_block":
        options = pickers.shared_block_options(cfg)
    else:
        options = pickers.path_suggestions(cfg, bctx.user_id, query)
    payload = {"status": "ok", "kind": kind, "options": options}
    if kind in _PATH_KINDS and query.strip():
        payload["check"] = pickers.path_check(cfg, bctx.user_id, query)
    _emit(payload)


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
@click_host_path(READ)
@click.option("--directive-file", "directive_file", default=None, help="Read the directive from this file")
@click.option("--render-mode", "render_mode", default="synthesis")
@click.option("--options", default=None, help="JSON options object")
@click.option(
    "--sources", "sources_json", default=None,
    help='JSON list of sources, [{"kind", "config", "enabled"?}], saved with the block',
)
@click.pass_context
def blocks_add(ctx, briefing, title, directive, directive_file, render_mode, options, sources_json):
    bctx = _ctx(ctx)
    payload = {
        "briefing_name": briefing, "title": title,
        "directive": _directive(directive, directive_file), "render_mode": render_mode,
        "options": _json_object("--options", options) if options is not None else {},
    }
    if sources_json is not None:
        payload["sources"] = _json_list("--sources", sources_json)
    _save_block(bctx, payload)


@blocks.command("save")
@click_host_path(READ)
@click.option(
    "--file", "payload_file", required=True,
    help="JSON file holding the PUT /blocks body: a block, with or without id",
)
@click.pass_context
def blocks_save(ctx, payload_file):
    """Create or update a block and its whole source list from one JSON body."""
    bctx = _ctx(ctx)
    try:
        payload = json.loads(_read_file("--file", payload_file))
    except ValueError as e:
        _fail("invalid_argument", f"invalid --file JSON: {e}")
    if not isinstance(payload, dict):
        _fail("invalid_argument", "--file must hold a JSON object")
    if "reorder" in payload:
        _fail("invalid_argument", "blocks save does not reorder; use blocks reorder")
    _save_block(bctx, payload)


@blocks.command("set")
@click.option("--id", "block_id", type=int, required=True)
@click.option("--title", default=None)
@click.option("--directive", default=None)
@click_host_path(READ)
@click.option("--directive-file", "directive_file", default=None, help="Read the directive from this file")
@click.option("--render-mode", "render_mode", default=None)
@click.option("--options", default=None, help="JSON options object (replaces the stored one)")
@click.option(
    "--merge-options", "merge_options", default=None,
    help="JSON object merged over the stored options, presentation merged too",
)
@click.pass_context
def blocks_set(ctx, block_id, title, directive, directive_file, render_mode, options, merge_options):
    bctx = _ctx(ctx)
    if options is not None and merge_options is not None:
        _fail("invalid_argument", "--options and --merge-options are mutually exclusive")
    directive = _directive(directive, directive_file)
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
    ordered = _id_list(ids)
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


def _check_source(bctx: BriefingsContext, kind: str, config: dict, *, stored=None) -> None:
    try:
        check_source(bctx.app_config, bctx.user_id, kind, config, stored=stored)
    except SourceRefused as e:
        _fail(e.reason, str(e), field=e.field)


def _id_list(raw: str) -> list[int]:
    try:
        return [int(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        _fail("invalid_argument", "--ids must be a comma-separated list of ids")


@sources.command("add")
@click.option("--block", "block_id", type=int, required=True)
@click.option("--kind", required=True)
@click.option("--config", "config_json", default=None, help="JSON config object")
@click.option("--disabled", is_flag=True, default=False, help="Store the source switched off")
@click.pass_context
def sources_add(ctx, block_id, kind, config_json, disabled):
    bctx = _ctx(ctx)
    if kind not in SOURCE_KINDS:
        _fail("invalid_argument", f"invalid kind '{kind}' (one of {', '.join(SOURCE_KINDS)})")
    cfg = _json_object("--config", config_json) if config_json else {}
    with bdb.connect(bctx.db_path) as conn:
        if not bdb.get_block(conn, block_id, with_sources=False):
            _fail("not_found", "block not found")
        _check_source(bctx, kind, cfg)
        sid = bdb.add_source(conn, block_id=block_id, kind=kind, config=cfg, enabled=not disabled)
        conn.commit()
    _emit({"status": "ok", "id": sid})


@sources.command("set")
@click.option("--id", "source_id", type=int, required=True)
@click.option("--config", "config_json", default=None, help="JSON config object (replaces the stored one)")
@click.option(
    "--merge-config", "merge_config", default=None,
    help="JSON object merged over the stored config; a null value deletes that key",
)
@click.option("--enabled/--disabled", "enabled", default=None)
@click.pass_context
def sources_set(ctx, source_id, config_json, merge_config, enabled):
    """Change a source in place; its id and position stay as they are."""
    bctx = _ctx(ctx)
    if config_json is not None and merge_config is not None:
        _fail("invalid_argument", "--config and --merge-config are mutually exclusive")
    if config_json is None and merge_config is None and enabled is None:
        _fail("invalid_argument", "nothing to change: pass --config, --merge-config, --enabled or --disabled")
    replacement = _json_object("--config", config_json) if config_json is not None else None
    patch = _json_object("--merge-config", merge_config) if merge_config is not None else None
    with bdb.connect(bctx.db_path) as conn:
        stored = bdb.get_source(conn, source_id)
        if stored is None:
            _fail("not_found", "source not found")
        config = replacement
        if patch is not None:
            config = {**(stored.config or {}), **patch}
            config = {key: value for key, value in config.items() if value is not None}
        if config is not None:
            _check_source(bctx, stored.kind, config, stored=stored.config)
        bdb.update_source(conn, source_id, config=config, enabled=enabled)
        conn.commit()
        updated = bdb.get_source(conn, source_id)
    _emit({"status": "ok", "source": _source_dict(updated)})


@sources.command("reorder")
@click.option("--block", "block_id", type=int, required=True)
@click.option("--ids", required=True, help="Comma-separated source ids in the new order")
@click.pass_context
def sources_reorder(ctx, block_id, ids):
    bctx = _ctx(ctx)
    ordered = _id_list(ids)
    with bdb.connect(bctx.db_path) as conn:
        if not bdb.get_block(conn, block_id, with_sources=False):
            _fail("not_found", "block not found")
        try:
            bdb.reorder_sources(conn, block_id, ordered)
        except ValueError as e:
            _fail("invalid_argument", str(e))
        conn.commit()
    _emit({"status": "ok", "reordered": len(ordered)})


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
