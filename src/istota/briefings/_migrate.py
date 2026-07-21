"""Initialisation + one-time legacy migration for the briefings module.

``ensure_initialised(ctx, app_config=...)`` runs on first touch (route dependency,
executor, scheduler): it inits the DB (WAL + schema) and, guarded by a
per-briefing ``schema_meta`` sentinel, seeds each of the user's briefings into
the module DB as editable content blocks + sources. A briefing is seeded from
one of two sources, config-authored blocks taking precedence: config-authored
rich ``[[briefings.blocks]]`` (:func:`normalize_block_specs`) when present, else
the legacy ``briefing_configs.components`` translation
(:func:`blocks_from_components`).

Both translations are pure functions so they are unit-testable without a DB. The
seeder never mutates the framework ``briefing_configs`` row — it only seeds the
module DB — and is idempotent: the sentinel makes a re-run a no-op, and it skips
a briefing that already has blocks.
"""

from __future__ import annotations

import logging

from istota.briefings import db as briefings_db
from istota.briefings.models import (
    RENDER_MODES,
    SOURCE_KINDS,
    STRUCTURED_KINDS,
    BriefingsContext,
)


logger = logging.getLogger(__name__)


_MIGRATION_SENTINEL = "migrated_from_components"


# Fixed block order, mirroring the legacy output order
# (NEWS → MARKETS → CALENDAR → TODOS → NOTES → REMINDER). News splits into a
# newsletter ("News", email source) block and a frontpage ("Headlines", browse
# sources) block, both part of the legacy NEWS section.
_BLOCK_ORDER = ("news", "headlines", "markets", "calendar", "todos", "notes", "reminders")


def _component_enabled(components: dict, key: str) -> bool:
    """Permissive enabled check for migration.

    A component is on when its value is ``True`` or a dict that does not
    explicitly set ``enabled: false``. More forgiving than the legacy
    ``_component_enabled`` (which required an explicit ``enabled`` key on dicts)
    so a ``news = {sources = [...]}`` block migrates rather than silently
    vanishing. Best-effort — the legacy path remains the fallback for anything
    unmigrated.
    """
    value = components.get(key)
    if value is True:
        return True
    if isinstance(value, dict):
        return bool(value.get("enabled", True))
    return False


def _sender_to_pattern(source: str) -> str:
    """Convert a legacy ``news.sources`` entry to an fnmatch sender pattern.

    An email address (contains ``@``) is used literally; a bare domain becomes
    ``*@domain``. Already-globbed entries pass through untouched.
    """
    source = source.strip()
    if not source:
        return ""
    if "@" in source or "*" in source:
        return source
    return f"*@{source}"


