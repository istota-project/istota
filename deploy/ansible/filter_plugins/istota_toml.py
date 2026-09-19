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

``istota_vault_users_toml(users)`` renders the other per-user TOML the role
emits: a ``[users.<uid>]`` block carrying ``vault_path`` for every user who
declares one. That key is TOML-only — no ``user_profiles`` column, so no CLI
and no web route — which meant that until ISSUE-505 the credential vault could
not be pointed at an operator-chosen file from inventory at all. What a *user*
sets is a filename out of their own vault folder, which is not a config key.

``istota_toml_escape(value)`` is the other half and is used by the rest of the
role rather than by anything here: ``config.toml.j2`` interpolates operator
values into TOML basic strings it writes the quotes for, and every one of those
sites needs the escape applied. It is exported as a filter for that, and the
renderers above go through it too — a briefing ``directive`` is prose, so the
newline it may carry was rendering as a real newline inside a basic string and
producing a file that does not parse.
"""
from __future__ import annotations


# TOML v1.0.0 gives a basic string six shorthand escapes plus `\uXXXX`. `\t` is
# the one that is also legal raw; it is escaped anyway so a tab in a rendered
# credential is visible in the file rather than indistinguishable from layout.
_TOML_SHORTHAND = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def istota_toml_escape(value) -> str:
    """Escape a value for interpolation *inside* a TOML basic string.

    Returns the escaped text without the surrounding quotes, because
    ``config.toml.j2`` writes its own: a template line is ``key = "{{ v |
    istota_toml_escape }}"``, and several compose a path or a URL out of a
    literal and a variable, which a quote-emitting filter could not express.

    The rule is applied to the whole value and never to a subset chosen by a
    "does this look dangerous" test. That heuristic is what let the same defect
    survive review one layer up in ``settings_to_vars._yaml_scalar``
    (ISSUE-436): both arms there quoted, only one escaped, and a value carrying
    a ``"`` anywhere but position 0 was wrapped in quotes with its own quotes
    raw.

    Per character rather than a chain of ``str.replace``. The shell counterpart
    in ``docker/istota/render-config.sh`` has to comment that the backslash
    substitution must run first, or it doubles the backslash the quote
    substitution just introduced; a single pass over the characters cannot have
    that bug at all, and stays correct as escapes are added.

    Non-strings are stringified rather than rejected, so this never raises
    mid-render. Ansible concatenates template nodes with ``to_text`` rather
    than ``str``; the two agree on every type a role variable can actually
    hold (``str``, ``int``, ``bool``, ``None``, ``AnsibleUnsafeText``), so no
    rendered value changes. They differ on ``bytes``, which reaches this
    template from nowhere.
    """
    text = value if isinstance(value, str) else str(value)
    out = []
    for char in text:
        shorthand = _TOML_SHORTHAND.get(char)
        if shorthand is not None:
            out.append(shorthand)
        elif char < " " or char == "\x7f":
            # The remaining C0 controls and DEL have no shorthand and are
            # forbidden raw in a basic string.
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return "".join(out)


def _toml_str(value: str) -> str:
    return f'"{istota_toml_escape(value)}"'


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
        # Quoted keys, for the reason `config.toml.j2` quotes its own: a bare
        # TOML key admits only `[A-Za-z0-9_-]`, so `{"max items": 3}` does not
        # parse and `{"max.items": 3}` silently becomes a nested table. These
        # keys are operator YAML (`options`, a source's `config`), so both are
        # reachable without anybody doing anything unusual.
        inner = ", ".join(
            f"{_toml_str(str(k))} = {_toml_value(v)}" for k, v in value.items()
        )
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
            # Quoted for the same reason as the inline-table keys above: a user
            # id carrying a dot would otherwise split the table path and file
            # every briefing under a user nobody has.
            prefix = f"users.{_toml_str(str(uid))}.briefings"
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

    Each entry carries ``name`` / ``cron`` / optional ``title`` / ``output``
    plus the same nested
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
        # Omitted when blank so the config keeps deriving the title from the name.
        if briefing.get("title"):
            out.append(f"title = {_toml_value(briefing['title'])}")
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


def istota_vault_users_toml(users) -> str:
    """Render ``[users.<uid>]`` blocks carrying the credential vault's path.

    Returns "" when no user declares one, so a deployment where every vault is
    a file in the user's own folder renders no ``[users.<uid>]`` section at
    all. Not tidiness, and load-bearing rather
    than incidental: the Ansible shape rendered no such block before this, so
    ``config.users`` there was built only by the ``user_profiles`` overlay. A
    block emitted per user would hand ``scheduler``'s startup
    ``import_from_user_configs`` a TOML-built ``UserConfig`` carrying nothing
    but defaults, and ``merge_into_user_config`` reads an empty DB list as "the
    user emptied it" rather than "not populated yet".

    **Fidelity, not judgement.** A path is rendered as written and refused, if
    it is going to be, by ``storage.resolve_user_vault_path``, which names the
    reason in the daemon log. Filtering here would leave the operator with an
    inventory entry that does nothing and no message anywhere naming it. The
    exception is a value of the wrong *type*, which is dropped: there is no
    spelling of it the loader could name back, and stringifying a ``7`` would
    configure a vault at the path ``"7"``.

    **Fidelity covers what this renders, and a retired key is outside it.** The
    service list this used to emit alongside the path is gone, and one left in
    inventory is dropped here without a word — as is any other key this does
    not read. The loader says nothing either: ``users`` is in
    ``config._HANDWRITTEN``, so ``_parse_user_data`` builds the block with
    ``.get()`` and an unrecognised key in it has always been silent, for every
    key rather than for this one. Stated rather than fixed, because the fix is
    unknown-key reporting for the whole section and that is its own change.

    Emitted before ``istota_briefing_blocks_toml``'s
    ``[[users.<uid>.briefings]]``: TOML admits a super-table after a sub-table,
    but a scalar written after one belongs to the sub-table.
    """
    if not isinstance(users, dict):
        return ""
    out: list[str] = []
    for uid, user_cfg in users.items():
        if not isinstance(user_cfg, dict):
            continue
        raw_path = user_cfg.get("vault_path")
        path = raw_path if isinstance(raw_path, str) else ""
        if not path:
            continue
        # Quoted for the reason the briefing renderer quotes its own: a user id
        # carrying a dot would otherwise split the table path and file the vault
        # under a user nobody has.
        out.append(f"[users.{_toml_str(str(uid))}]")
        out.append(f"vault_path = {_toml_value(path)}")
        out.append("")
    return "\n".join(out).rstrip() + ("\n" if out else "")


class FilterModule:
    def filters(self):
        return {
            "istota_briefing_blocks_toml": istota_briefing_blocks_toml,
            "istota_default_briefings_toml": istota_default_briefings_toml,
            "istota_briefing_shared_blocks_toml": istota_briefing_shared_blocks_toml,
            "istota_vault_users_toml": istota_vault_users_toml,
            "istota_toml_escape": istota_toml_escape,
        }
