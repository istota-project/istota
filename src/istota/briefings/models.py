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
    # The user's own mail from one connected account. Personal only: it is in
    # neither GROUP_SOURCE_KINDS nor ALLOWED_SHARED_SOURCE_KINDS.
    "my_email",
    # Curated shared content read from the shared_kv store. `shared_block` is a
    # module-owned shared block (namespace "briefing_shared_blocks", key = block
    # name). It can be synthesis or structured — the block's render_mode decides,
    # so it's not in STRUCTURED_KINDS.
    "shared_block",
)

# Blocks whose gathered content is deterministically pre-formatted and passed to
# the model for near-verbatim inclusion (markets/calendar), vs. synthesized.
RENDER_MODES = ("synthesis", "structured")
# A shared block may also be `agent`: its directive is a whole task prompt,
# run as a sandboxed research task (rules in validate_shared_block_payload).
SHARED_RENDER_MODES = (*RENDER_MODES, "agent")
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
# A starting directive for templates that need one. Source config stays empty:
# each resolver supplies its own defaults.
_TEMPLATE_DIRECTIVES = {
    "my_email": (
        "List the five most pressing action items from my email in the past "
        "24 hours, as bullet points. Name who is asking and by when."
    ),
}
BLOCK_TEMPLATES = [
    {"id": template_id, "label": label, "description": description, "block": {
        "title": label, "directive": _TEMPLATE_DIRECTIVES.get(template_id, ""), "render_mode": mode,
        "options": {"presentation": {"width": "auto"}},
        "sources": [{"kind": kind, "config": {}, "enabled": True}] if kind else [],
    }}
    for template_id, label, description, kind, mode in [
        ("feeds", "News from feeds", "Choose a feed subscription or category.", "rss", "synthesis"),
        ("websites", "News from websites", "Choose a site preset or enter a URL.", "browse", "synthesis"),
        ("newsletters", "Newsletters", "Summarize newsletters from your email sources.", "email", "synthesis"),
        ("my_email", "My email", "Action items from an inbox you connected.", "my_email", "synthesis"),
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
    # The istota Config, set by the two callers that invoke the Click tree.
    app_config: Any = None

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

    id: int | str
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
    read_at: str | None = None
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
    if render_mode not in SHARED_RENDER_MODES:
        raise ValueError(f"render_mode must be one of {list(SHARED_RENDER_MODES)}")
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
    trusted = bool(payload.get("trusted", False))
    directive = payload.get("directive") or None
    if render_mode == "agent":
        # Pre-gathered page text would land in the trusted task prompt, and the
        # output is model text over arbitrary pages, so never trusted either.
        if sources:
            raise ValueError("agent blocks take no sources")
        if trusted:
            raise ValueError("agent blocks are never trusted")
        if not isinstance(directive, str) or not directive.strip():
            raise ValueError("agent blocks need a task prompt")
    return {
        "name": name,
        "cron": cron,
        "title": (payload.get("title") or "").strip(),
        "directive": directive,
        "render_mode": render_mode,
        "enabled": bool(payload.get("enabled", True)),
        "trusted": trusted,
        "sources": sources,
    }


# `email` is the shared newsletter pool: unowned mail only, read under no user's
# identity, so it is public to the installation rather than personal.
GROUP_SOURCE_KINDS = frozenset({
    "browse", "markets", "email", "group_memory", "group_file", "shared_block",
    "personal_file", "personal_calendar",
})
GROUP_CHANNELS = ("in_app", "email", "talk", "ntfy")
CALENDAR_FIELDS = ("time", "title", "description", "attendees", "links", "location")
CALENDAR_LOOKBACK_DAYS = (0, 1, 7, 30)
CALENDAR_LOOKAHEAD_DAYS = (1, 7, 14, 30)


def normalize_text_file_reference(raw):
    path = _group_text(raw, "file path", required=True, limit=1000)
    if (any(part in ("", ".", "..") for part in path.split("/"))
            or any(c in path for c in ("\\", "\0", ":"))
            or not path.lower().endswith((".txt", ".md", ".markdown"))):
        raise ValueError("select one relative text file")
    return path


def normalize_group_email_config(config):
    if set(config) - {"mode", "senders", "lookback_hours"}:
        raise ValueError("unknown newsletter source setting")
    if config.get("mode", "shared") not in ("shared", "senders"):
        raise ValueError("newsletter mode must be shared or senders")
    senders = config.get("senders", [])
    if (not isinstance(senders, list) or len(senders) > 100
            or not all(isinstance(s, str) and 0 < len(s) <= 200 for s in senders)):
        raise ValueError("newsletter senders must be a list of patterns")
    hours = config.get("lookback_hours", 12)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= 24 * 30:
        raise ValueError("newsletter lookback must be between 0 and 720 hours")
    return dict(config)


def normalize_group_resource_config(kind, config):
    if kind == "group_memory":
        if config:
            raise ValueError("group memory takes no resource override")
        return {}
    if set(config) != {"path"}:
        raise ValueError("select one group file")
    return {"path": normalize_text_file_reference(config["path"])}


def normalize_contribution_config(kind, config, *, allow_pending=False):
    """One resource and a bounded disclosure, independent of caller authority."""
    from urllib.parse import unquote, urlsplit
    from istota.sandbox.user_scope import is_scopable_user_id

    allowed = {"owner_id", "path"} if kind == "personal_file" else {
        "owner_id", "calendar_url", "fields", "include_private", "lookback_days", "lookahead_days",
    }
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError("invalid contribution config")
    owner = _group_text(config.get("owner_id"), "source owner", required=True, limit=100)
    if not is_scopable_user_id(owner) or owner.startswith("__"):
        raise ValueError("invalid source owner")
    if allow_pending and set(config) == {"owner_id"}:
        return {"owner_id": owner}
    if kind == "personal_file":
        return {"owner_id": owner, "path": normalize_text_file_reference(config.get("path"))}
    url = _group_text(config.get("calendar_url"), "calendar reference", required=True, limit=2000)
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or any(part in (".", "..") for part in unquote(parsed.path).split("/"))):
        raise ValueError("select one calendar URL")
    fields = config.get("fields", ["time", "title"])
    if not isinstance(fields, list) or not fields or any(not isinstance(f, str) or f not in CALENDAR_FIELDS for f in fields):
        raise ValueError("invalid calendar fields")
    private = config.get("include_private", False)
    if type(private) is not bool:
        raise ValueError("include_private must be a boolean")
    result = {"owner_id": owner, "calendar_url": url, "fields": [f for f in CALENDAR_FIELDS if f in fields],
              "include_private": private}
    for window_field, default, choices in (("lookback_days", 0, CALENDAR_LOOKBACK_DAYS), ("lookahead_days", 1, CALENDAR_LOOKAHEAD_DAYS)):
        value = config.get(window_field, default)
        if type(value) is not int or value not in choices:
            raise ValueError("invalid calendar window")
        result[window_field] = value
    return result


def _group_text(value, field, *, required=False, limit=20000):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"invalid {field}")
    return value.strip()


