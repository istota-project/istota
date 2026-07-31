"""Built-in structured source resolvers.

Wrap the existing structured fetchers (markets/calendar) for byte-identical
behaviour. ``todos`` / ``reminders`` / ``notes`` read a workspace file whose
**path is a briefing-source property** (``config.path``) — there is no convention
default filename. A source without a ``path`` returns a not-configured result
(reads nothing); the user sets the path in the web editor.

The path is interpreted **relative to the user's own ``/Users/<user_id>/``
folder** (:func:`_resolve_user_path`), so ``shared/team-todo.md`` reaches a file
shared with the bot and ``istota/config/TODO.md`` reaches the bot workspace —
both siblings under the user's folder. ``..`` segments are dropped so a path can
never climb above that folder, and a path naming another user resolves as a
(nonexistent) subpath of your own folder rather than a cross-user read.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from istota.briefings.sources import GatheredSource, SourceContext
from istota.briefings.sources._markdown import flatten_vault_links


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


def _resolve_user_path(user_id: str, raw: str | None) -> str | None:
    """Map a user-supplied source path to a workspace-root-relative path.

    A relative path resolves under the user's own ``/Users/<user_id>/`` folder,
    so ``shared/x.md`` and ``istota/config/TODO.md`` both work. A path already
    rooted at the user's *own* folder (``Users/<user_id>/…`` or
    ``/Users/<user_id>/…``) passes through unchanged (explicit / back-compat).
    ``.`` / ``..`` segments are dropped so the result can never escape upward,
    and a path naming a *different* user is treated as a subpath of this user's
    folder (nonexistent) rather than a cross-user read. Blank ⇒ ``None``.
    """
    if not raw or not raw.strip():
        return None
    parts = [p for p in raw.strip().split("/") if p not in ("", ".", "..")]
    if not parts:
        return None
    if len(parts) >= 2 and parts[0] == "Users" and parts[1] == user_id:
        return "/".join(parts)  # already rooted at the user's own folder
    return "/".join(["Users", user_id, *parts])


def _workspace_file(ctx: SourceContext, override: str | None) -> str | None:
    """Return the read path for a source, scoped to the user's own folder.

    The source-config ``path`` (set in the web editor) is resolved relative to
    the user's ``/Users/<user_id>/`` folder via :func:`_resolve_user_path`.
    ``None`` when unset — the caller treats that as not-configured (reads
    nothing).
    """
    return _resolve_user_path(ctx.user_id, override)


def _read_workspace_text(ctx: SourceContext, path: str) -> str | None:
    """Read a workspace file, returning None on any error / missing file."""
    try:
        from istota.skills.files import read_text
        return read_text(ctx.app_config, path)
    except Exception:  # noqa: BLE001
        return None


# -- markets ------------------------------------------------------------------


def resolve_markets(config: dict, ctx: SourceContext) -> GatheredSource:
    now, tz_str, is_morning, is_weekend = _now_in_user_tz(ctx)
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
        market_data = _fetch_market_data(market_config, mode, tz_str=tz_str)
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


# A checkbox list item: bullet marker, "[ ]"/"[x]"/"[X]", then the text.
_CHECKBOX_RE = re.compile(r"^[-*+]\s+\[(?P<mark>[ xX])\]\s*\S")
# A plain bullet list item: "- ", "* ", "+ ".
_BULLET_RE = re.compile(r"^[-*+]\s+\S")
# A numbered list item: "1. " or "1) ".
_NUMBERED_RE = re.compile(r"^\d+[.)]\s+\S")
_HORIZONTAL_RULES = {"---", "***", "___"}

# Section headings, one regex per dialect (see _heading_dialect).
# ATX: 1-6 hashes, the title, optional closing hashes. The space after the
# hashes is optional *here* but required to establish the dialect, so "#urgent"
# never turns a tag-using file into an ATX one while "#NOW" still labels inside
# a file that plainly uses headings.
_ATX_RE = re.compile(r"^#{1,6}\s*(?P<title>.*?)\s*#*\s*$")
_ATX_STRICT_RE = re.compile(r"^#{1,6}\s+\S")
# Setext: the title line is underlined by "===" / "---" on the next line.
_SETEXT_UNDERLINE_RE = re.compile(r"^(?:={2,}|-{2,})$")
# A whole line in bold, the usual section marker in files that avoid markdown
# headings: "**NOW**" / "__NOW__".
_BOLD_LINE_RE = re.compile(r"^(?:\*\*|__)(?P<title>.+?)(?:\*\*|__)$")
# A bare label line: "NOW:" and nothing after the colon.
_LABEL_LINE_RE = re.compile(r"^(?P<title>[^:]{1,60}):$")


def _is_item_line(line: str) -> bool:
    """Whether a stripped line is a list item (so never a heading)."""
    return bool(_BULLET_RE.match(line) or _NUMBERED_RE.match(line))


# A YAML mapping key ("tags:", "created: 2026-07-28") — what tells frontmatter
# apart from a list that merely opens with a horizontal rule.
_YAML_KEY_RE = re.compile(r"^[A-Za-z_][\w .-]*:(\s|$)")


def _strip_frontmatter(lines: list[str]) -> list[str]:
    """Drop a leading YAML frontmatter block.

    A todo file kept in a notes vault opens with one, and its sequence values
    (``tags:`` then ``  - personal``) are indistinguishable from bullets — so
    the block would lead with two todos named after the file's own tags. Its
    mapping keys also read as label-style headings.

    Only stripped when the delimited block actually contains a mapping key, so
    a list that merely opens with a horizontal rule keeps all of its content.
    """
    if not lines or lines[0].strip() != "---":
        return lines
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            block = lines[1:idx]
            if any(_YAML_KEY_RE.match(raw.strip()) for raw in block):
                return lines[idx + 1:]
            return lines  # rule-delimited content, not frontmatter
    return lines  # unterminated — a rule, not frontmatter


def _heading_dialect(lines: list[str]) -> str | None:
    """How this file marks its sections, or None if it marks none.

    A file is parsed in **one** dialect, picked by what it actually contains.
    Trying every style everywhere would let a stray ``Blockers:`` line in a
    ``###``-headed file steal the items under ``### NOW`` — which is exactly
    the attribution the caller is about to filter on. Priority runs from the
    most explicit marker to the least: ATX, setext, bold, label.
    """
    found: set[str] = set()
    for idx, raw in enumerate(lines):
        line = raw.strip()
        if not line or line in _HORIZONTAL_RULES or _is_item_line(line):
            continue
        if _ATX_STRICT_RE.match(line):
            return "atx"  # highest priority — no need to look further
        if _BOLD_LINE_RE.match(line):
            found.add("bold")
            continue
        following = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
        if _SETEXT_UNDERLINE_RE.match(following):
            found.add("setext")
            continue
        if _LABEL_LINE_RE.match(line):
            found.add("label")
    for dialect in ("setext", "bold", "label"):
        if dialect in found:
            return dialect
    return None


def _heading_at(lines: list[str], idx: int, dialect: str | None) -> tuple[bool, str | None, int]:
    """Read line ``idx`` as a heading in ``dialect``.

    Returns ``(is_heading, label, extra_lines_consumed)``. A heading with no
    title (a bare ``###``) is ``(True, None, 0)`` — it *clears* the current
    section rather than leaving later items attributed to the one above it.
    """
    line = lines[idx].strip()
    if dialect == "atx":
        if not line.startswith("#"):
            return False, None, 0
        match = _ATX_RE.match(line)
        return True, (match.group("title").strip() or None) if match else None, 0
    if dialect == "setext":
        following = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
        if _SETEXT_UNDERLINE_RE.match(following):
            return True, line or None, 1  # also swallow the underline
        return False, None, 0
    if dialect in ("bold", "label"):
        pattern = _BOLD_LINE_RE if dialect == "bold" else _LABEL_LINE_RE
        match = pattern.match(line)
        if match:
            return True, match.group("title").strip() or None, 0
    return False, None, 0


def _extract_todo_items(content: str) -> list[dict]:
    """Pull pending todo lines from a markdown-ish list, format-lenient.

    Accepts any standard todo-list line — GitHub checkboxes (``- [ ]``),
    plain bullets (``-`` / ``*`` / ``+``), and numbered items (``1.`` /
    ``1)``) — with arbitrary leading indentation. Checked checkboxes
    (``- [x]``) are treated as done and excluded; horizontal rules, blank
    lines, and unmarked prose are skipped.

    Each item is ``{"text": <line>, "section": <heading> | None}``. Headings
    are still never items, but they are no longer *discarded*: an item records
    the most recent heading above it, at any level, so a block directive naming
    a section ("only show items under NOW") has something to act on. Before
    this the whole file flattened into one undifferentiated list and such a
    directive was impossible to honour, not merely ignored (ISSUE-207).

    "Most recent heading at any level" is deliberately flat rather than a
    nested breadcrumb: it is what a person means by "the section this item is
    under", and a breadcrumb would prefix every group with the document title.

    Vault note-links are flattened out of the emitted text and section labels
    (:func:`flatten_vault_links`, ISSUE-215), but only at emission — every
    parsing decision above is made on the file as written. Sanitising the input
    instead let a link masquerade as structure: ``- [x](Done.md) ship it`` lost
    its checkbox shape and came back as a pending todo, and a link whose text
    ended in a colon could establish the label heading dialect and re-attribute
    every item below it.

    Headings are read in the file's own dialect (:func:`_heading_dialect`), so
    a todo list that marks its sections with ``**NOW**``, ``NOW:`` or a setext
    underline is understood as well as one using ``### NOW``. Items are matched
    before headings, so a bullet is never also read as a section, and YAML
    frontmatter is dropped first (:func:`_strip_frontmatter`) so a notes-vault
    file's own tags don't arrive as the first two todos.
    """
    lines = _strip_frontmatter(content.splitlines())
    dialect = _heading_dialect(lines)
    items: list[dict] = []
    section: str | None = None
    skip = 0
    for idx, raw in enumerate(lines):
        if skip:
            skip -= 1
            continue
        line = raw.strip()
        if not line or line in _HORIZONTAL_RULES:
            continue
        checkbox = _CHECKBOX_RE.match(line)
        if checkbox:
            if checkbox.group("mark") == " ":  # unchecked → pending
                items.append({"text": flatten_vault_links(line), "section": section})
            continue  # checked → done, skip
        if _is_item_line(line):
            items.append({"text": flatten_vault_links(line), "section": section})
            continue
        is_heading, label, consumed = _heading_at(lines, idx, dialect)
        if is_heading:
            section = flatten_vault_links(label) if label else label
            skip = consumed
    return items


def _cap_todo_items(items: list[dict], max_chars: int) -> tuple[list[dict], int]:
    """Trim ``items`` to fit ``max_chars``, dropping whole items from the tail.

    Returns ``(kept, dropped_count)``. ``max_chars`` of 0 is unlimited.

    The other sources cap by slicing their text, which is right for prose and
    wrong for a list: a cut mid-line renders as a todo asserting something the
    file doesn't say. So the budget is spent item by item and an item is never
    split — an item that doesn't fit is dropped whole, along with everything
    after it, keeping document order (which usually puts the live section
    first).

    The budget counts what the renderer will actually emit, so a section label
    is charged once per group it introduces. The first item is always kept:
    reporting "no pending todos" for a file full of them because the cap is
    set low would be a lie, and a single item over the whole budget means the
    budget is unachievable regardless.
    """
    if not max_chars or not items:
        return items, 0
    kept: list[dict] = []
    used = 0
    current: str | None = None
    for item in items:
        section = item.get("section")
        cost = len(item["text"]) + 1  # the line plus its newline
        if section and section != current:
            cost += len(section) + 2  # the "NOW:" label line
        if kept and used + cost > max_chars:
            break
        used += cost
        current = section
        kept.append(item)
    return kept, len(items) - len(kept)


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
    items = _extract_todo_items(content)
    if not items:
        return GatheredSource(
            kind="todos", title="Todos",
            provenance="(no pending todos)", ok=False,
        )
    items, dropped = _cap_todo_items(items, int(ctx.module_config.max_source_chars))
    provenance = f"{len(items)} pending"
    sections = {item["section"] for item in items if item["section"]}
    if sections:
        provenance += f" in {len(sections)} section{'s' if len(sections) > 1 else ''}"
    if dropped:
        provenance += f" ({dropped} more omitted — over the size cap)"
    return GatheredSource(
        kind="todos", title="Todos", items=items, provenance=provenance,
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
    # Flattened *after* the pick, so the shuffle queue stays keyed on the raw
    # file content — sanitising first would reset every user's queue on
    # upgrade, and again on any later change to the pass (ISSUE-215). The
    # emptiness check is re-run on the result: a reminder that was nothing but
    # a note-link flattens away, and an empty verbatim block would render as a
    # bare header with no body rather than being omitted as a dead source.
    reminder = flatten_vault_links(reminder).strip() if reminder else ""
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
    # Flattened before the cap so the budget counts what is emitted (ISSUE-215).
    text = flatten_vault_links(content).strip()
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "\n[...truncated]"
    return GatheredSource(
        kind="notes", title="Notes", text=text, provenance="notes file",
    )
