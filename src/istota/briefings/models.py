"""Data types for the briefings module.

The :class:`BriefingsContext` is the per-user runtime handle (paths). Built by
:func:`istota.briefings.workspace.synthesize_briefings_context` or the loader
:func:`istota.briefings._loader.resolve_for_user`. Everything else takes a
context and operates on it.

A *briefing* is an ordered list of content *blocks*; each block has a title, an
optional synthesis directive, and 1..N *sources*. At generation the LLM
synthesizes each block from its gathered sources into one coherent titled
section. Mirrors :mod:`istota.feeds.models`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# The typed source kinds a block can fan in. `rss`/`email`/`browse` gather live
# external content; the rest delegate to the existing structured built-ins.
SOURCE_KINDS = (
    "rss",
    "email",
    "browse",
    "markets",
    "calendar",
    "todos",
    "reminders",
    "notes",
    # Curated shared content read from the shared_kv store. `shared_block` is a
    # module-owned shared block (namespace "briefing_shared_blocks", key = block
    # name). It can be synthesis or structured — the block's render_mode decides,
    # so it's not in STRUCTURED_KINDS.
    "shared_block",
)

# Blocks whose gathered content is deterministically pre-formatted and passed to
# the model for near-verbatim inclusion (markets/calendar), vs. synthesized.
RENDER_MODES = ("synthesis", "structured")

# Source kinds an admin-editable *shared* block may use — genuinely user-agnostic
# only (a shared block is generated once globally under the __system__ identity,
# so a per-user kind like ``rss`` would leak one person's subscriptions to
# everyone). The admin CRUD route + CLI validate against this; generation defends
# again via ``shared_blocks._allowed_sources``. Canonical set lives here so the
# web/route/CLI layers import it without pulling the generation module.
ALLOWED_SHARED_SOURCE_KINDS = frozenset({"browse", "markets", "email"})

# Source kinds whose content is structured (pre-rendered verbatim) rather than
# free-text synthesized. Used to pick a block's default render_mode.
STRUCTURED_KINDS = ("markets", "calendar")


@dataclass
class BriefingsContext:
    """Per-user runtime handle for the briefings module.

    All paths are absolute. ``data_dir`` / ``db_path`` are materialised by the
    workspace loader. ``workspace_root`` is the user's bot workspace dir (parent
    of ``briefings/``); callers that need the bot dir (e.g. to resolve a
    source ``path`` relative to the workspace) consult it rather than
    ``data_dir.parent`` (which is unreliable under a ``data_dir`` override).
    """

    user_id: str
    data_dir: Path
    db_path: Path
    workspace_root: Path | None = None
    configured_briefing_names: tuple[str, ...] = ()

    def ensure_dirs(self) -> None:
        """Create the data dir and the SQLite parent dir."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


@dataclass
class BriefingBlock:
    """A row from the ``briefing_blocks`` table.

    ``options`` is a decoded JSON dict (story_count, lookback_hours, tone,
    max_chars, …). ``sources`` is populated by the CRUD layer when a block is
    fetched with its sources.
    """

    id: int
    briefing_name: str
    position: int
    title: str
    directive: str | None = None
    render_mode: str = "synthesis"
    options: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    sources: list["BlockSource"] = field(default_factory=list)


@dataclass
class BlockSource:
    """A row from the ``briefing_block_sources`` table.

    ``config`` is a decoded JSON dict, kind-specific (see the module docs).
    """

    id: int
    block_id: int
    position: int
    kind: str
    config: dict = field(default_factory=dict)
    enabled: bool = True
    created_at: str = ""


@dataclass
class ArchivedBriefing:
    """A row from the ``briefing_archive`` table — one rendered briefing."""

    id: int
    briefing_name: str
    subject: str | None
    body_md: str
    generated_at: str
    task_id: int | None = None
    block_meta: dict = field(default_factory=dict)
    delivered_to: list[str] = field(default_factory=list)


@dataclass
class BriefingItem:
    """A row from the ``briefing_items`` table (Phase 2, continuous ingestion).

    Created now, unused until the continuous-ingestion phase ships.
    """

    id: int
    source_kind: str
    dedup_key: str
    ingested_at: str
    source_ref: str | None = None
    title: str | None = None
    body_md: str | None = None
    summary_md: str | None = None
    url: str | None = None
    published_at: str | None = None


def parse_json_dict(raw: Any, *, strict: bool = False) -> dict:
    """Decode a DB JSON object; strict callers refuse empty/invalid values."""
    if strict:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(parsed, dict):
            raise ValueError("expected JSON object")
        return parsed
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def parse_json_list(raw: Any) -> list:
    """Coerce a DB JSON column into a list; ``[]`` on empty/invalid."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def validate_shared_block_payload(payload: dict) -> dict:
    """Validate and normalize a shared-block definition without a web dependency."""
    from croniter import croniter

    name = (payload.get("name") or "").strip()
    if not name or not _SLUG_RE.match(name):
        raise ValueError("name must be a slug: lowercase letters, digits, - and _")
    cron = (payload.get("cron") or "").strip()
    if not cron or not croniter.is_valid(cron):
        raise ValueError(f"invalid cron expression: {cron!r}")
    render_mode = payload.get("render_mode") or "synthesis"
    if render_mode not in RENDER_MODES:
        raise ValueError(f"render_mode must be one of {list(RENDER_MODES)}")
    raw_sources = payload.get("sources")
    if raw_sources is None:
        raw_sources = []
    if not isinstance(raw_sources, list):
        raise ValueError("sources must be a list")
    sources: list = []
    for s in raw_sources:
        if not isinstance(s, dict) or "kind" not in s:
            raise ValueError("each source must be an object with a 'kind'")
        kind = s["kind"]
        if kind not in ALLOWED_SHARED_SOURCE_KINDS:
            raise ValueError(
                f"source kind {kind!r} not allowed for shared blocks "
                f"(allowed: {sorted(ALLOWED_SHARED_SOURCE_KINDS)})",
            )
        sources.append({"kind": kind, "config": s.get("config") or {}})
    return {
        "name": name,
        "cron": cron,
        "title": (payload.get("title") or "").strip(),
        "directive": payload.get("directive") or None,
        "render_mode": render_mode,
        "enabled": bool(payload.get("enabled", True)),
        "trusted": bool(payload.get("trusted", False)),
        "sources": sources,
    }
