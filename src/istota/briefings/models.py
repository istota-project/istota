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

from copy import deepcopy
import json
import logging
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
PRESENTATION_WIDTHS = ("auto", "wide", "full")

logger = logging.getLogger(__name__)

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


# Draft definitions only; runtime pickers supply feed, site, file and shared names.
BLOCK_TEMPLATES = [
    {"id": template_id, "label": label, "description": description, "block": {
        "title": label, "directive": "", "render_mode": mode,
        "options": {"presentation": {"width": "auto"}},
        "sources": [{"kind": kind, "config": {}, "enabled": True}] if kind else [],
    }}
    for template_id, label, description, kind, mode in [
        ("feeds", "News from feeds", "Choose a feed subscription or category.", "rss", "synthesis"),
        ("websites", "News from websites", "Choose a site preset or enter a URL.", "browse", "synthesis"),
        ("newsletters", "Newsletters", "Summarize newsletters from your email sources.", "email", "synthesis"),
        ("calendar", "Calendar", "Show your upcoming calendar events.", "calendar", "structured"),
        ("markets", "Markets", "Show the current market summary.", "markets", "structured"),
        ("tasks", "Tasks", "Choose a file containing your tasks.", "todos", "synthesis"),
        ("notes", "Notes", "Choose a file containing your notes.", "notes", "synthesis"),
        ("reminder", "Reminder", "Choose a file containing your reminders.", "reminders", "synthesis"),
        ("shared", "Shared content", "Choose an available shared block.", "shared_block", "structured"),
        ("custom", "Custom", "Start with a title and add your own sources.", None, "synthesis"),
    ]
]


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
    document: dict | None = None


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


def _validate_presentation(presentation: object) -> dict:
    """Return the supported layout fields, rejecting malformed new values."""
    if not isinstance(presentation, dict):
        raise ValueError("presentation must be an object")
    width = presentation.get("width", "auto")
    if not isinstance(width, str) or width not in PRESENTATION_WIDTHS:
        raise ValueError("presentation.width must be auto, wide or full")
    return {"width": width}


def validate_block_options(options: object) -> dict:
    """Validate reserved options while retaining unrelated configuration."""
    if not isinstance(options, dict):
        raise ValueError("options must be an object")
    if "presentation" in options:
        _validate_presentation(options["presentation"])
    return deepcopy(options)


def presentation_for_options(options: dict) -> dict:
    """Read optional stored presentation without breaking old definitions."""
    if "presentation" not in options:
        return {"width": "auto"}
    try:
        return _validate_presentation(options["presentation"])
    except ValueError:
        logger.warning("briefing_presentation_invalid")
        return {"width": "auto"}


def validate_briefing_document(document: object) -> dict:
    """Validate a version-1 edition, retaining future kinds' Markdown fallback."""
    if not isinstance(document, dict) or type(document.get("version")) is not int or document["version"] != 1:
        raise ValueError("briefing_document_version")
    if not isinstance(document.get("title"), str) or not document["title"].strip():
        raise ValueError("briefing_document_title")
    if not isinstance(document.get("blocks"), list):
        raise ValueError("briefing_document_blocks")
    blocks = []
    seen = set()
    for block in document["blocks"]:
        if not isinstance(block, dict):
            raise ValueError("briefing_document_block")
        block_id = block.get("block_id")
        if type(block_id) is not int or block_id in seen:
            raise ValueError("briefing_document_block_id")
        seen.add(block_id)
        for name in ("title", "kind", "body_md"):
            if not isinstance(block.get(name), str):
                raise ValueError("briefing_document_" + name)
        if not block["title"].strip() or not block["kind"].strip():
            raise ValueError("briefing_document_block_label")
        if block.get("source_status") not in ("complete", "partial"):
            raise ValueError("briefing_document_source_status")
        try:
            presentation = _validate_presentation(block.get("presentation"))
        except ValueError:
            raise ValueError("briefing_document_presentation") from None
        blocks.append({
            "block_id": block_id, "title": block["title"], "kind": block["kind"],
            "body_md": block["body_md"], "presentation": presentation,
            "source_status": block["source_status"],
        })
    return {"version": 1, "title": document["title"], "blocks": blocks}


def normalize_block_payload(payload: object) -> dict:
    """Validate a block patch and optional complete source list before writes."""
    if not isinstance(payload, dict):
        raise ValueError("block must be an object")
    normalized = {}
    if "id" in payload:
        block_id = payload["id"]
        if type(block_id) is not int or block_id <= 0:
            raise ValueError("id must be a positive integer")
        normalized["id"] = block_id
    creating = "id" not in normalized
    for name in ("briefing_name", "title"):
        if name in payload or creating:
            value = payload.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} required")
            normalized[name] = value.strip()
    if "directive" in payload or creating:
        directive = payload.get("directive")
        if directive is not None and not isinstance(directive, str):
            raise ValueError("directive must be text or null")
        normalized["directive"] = directive or ""
    if "render_mode" in payload or creating:
        mode = payload.get("render_mode", "synthesis")
        if mode not in RENDER_MODES:
            raise ValueError("render_mode must be synthesis or structured")
        normalized["render_mode"] = mode
    if "options" in payload or creating:
        normalized["options"] = validate_block_options(payload.get("options", {}))
    if "sources" in payload:
        raw_sources = payload["sources"]
        if not isinstance(raw_sources, list):
            raise ValueError("sources must be a list")
        if creating and not raw_sources:
            raise ValueError("a new block requires at least one source")
        sources = []
        seen = set()
        for raw in raw_sources:
            if not isinstance(raw, dict) or raw.get("kind") not in SOURCE_KINDS:
                raise ValueError("each source requires a valid kind")
            source = {"kind": raw["kind"]}
            if "id" in raw:
                source_id = raw["id"]
                if type(source_id) is not int or source_id <= 0 or source_id in seen:
                    raise ValueError("source ids must be distinct positive integers")
                seen.add(source_id)
                source["id"] = source_id
            config = raw.get("config", {})
            if not isinstance(config, dict):
                raise ValueError("source config must be an object")
            enabled = raw.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ValueError("source enabled must be a boolean")
            source.update(config=deepcopy(config), enabled=enabled)
            sources.append(source)
        normalized["sources"] = sources
    # Refuse non-JSON values before any SQL, including callers outside HTTP.
    try:
        json.dumps(normalized, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("block fields must contain valid JSON values") from None
    return normalized
