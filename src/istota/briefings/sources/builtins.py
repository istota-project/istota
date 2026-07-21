"""Built-in structured source resolvers.

Wrap the existing structured fetchers (markets/calendar) for byte-identical
behaviour. ``todos`` / ``reminders`` / ``notes`` read a workspace file whose
**path is a briefing-source property** (``config.path``) — there is no convention
default filename. The ``notes/`` folder convention lives only in the prompt's
workspace-layout line as guidance for the model, not in the resolver. A
source without a ``path`` returns a not-configured result (reads nothing);
the user configures the path in the web editor or the migration.
"""

from __future__ import annotations

import hashlib
import logging
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from istota.briefings.sources import GatheredSource, SourceContext


logger = logging.getLogger(__name__)


def _now_in_user_tz(ctx: SourceContext) -> tuple[datetime, str, bool, bool]:
    """Return (now, tz_str, is_morning, is_weekend) for the user's timezone."""
    tz_str = "UTC"
    try:
        tz_str = ctx.app_config.resolve_user_timezone(ctx.user_id)
    except Exception:  # noqa: BLE001
        pass
    try:
        tz = ZoneInfo(tz_str)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
        tz_str = "UTC"
    now = ctx.now.astimezone(tz) if ctx.now else datetime.now(tz)
    return now, tz_str, now.hour < 12, now.weekday() in (5, 6)


def _workspace_file(ctx: SourceContext, override: str | None) -> str | None:
    """Return the source-config ``path`` override, or None when unset.

    After the Resources sunset there is no convention default filename for
    todos / reminders / notes — the location is a briefing-source property
    (set in the web editor or the migration). The ``notes/`` folder
    convention lives only in the prompt's workspace-layout line as guidance
    for the model, not in the resolver.
    """
    return override


def _read_workspace_text(ctx: SourceContext, path: str) -> str | None:
    """Read a workspace file, returning None on any error / missing file."""
    try:
        from istota.skills.files import read_text
        return read_text(ctx.app_config, path)
    except Exception:  # noqa: BLE001
        return None


# -- markets ------------------------------------------------------------------


def resolve_markets(config: dict, ctx: SourceContext) -> GatheredSource:
    now, _tz, is_morning, is_weekend = _now_in_user_tz(ctx)
    mode = "morning" if is_morning else "evening"
    try:
        from istota.skills.briefing import (
            _fetch_finviz_market_data,
            _fetch_market_data,
        )
    except Exception:  # noqa: BLE001
        return GatheredSource(
            kind="markets", title="Markets",
            provenance="(markets unavailable)", ok=False,
        )

    market_config = {
        k: v for k, v in config.items() if k in ("futures", "indices")
    }
    parts: list[str] = []
    if not is_weekend:
        market_data = _fetch_market_data(market_config, mode)
        if market_data:
            parts.append(market_data)
        if not is_morning:
            finviz = _fetch_finviz_market_data()
            if finviz:
                parts.append(finviz)

    if not parts:
        note = "(no market quotes — weekend)" if is_weekend else "(no market data)"
        return GatheredSource(
            kind="markets", title="Markets", provenance=note, ok=False,
        )
    return GatheredSource(
        kind="markets", title="Markets", text="\n\n".join(parts),
        provenance=f"{mode} market data",
    )


# -- calendar -----------------------------------------------------------------


def resolve_calendar(config: dict, ctx: SourceContext) -> GatheredSource:
    now, tz_str, is_morning, _weekend = _now_in_user_tz(ctx)
    try:
        from istota.skills.briefing import _fetch_calendar_events
    except Exception:  # noqa: BLE001
        return GatheredSource(
            kind="calendar", title="Calendar",
            provenance="(calendar unavailable)", ok=False,
        )
    content = _fetch_calendar_events(ctx.app_config, ctx.user_id, is_morning, tz_str)
    if not content:
        return GatheredSource(
            kind="calendar", title="Calendar",
            provenance="(no calendars available)", ok=False,
        )
    return GatheredSource(
        kind="calendar", title="Calendar", text=content,
        provenance="calendar events",
    )


# -- todos --------------------------------------------------------------------


def resolve_todos(config: dict, ctx: SourceContext) -> GatheredSource:
    path = _workspace_file(ctx, config.get("path"))
    if not path:
        return GatheredSource(
            kind="todos", title="Todos",
            provenance="(no path configured — set the source path)", ok=False,
        )
    content = _read_workspace_text(ctx, path)
    if not content:
        return GatheredSource(
            kind="todos", title="Todos",
            provenance="(no TODO file at configured path)", ok=False,
        )
    items = [
        {"text": line.strip()}
        for line in content.splitlines()
        if line.strip().startswith(("- [ ]", "* [ ]"))
    ]
    if not items:
        return GatheredSource(
            kind="todos", title="Todos",
            provenance="(no pending todos)", ok=False,
        )
    return GatheredSource(
        kind="todos", title="Todos", items=items,
        provenance=f"{len(items)} pending",
    )


# -- reminders ----------------------------------------------------------------


def resolve_reminders(config: dict, ctx: SourceContext) -> GatheredSource:
    path = _workspace_file(ctx, config.get("path"))
    if not path:
        return GatheredSource(
            kind="reminders", title="Reminder",
            provenance="(no path configured — set the source path)", ok=False,
        )
    content = _read_workspace_text(ctx, path)
    if not content:
        return GatheredSource(
            kind="reminders", title="Reminder",
            provenance="(no reminders file at configured path)", ok=False,
        )
    reminder = _pick_reminder(ctx, content)
    if not reminder:
        return GatheredSource(
            kind="reminders", title="Reminder",
            provenance="(no reminders)", ok=False,
        )
    return GatheredSource(
        kind="reminders", title="Reminder", text=reminder,
        provenance="daily reminder (pre-selected — include verbatim)",
    )


def _pick_reminder(ctx: SourceContext, content: str) -> str | None:
    """Shuffle-queue reminder selection, keyed on content hash.

    Reuses the framework ``reminder_state`` table (same behaviour as the legacy
    ``_fetch_random_reminder``): each reminder shows once before any repeats;
    the queue resets when the file content changes.
    """
    from istota import db
    from istota.skills.briefing import _parse_reminders

    reminders = _parse_reminders(content)
    if not reminders:
        return None
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    try:
        with db.get_db(ctx.app_config.db_path) as conn:
            state = db.get_reminder_state(conn, ctx.user_id)
            if state is None or state.content_hash != content_hash or not state.queue:
                indices = list(range(len(reminders)))
                random.shuffle(indices)
                queue = indices
            else:
                queue = state.queue
            next_index = queue.pop(0)
            db.set_reminder_state(conn, ctx.user_id, queue, content_hash)
            conn.commit()
            return reminders[next_index % len(reminders)]
    except Exception as e:  # noqa: BLE001
        logger.warning("reminder state error, falling back to random: %s", e)
        return random.choice(reminders)


# -- notes --------------------------------------------------------------------


def resolve_notes(config: dict, ctx: SourceContext) -> GatheredSource:
    path = _workspace_file(ctx, config.get("path"))
    if not path:
        return GatheredSource(
            kind="notes", title="Notes",
            provenance="(no path configured — set the source path)", ok=False,
        )
    content = _read_workspace_text(ctx, path)
    if not content or not content.strip():
        return GatheredSource(
            kind="notes", title="Notes",
            provenance="(no notes file at configured path)", ok=False,
        )
    max_chars = int(ctx.module_config.max_source_chars)
    text = content.strip()
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "\n[...truncated]"
    return GatheredSource(
        kind="notes", title="Notes", text=text, provenance="notes file",
    )
