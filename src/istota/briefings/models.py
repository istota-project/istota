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
)

# Blocks whose gathered content is deterministically pre-formatted and passed to
# the model for near-verbatim inclusion (markets/calendar), vs. synthesized.
RENDER_MODES = ("synthesis", "structured")

# Source kinds whose content is structured (pre-rendered verbatim) rather than
# free-text synthesized. Used to pick a block's default render_mode.
STRUCTURED_KINDS = ("markets", "calendar")


@dataclass
class BriefingsContext:
    """Per-user runtime handle for the briefings module.

    All paths are absolute. ``data_dir`` / ``db_path`` are materialised by the
    workspace loader. ``workspace_root`` is the user's bot workspace dir (parent
    of ``briefings/``); callers that need workspace-relative default files
    (todos/reminders/notes convention paths) consult it rather than
    ``data_dir.parent`` (which is unreliable under a ``data_dir`` override).
    """

    user_id: str
    data_dir: Path
    db_path: Path
    workspace_root: Path | None = None

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


def parse_json_dict(raw: Any) -> dict:
    """Coerce a DB JSON column into a dict; ``{}`` on empty/invalid."""
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
