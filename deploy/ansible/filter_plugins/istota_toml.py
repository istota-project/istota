"""Ansible filter: render config-authored briefing blocks as TOML.

Per-user briefing schedule + delivery live in the ``briefing_configs`` DB table
(provisioned via ``istota briefings schedule ensure``); config.toml renders no
per-user data for those. Config-authored *content blocks*, however, are an
in-memory-only ``Config`` field (see the config-authored-rich-briefing-blocks
spec): they must reach ``Config.users[uid].briefings[*].blocks`` via config.toml
so the module-DB seeder can materialise them once.

``istota_briefing_blocks_toml(users)`` renders a content-only
``[[users.<uid>.briefings]]`` stub (``name`` + ``cron`` + the nested
``[[...blocks]]`` / ``[[...blocks.sources]]``) for every briefing that declares a
non-empty ``blocks`` list. Schedule/delivery still flow through the CLI/DB path;
``config._apply_user_briefings`` re-attaches these blocks onto the surviving
DB-sourced entry by matching ``name``.

Only leaf dicts (``options`` / source ``config``) render as TOML inline tables;
blocks and sources render as array-of-tables so the shape mirrors the spec's
authoring example exactly.
"""
from __future__ import annotations


def _toml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_value(value) -> str:
    """Render a scalar / list / leaf-dict as a TOML value expression."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _toml_str(value)
    if isinstance(value, (list, tuple)):
        return "[ " + ", ".join(_toml_value(v) for v in value) + " ]"
    if isinstance(value, dict):
        inner = ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items())
        return "{ " + inner + " }" if inner else "{}"
    # Fallback: stringify unknown types (None, etc.) defensively.
    return _toml_str(str(value))


def _render_source(prefix: str, src: dict) -> list[str]:
    lines = [f"    [[{prefix}.blocks.sources]]"]
    kind = src.get("kind", "")
    lines.append(f"    kind = {_toml_value(kind)}")
    cfg = src.get("config") or {}
    lines.append(f"    config = {_toml_value(cfg)}")
    return lines


def _render_block(prefix: str, block: dict) -> list[str]:
    lines = [f"  [[{prefix}.blocks]]"]
    lines.append(f"  title = {_toml_value(block.get('title', ''))}")
    if block.get("directive"):
        lines.append(f"  directive = {_toml_value(block['directive'])}")
    if block.get("render_mode"):
        lines.append(f"  render_mode = {_toml_value(block['render_mode'])}")
    options = block.get("options") or {}
    if options:
        lines.append(f"  options = {_toml_value(options)}")
    for src in block.get("sources") or []:
        lines.append("")
        lines.extend(_render_source(prefix, src))
    return lines


def istota_briefing_blocks_toml(users) -> str:
    """Render ``[[users.X.briefings]]`` block stubs for blocks-bearing briefings.

    Returns "" when no briefing across all users declares ``blocks``, so the
    template can render nothing (byte-unchanged config) in the common case.
    """
    if not isinstance(users, dict):
        return ""
    out: list[str] = []
    for uid, user_cfg in users.items():
        if not isinstance(user_cfg, dict):
            continue
        for briefing in user_cfg.get("briefings") or []:
            blocks = briefing.get("blocks") if isinstance(briefing, dict) else None
            if not blocks:
                continue
            prefix = f"users.{uid}.briefings"
            out.append(f"[[{prefix}]]")
            out.append(f"name = {_toml_value(briefing.get('name', ''))}")
            out.append(f"cron = {_toml_value(briefing.get('cron', ''))}")
            for block in blocks:
                out.append("")
                out.extend(_render_block(prefix, block))
            out.append("")
    return "\n".join(out).rstrip() + ("\n" if out else "")


def istota_default_briefings_toml(defaults) -> str:
    """Render the top-level ``[[default_briefings]]`` section from a list.

    Each entry carries ``name`` / ``cron`` / ``output`` plus the same nested
    ``[[default_briefings.blocks]]`` / ``[[...blocks.sources]]`` shape as a
    per-user briefing. Returns "" for an empty/invalid list so the template
    renders nothing (byte-unchanged config) when no defaults are configured.
    """
    if not isinstance(defaults, (list, tuple)):
        return ""
    out: list[str] = []
    for briefing in defaults:
        if not isinstance(briefing, dict) or not briefing.get("name"):
            continue
        prefix = "default_briefings"
        out.append(f"[[{prefix}]]")
        out.append(f"name = {_toml_value(briefing.get('name', ''))}")
        out.append(f"cron = {_toml_value(briefing.get('cron', ''))}")
        out.append(f"output = {_toml_value(briefing.get('output', 'talk'))}")
        for block in briefing.get("blocks") or []:
            out.append("")
            out.extend(_render_block(prefix, block))
        out.append("")
    return "\n".join(out).rstrip() + ("\n" if out else "")


def istota_briefing_shared_blocks_toml(shared_blocks) -> str:
    """Render the top-level ``[[briefing_shared_blocks]]`` section from a list.

    Each entry is a one-block shared briefing (name/cron/title/directive/
    render_mode/enabled + nested ``[[briefing_shared_blocks.sources]]``),
    generated once globally and written into ``shared_kv`` (shared-kv-curated-
    content spec). Returns "" for an empty/invalid list so the template renders
    nothing and ``load_config`` seeds ``DEFAULT_SHARED_BLOCKS`` instead.
    """
    if not isinstance(shared_blocks, (list, tuple)):
        return ""
    out: list[str] = []
    for block in shared_blocks:
        if not isinstance(block, dict) or not block.get("name") or not block.get("cron"):
            continue
        prefix = "briefing_shared_blocks"
        out.append(f"[[{prefix}]]")
        out.append(f"name = {_toml_value(block.get('name', ''))}")
        out.append(f"cron = {_toml_value(block.get('cron', ''))}")
        if block.get("title"):
            out.append(f"title = {_toml_value(block['title'])}")
        if block.get("directive"):
            out.append(f"directive = {_toml_value(block['directive'])}")
        if block.get("render_mode"):
            out.append(f"render_mode = {_toml_value(block['render_mode'])}")
        if "enabled" in block:
            out.append(f"enabled = {_toml_value(bool(block['enabled']))}")
        if "trusted" in block:
            out.append(f"trusted = {_toml_value(bool(block['trusted']))}")
        for src in block.get("sources") or []:
            out.append("")
            out.append(f"  [[{prefix}.sources]]")
            out.append(f"  kind = {_toml_value(src.get('kind', ''))}")
            out.append(f"  config = {_toml_value(src.get('config') or {})}")
        out.append("")
    return "\n".join(out).rstrip() + ("\n" if out else "")


class FilterModule:
    def filters(self):
        return {
            "istota_briefing_blocks_toml": istota_briefing_blocks_toml,
            "istota_default_briefings_toml": istota_default_briefings_toml,
            "istota_briefing_shared_blocks_toml": istota_briefing_shared_blocks_toml,
        }