def blocks_from_components(components: dict) -> list[dict]:
    """Translate a legacy ``components`` blob into ordered block specs.

    Returns a list of ``{"title", "directive", "render_mode", "options",
    "sources": [{"kind", "config"}]}`` dicts in the fixed legacy order. Pure —
    no DB access — so it is directly unit-testable. The dead ``email`` component
    (general unread-mail summary) is dropped.
    """
    if not isinstance(components, dict):
        return []

    specs: list[dict] = []

    for key in _BLOCK_ORDER:
        if not _component_enabled(components, key):
            continue
        value = components.get(key)
        cfg = value if isinstance(value, dict) else {}

        if key == "news":
            sources_list = cfg.get("sources") or []
            lookback = cfg.get("lookback_hours", 12)
            if sources_list:
                patterns = [
                    p for p in (_sender_to_pattern(s) for s in sources_list) if p
                ]
                source_cfg = {"mode": "senders", "senders": patterns,
                              "lookback_hours": lookback}
            else:
                source_cfg = {"mode": "shared", "lookback_hours": lookback}
            specs.append({
                "title": "News",
                "directive": None,
                "render_mode": "synthesis",
                "options": {},
                "sources": [{"kind": "email", "config": source_cfg}],
            })

        elif key == "headlines":
            keys = cfg.get("sources") or []
            src = [
                {"kind": "browse", "config": {"preset": k}}
                for k in keys
            ]
            if not src:
                continue
            specs.append({
                "title": "Headlines",
                "directive": None,
                "render_mode": "synthesis",
                "options": {},
                "sources": src,
            })

        elif key == "markets":
            # Carry futures/indices overrides into the markets source config.
            source_cfg = {
                k: v for k, v in cfg.items()
                if k in ("futures", "indices")
            }
            specs.append({
                "title": "Markets",
                "directive": None,
                "render_mode": "structured",
                "options": {},
                "sources": [{"kind": "markets", "config": source_cfg}],
            })

        elif key == "calendar":
            specs.append({
                "title": "Calendar",
                "directive": None,
                "render_mode": "structured",
                "options": {},
                "sources": [{"kind": "calendar", "config": {}}],
            })

        elif key == "todos":
            # No convention path — the user sets the source `path` in the web
            # editor. Seeds empty so the block exists; the source reads
            # nothing until configured.
            specs.append({
                "title": "Todos",
                "directive": None,
                "render_mode": "synthesis",
                "options": {},
                "sources": [{"kind": "todos", "config": {}}],
            })

        elif key == "notes":
            # No convention path — the user sets the source `path` in the web
            # editor (the `notes/` folder convention is prompt guidance only).
            specs.append({
                "title": "Notes",
                "directive": None,
                "render_mode": "synthesis",
                "options": {},
                "sources": [{"kind": "notes", "config": {}}],
            })

        elif key == "reminders":
            # No convention path — the user sets the source `path` in the web
            # editor. Seeds empty so the block exists; the source reads
            # nothing until configured.
            specs.append({
                "title": "Reminder",
                "directive": None,
                "render_mode": "synthesis",
                "options": {},
                "sources": [{"kind": "reminders", "config": {}}],
            })

    return specs


def _normalize_source(raw, *, briefing_name: str) -> dict | None:
    """Coerce one config-authored source to ``{"kind", "config"}`` or drop it.

    Returns ``None`` (and logs a WARNING) for a non-dict entry or an unknown
    ``kind``. ``config`` defaults to ``{}`` when missing or non-dict.
    """
    if not isinstance(raw, dict):
        logger.warning(
            "briefings config blocks: source is not a table in %r; skipping",
            briefing_name,
        )
        return None
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in SOURCE_KINDS:
        logger.warning(
            "briefings config blocks: unknown source kind %r in %r; skipping",
            kind, briefing_name,
        )
        return None
    cfg = raw.get("config")
    if not isinstance(cfg, dict):
        cfg = {}
    return {"kind": kind, "config": cfg}


def normalize_block_specs(raw, *, briefing_name: str = "?") -> list[dict]:
    """Coerce config-authored ``blocks`` into seeder spec dicts (total, fail-soft).

    Produces the same ``{"title", "directive", "render_mode", "options",
    "sources": [{"kind", "config"}]}`` shape :func:`blocks_from_components`
    emits, so ``_seed_blocks`` consumes it unchanged. Pure and unit-testable.

    Best-effort throughout: a non-list input, a non-dict block, a titleless
    block, an unknown source kind, or a block that ends up with zero valid
    sources is skipped with a WARNING — this never raises. ``render_mode``
    defaults by the first source kind (``structured`` for
    :data:`STRUCTURED_KINDS`, else ``synthesis``) when omitted, mirroring the
    legacy components translation.
    """
    if not isinstance(raw, list):
        return []

    specs: list[dict] = []
    for entry in raw:
        try:
            if not isinstance(entry, dict):
                logger.warning(
                    "briefings config blocks: block is not a table in %r; skipping",
                    briefing_name,
                )
                continue

            title = entry.get("title")
            if not isinstance(title, str) or not title.strip():
                logger.warning(
                    "briefings config blocks: block missing title in %r; skipping",
                    briefing_name,
                )
                continue

            sources: list[dict] = []
            for raw_src in entry.get("sources") or []:
                src = _normalize_source(raw_src, briefing_name=briefing_name)
                if src is not None:
                    sources.append(src)
            if not sources:
                logger.warning(
                    "briefings config blocks: block %r in %r has no valid "
                    "sources; skipping", title, briefing_name,
                )
                continue

            directive = entry.get("directive")
            directive = directive if isinstance(directive, str) and directive else None

            render_mode = entry.get("render_mode")
            if render_mode is None:
                first_kind = sources[0]["kind"]
                render_mode = "structured" if first_kind in STRUCTURED_KINDS else "synthesis"
            elif render_mode not in RENDER_MODES:
                logger.warning(
                    "briefings config blocks: unknown render_mode %r for block "
                    "%r in %r; defaulting to synthesis",
                    render_mode, title, briefing_name,
                )
                render_mode = "synthesis"

            options = entry.get("options")
            if not isinstance(options, dict):
                options = {}

            specs.append({
                "title": title,
                "directive": directive,
                "render_mode": render_mode,
                "options": options,
                "sources": sources,
            })
        except Exception as e:  # noqa: BLE001 — never fail init on a bad block
            logger.warning(
                "briefings config blocks: failed to normalize a block in %r: %s",
                briefing_name, e,
            )

    return specs