def _group_sources(raw, seen):
    if not isinstance(raw, list):
        raise ValueError("sources must be a list")
    sources = []
    for source in raw:
        if not isinstance(source, dict) or set(source) - {"id", "kind", "config"}:
            raise ValueError("invalid source")
        source_id = _group_text(source.get("id"), "source id", required=True, limit=100)
        if not _SLUG_RE.fullmatch(source_id) or source_id in seen:
            raise ValueError("source ids must be unique slugs")
        seen.add(source_id)
        kind = source.get("kind")
        if not isinstance(kind, str) or kind not in GROUP_SOURCE_KINDS:
            raise ValueError("source kind is not allowed for group briefings")
        config = source.get("config", {})
        if not isinstance(config, dict):
            raise ValueError("source config must be an object")
        if kind in ("personal_file", "personal_calendar"):
            config = normalize_contribution_config(kind, config, allow_pending=True)
        elif kind in ("group_memory", "group_file"):
            config = normalize_group_resource_config(kind, config)
        elif kind == "email":
            config = normalize_group_email_config(config)
        sources.append({"id": source_id, "kind": kind, "config": config})
    return sources


def normalize_group_generation(mode: str, raw: dict) -> dict:
    """Strict group input validation; personal seeding's fail-soft parser cannot be used."""
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        raise ValueError("generation version must be 1")
    common = {"version", "contains_personal_information", "audience_acknowledged"}
    allowed = common | ({"prompt", "research", "inputs"} if mode == "prompt" else {"blocks"})
    if set(raw) - allowed:
        raise ValueError("unknown generation setting")
    result = {"version": 1}
    for flag in ("contains_personal_information", "audience_acknowledged"):
        value = raw.get(flag, False)
        if type(value) is not bool:
            raise ValueError(f"{flag} must be a boolean")
        result[flag] = value
    seen = set()
    if mode == "prompt":
        result["prompt"] = _group_text(raw.get("prompt"), "generation prompt", required=True)
        research = raw.get("research", {})
        if not isinstance(research, dict) or set(research) - {"enabled", "prompt", "allowed_domains"}:
            raise ValueError("invalid research settings")
        enabled = research.get("enabled", False)
        if type(enabled) is not bool:
            raise ValueError("research enabled must be a boolean")
        prompt = _group_text(research.get("prompt", ""), "research prompt", required=enabled)
        domains = research.get("allowed_domains", [])
        if not isinstance(domains, list):
            raise ValueError("allowed domains must be a list")
        if domains:
            raise ValueError("research does not enforce an allowed-domain policy")
        result["research"] = {"enabled": enabled, "prompt": prompt, "allowed_domains": []}
        result["inputs"] = _group_sources(raw.get("inputs", []), seen)
    else:
        blocks = raw.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise ValueError("blocks mode requires blocks")
        result["blocks"] = []
        for block in blocks:
            if not isinstance(block, dict) or set(block) - {"title", "directive", "render_mode", "options", "sources"}:
                raise ValueError("invalid block")
            sources = _group_sources(block.get("sources"), seen)
            if not sources:
                raise ValueError("each block requires a source")
            render_mode = block.get("render_mode", "structured" if sources[0]["kind"] in STRUCTURED_KINDS else "synthesis")
            if render_mode not in RENDER_MODES:
                raise ValueError("invalid block render mode")
            options = block.get("options", {})
            if not isinstance(options, dict):
                raise ValueError("block options must be an object")
            directive = block.get("directive")
            directive = "" if directive is None else _group_text(directive, "block directive")
            result["blocks"].append({
                "title": _group_text(block.get("title"), "block title", required=True, limit=200),
                "directive": directive, "render_mode": render_mode, "options": options, "sources": sources,
            })
    # Roundtrip rejects non-JSON inputs and prevents caller mutations of snapshots.
    return json.loads(json.dumps(result, allow_nan=False))


