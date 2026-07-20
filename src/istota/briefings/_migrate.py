"""Initialisation + one-time legacy migration for the briefings module.

``ensure_initialised(ctx, app_config=...)`` runs on first touch (route dependency,
executor, scheduler): it inits the DB (WAL + schema) and, guarded by a DB-wide
``schema_meta`` sentinel, translates each of the user's existing
``briefing_configs.components`` blobs into equivalent content blocks + sources.

The translation is a pure function (:func:`blocks_from_components`) so it is
unit-testable without a DB. The migration never mutates the framework
``briefing_configs`` row — it only seeds the module DB — and is idempotent: the
sentinel makes a re-run a no-op, and it skips a briefing that already has blocks.
"""

from __future__ import annotations

import logging

from istota.briefings import db as briefings_db
from istota.briefings.models import BriefingsContext


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
            specs.append({
                "title": "Todos",
                "directive": None,
                "render_mode": "synthesis",
                "options": {},
                "sources": [{"kind": "todos", "config": {}}],
            })

        elif key == "notes":
            specs.append({
                "title": "Notes",
                "directive": None,
                "render_mode": "synthesis",
                "options": {},
                "sources": [{"kind": "notes", "config": {}}],
            })

        elif key == "reminders":
            specs.append({
                "title": "Reminder",
                "directive": None,
                "render_mode": "synthesis",
                "options": {},
                "sources": [{"kind": "reminders", "config": {}}],
            })

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
    """One-time components → blocks migration, tracked **per briefing**.

    Reads the same briefing set that ``check_briefings`` schedules
    (``get_briefings_for_user``) and migrates each briefing exactly once,
    guarded by its own sentinel. A per-briefing (not DB-wide) sentinel is
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