def _seed_blocks(conn, briefing_name: str, specs: list[dict]) -> None:
    """Write the translated block specs for a briefing (skip if it has any)."""
    existing = briefings_db.list_blocks(conn, briefing_name, with_sources=False)
    if existing:
        return
    for pos, spec in enumerate(specs):
        block_id = briefings_db.add_block(
            conn,
            briefing_name=briefing_name,
            title=spec["title"],
            directive=spec.get("directive"),
            render_mode=spec.get("render_mode", "synthesis"),
            options=spec.get("options") or {},
            position=pos,
        )
        for src in spec.get("sources", []):
            briefings_db.add_source(
                conn,
                block_id=block_id,
                kind=src["kind"],
                config=src.get("config") or {},
            )


def _briefing_sentinel(name: str) -> str:
    """Per-briefing migration sentinel key."""
    return f"{_MIGRATION_SENTINEL}:{name}"


def _migrate_components(ctx: BriefingsContext, app_config) -> None:
    """One-time seed of each briefing's content blocks, tracked **per briefing**.

    Reads the same briefing set that ``check_briefings`` schedules
    (``get_briefings_for_user``) and seeds each briefing exactly once, guarded by
    its own sentinel. Content comes from the briefing's config-authored rich
    ``blocks`` when present, else the legacy ``components`` translation. A per-briefing (not DB-wide) sentinel is
    required so that a briefing created *after* the module DB's first touch is
    still migrated on a later init — a DB-wide sentinel set on an empty first
    touch (e.g. opening the on-by-default Briefings tab before configuring
    anything) would permanently disable migration for every briefing added
    afterward. The per-briefing sentinel also preserves resurrection
    protection: once a briefing is migrated, deleting all its blocks won't
    re-seed them. Best-effort: any error is logged and swallowed so DB init
    never fails.
    """
    if app_config is None:
        return
    try:
        from istota.skills.briefing import get_briefings_for_user
    except Exception:  # noqa: BLE001
        return

    try:
        briefings = get_briefings_for_user(app_config, ctx.user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "briefings migration: could not load briefings for %s: %s",
            ctx.user_id, e,
        )
        return

    with briefings_db.connect(ctx.db_path) as conn:
        for briefing in briefings:
            sentinel = _briefing_sentinel(briefing.name)
            if briefings_db.meta_get(conn, sentinel) == "1":
                continue
            try:
                raw_blocks = getattr(briefing, "blocks", None)
                if isinstance(raw_blocks, list) and raw_blocks:
                    # Config-authored rich blocks win over legacy components.
                    specs = normalize_block_specs(
                        raw_blocks, briefing_name=briefing.name,
                    )
                else:
                    specs = blocks_from_components(briefing.components or {})
                if specs:
                    _seed_blocks(conn, briefing.name, specs)
                briefings_db.meta_set(conn, sentinel, "1")
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "briefings migration: failed for %s/%s: %s",
                    ctx.user_id, getattr(briefing, "name", "?"), e,
                )
        conn.commit()


def ensure_initialised(ctx: BriefingsContext, *, app_config=None) -> None:
    """Init the DB and run the one-time components migration.

    Idempotent and cheap after first call. ``app_config`` is required for the
    migration (to read the user's ``briefing_configs``); omitting it inits the
    DB but skips migration — callers with a config in scope (routes, executor,
    scheduler) pass it.
    """
    ctx.ensure_dirs()
    briefings_db.init_db(ctx.db_path)
    _migrate_components(ctx, app_config)
