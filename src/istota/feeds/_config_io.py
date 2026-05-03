"""Read/write feeds.toml.

feeds.toml is the source of truth for subscriptions and categories. The
SQLite ``feeds`` table is its materialised projection (kept in sync on
poll). Read state and entries live only in SQLite.

Accepts plain ``.toml`` or UPPERCASE ``.md`` with a fenced ```toml block —
matches CRON.md / BRIEFINGS.md / INVOICING.md convention.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import tomli

_TOML_BLOCK_RE = re.compile(r"```toml\s*\n(.*?)```", re.DOTALL)


def read_feeds_config(path: Path) -> dict[str, Any]:
    """Parse a feeds.toml (or FEEDS.md with a toml block) file.

    Returns the parsed dict with the documented top-level keys: ``settings``,
    ``categories``, ``feeds``. Missing keys default to empty.
    """
    if not path.exists():
        return {"settings": {}, "categories": [], "feeds": []}
    text = path.read_text()
    if path.suffix.lower() == ".md":
        match = _TOML_BLOCK_RE.search(text)
        if not match:
            raise ValueError(
                f"No ```toml code block found in {path}; expected a "
                f"FEEDS.md-style config with the TOML body fenced."
            )
        text = match.group(1)
    parsed = tomli.loads(text)
    parsed.setdefault("settings", {})
    parsed.setdefault("categories", [])
    parsed.setdefault("feeds", [])
    return parsed


def write_feeds_config(path: Path, data: dict[str, Any]) -> None:
    """Write the feeds.toml file.

    Hand-written formatter — ``tomli`` doesn't ship a writer and pulling in
    ``tomli_w`` for one file isn't worth the dep. The output round-trips
    through :func:`read_feeds_config`.

    For ``.md`` paths, wraps the TOML in a fenced block under a stub
    heading so the file stays readable.
    """
    body = _render_toml(data)
    if path.suffix.lower() == ".md":
        body = (
            "# Feeds\n\n"
            "Subscription list for the native feeds module. Edit through the "
            "settings UI or `istota-skill feeds` — manual edits to the toml "
            "block below are also fine.\n\n"
            "```toml\n" + body + "```\n"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _render_toml(data: dict[str, Any]) -> str:
    """Serialise the feeds.toml shape to TOML text.

    Only handles the shape this module produces — not a general TOML writer.
    """
    parts: list[str] = []

    settings = data.get("settings") or {}
    if settings:
        parts.append("[settings]")
        for k, v in settings.items():
            parts.append(f"{k} = {_format_value(v)}")
        parts.append("")

    for cat in data.get("categories") or []:
        parts.append("[[categories]]")
        for k, v in cat.items():
            parts.append(f"{k} = {_format_value(v)}")
        parts.append("")

    for feed in data.get("feeds") or []:
        parts.append("[[feeds]]")
        # Render in a stable order for diff-friendliness
        for key in ("url", "title", "category", "poll_interval_minutes"):
            if key in feed:
                parts.append(f"{key} = {_format_value(feed[key])}")
        for k, v in feed.items():
            if k in {"url", "title", "category", "poll_interval_minutes"}:
                continue
            parts.append(f"{k} = {_format_value(v)}")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def _format_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int) or isinstance(v, float):
        return str(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_format_value(x) for x in v) + "]"
    raise TypeError(f"Unsupported feeds.toml value type: {type(v).__name__}")