def validate_group_definition_payload(payload: dict) -> dict:
    from croniter import croniter
    from zoneinfo import ZoneInfo

    if not isinstance(payload, dict) or set(payload) - {
        "name", "title", "cron", "timezone", "enabled", "generation_mode", "generation", "channel", "excluded_user_ids",
    }:
        raise ValueError("invalid definition fields")
    name = _group_text(payload.get("name"), "name", required=True, limit=100)
    if not _SLUG_RE.fullmatch(name):
        raise ValueError("name must be a slug")
    cron = _group_text(payload.get("cron"), "cron", required=True, limit=100)
    if len(cron.split()) != 5 or not croniter.is_valid(cron):
        raise ValueError("invalid cron expression")
    zone = _group_text(payload.get("timezone"), "timezone", required=True, limit=100)
    ZoneInfo(zone)
    mode = payload.get("generation_mode")
    if mode not in ("prompt", "blocks"):
        raise ValueError("generation mode must be prompt or blocks")
    channel = payload.get("channel", "in_app")
    if channel not in GROUP_CHANNELS:
        raise ValueError("invalid delivery channel")
    enabled = payload.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    excluded = payload.get("excluded_user_ids", [])
    if not isinstance(excluded, list) or any(
        not isinstance(user, str) or not user or user != user.strip() for user in excluded
    ):
        raise ValueError("excluded_user_ids must be user ids")
    return {
        "excluded_user_ids": sorted(set(excluded)),
        "name": name, "title": _group_text(payload.get("title", ""), "title", limit=200),
        "cron": cron, "timezone": zone, "generation_mode": mode, "channel": channel,
        "enabled": enabled, "generation": normalize_group_generation(mode, payload.get("generation")),
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


MY_EMAIL_FIXED_ACCOUNTS = ("bot", "google")
_MY_EMAIL_IMAP_RE = re.compile(r"imap:([1-9][0-9]{0,17})")


def parse_my_email_account(value: object) -> str | int:
    """A ``my_email`` source's ``account``: ``"bot"``, ``"google"``, or a mail account id.

    ``imap:<id>`` names one of the user's connected mail accounts; whether the
    id is theirs is checked against the framework DB, not here. A missing
    account is the bot address. Raises ``ValueError`` for anything else.
    """
    if value is None or value == "":
        return "bot"
    if value in MY_EMAIL_FIXED_ACCOUNTS:
        return value
    if isinstance(value, str):
        match = _MY_EMAIL_IMAP_RE.fullmatch(value)
        if match:
            return int(match.group(1))
    raise ValueError("my_email account must be bot, google or imap:<id>")


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
            if raw["kind"] == "my_email":
                parse_my_email_account(config.get("account"))
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
