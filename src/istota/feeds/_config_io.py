"""Read/write feeds.toml.

feeds.toml is the source of truth for subscriptions and categories. The
SQLite ``feeds`` table is its materialised projection (kept in sync on
poll). Read state and entries live only in SQLite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomli


def read_feeds_config(path: Path) -> dict[str, Any]:
    """Parse a feeds.toml file.

    Returns the parsed dict with the documented top-level keys: ``settings``,
    ``categories``, ``feeds``. Missing keys default to empty.
    """
    if not path.exists():
        return {"settings": {}, "categories": [], "feeds": []}
    parsed = tomli.loads(path.read_text())
    parsed.setdefault("settings", {})
    parsed.setdefault("categories", [])
    parsed.setdefault("feeds", [])
    return parsed


def write_feeds_config(path: Path, data: dict[str, Any]) -> None:
    """Write the feeds.toml file.

    Hand-written formatter — ``tomli`` doesn't ship a writer and pulling in
    ``tomli_w`` for one file isn't worth the dep. The output round-trips
    through :func:`read_feeds_config`.
    """
    body = _render_toml(data)
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
