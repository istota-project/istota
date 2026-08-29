"""Per-skill user overlay documents.

An overlay is the user's own instructions for one skill, at
``{mount}/Users/<uid>/<bot_dir>/config/skills/<skill>.md``, appended to that
skill's bundled body by ``skills._loader.load_skills``. It is a flat markdown
file: bullets, optionally grouped under ``### `` subsections, and **no** ``##
`` sections — the injected block already sits under a ``#### `` label inside
the skill's own section, so a level-2 heading there escapes it. The loader
demotes one it finds; the CLI refuses to write one.

That flatness is why ``SectionedDoc`` cannot represent an overlay.
``parse_sectioned_doc`` slices at ``## ``, so a ``## `` line in an overlay
would become a *section heading* — silently restructuring the user's file on
the next write — while every op in ``ops.apply_ops`` requires an existing
``## `` heading to target and would reject every op against a document that
has none. So the document model here is a single synthetic ``Section`` whose
``lines`` are the whole file, and the ops are re-expressed over it.

**Re-expressed, not re-implemented.** Everything that decides what a bullet is,
where an appended one lands, and whether a match is unique comes from
``ops.py`` — `insert_bullet_in_region`, `find_unique_bullet`,
`normalize_to_bullet`, `validate_appendable_line` — so the two documents can
never drift on those. What is written here is only the dispatch and the
region selection, both of which genuinely differ: an overlay op names no
``## `` heading, and its optional ``### `` target is the *whole* addressing
scheme rather than an extra qualifier on one.

Outcomes and reject reasons are the same vocabulary ``ops.py`` uses, so a
caller can render either kind of result the same way:

- outcomes: ``applied``, ``noop_dup``, ``noop_no_match``
- reasons: ``unknown_op``, ``missing_field``, ``empty_line``, ``empty_match``,
  ``line_starts_with_hash``, ``multiple_matches``, ``subheading_missing``
"""

from __future__ import annotations

import copy
from typing import Any

from .ops import (
    find_unique_bullet,
    insert_bullet_in_region,
    normalize_to_bullet,
    validate_appendable_line,
)
from .types import (
    Section,
    classify_line,
    normalize_bullet_text,
    subsection_bounds,
    subsection_is_empty,
    subsection_region_indices,
    top_region_indices,
)

#: The synthetic section's heading. Never serialized — ``serialize_overlay_doc``
#: writes ``lines`` and nothing else — so its value is arbitrary; it exists
#: because ``Section`` requires one and the region helpers take a ``Section``.
_SYNTHETIC_HEADING = "__overlay__"

#: The ops an overlay accepts. ``add_heading`` / ``remove_heading`` are absent
#: because they act on ``## `` sections, which an overlay does not have;
#: ``add_fact`` is absent because it writes to the knowledge graph.
_REQUIRED_FIELDS = {
    "append": ("line",),
    "remove": ("match",),
    "replace": ("match", "line"),
    "remove_subheading": ("subheading",),
}


def parse_overlay_doc(text: str) -> Section:
    """Wrap an overlay file's text as a single synthetic ``Section``.

    ``text.split("\\n")`` on a newline-terminated string yields a trailing empty
    element, and it is kept — it *is* the trailing newline, and dropping it
    would make every write shave one byte off the file. Same convention as
    ``parse_sectioned_doc``.
    """
    if not text:
        return Section(heading=_SYNTHETIC_HEADING, lines=[])
    return Section(heading=_SYNTHETIC_HEADING, lines=text.split("\n"))


def serialize_overlay_doc(section: Section) -> str:
    """Render the synthetic section back to file text.

    Exact inverse of ``parse_overlay_doc`` for newline-terminated input, which
    is what a file written by this module always is. The one asymmetry is
    deliberate and matches ``serialize_sectioned_doc``: text with no trailing
    newline gains one, because a file on disk ends with a newline.
    """
    out = "\n".join(section.lines)
    if not out.strip():
        return ""
    if not out.endswith("\n"):
        out += "\n"
    return out


def apply_overlay_op(section: Section, op: dict[str, Any]) -> tuple[Section, str]:
    """Apply one op to an overlay section.

    Returns ``(new_section, outcome_or_reason)``. Operates on a deep copy, so
    the input is never mutated and a rejected op leaves the caller holding the
    original — which is what lets the CLI decide not to write at all. Never
    raises on a malformed op, matching ``apply_ops``.
    """
    kind = op.get("op")
    if kind not in _REQUIRED_FIELDS:
        return section, "unknown_op"
    if not all(f in op for f in _REQUIRED_FIELDS[kind]):
        return section, "missing_field"

    new_section = copy.deepcopy(section)
    if kind == "append":
        return new_section, _apply_append(new_section, op)
    if kind == "remove":
        return new_section, _apply_remove(new_section, op)
    if kind == "remove_subheading":
        return new_section, _apply_remove_subheading(new_section, op)
    return new_section, _apply_replace(new_section, op)


def _region(section: Section, op: dict) -> tuple[int, int] | None:
    """The ``(start, end)`` range an *append* targets, or None when it is missing.

    ``subheading`` omitted means the top region — everything before the first
    ``### `` line. Given, it means that subsection. This is the whole of an
    overlay's addressing scheme; there is no ``## `` level above it.

    Not shared with ``_match_region``, which answers for ``remove`` and
    ``replace``: an unnamed subsection means "the top region" when placing a
    new bullet and "the whole document" when hunting an existing one, and
    collapsing the two would either put appends at the end of the file or stop
    a bare ``remove`` reaching a bullet inside a subsection.
    """
    sub = op.get("subheading")
    if sub is not None and str(sub).strip():
        return subsection_region_indices(section, str(sub))
    return top_region_indices(section)


def _apply_append(section: Section, op: dict) -> str:
    line = op["line"]
    reason = validate_appendable_line(line)
    if reason:
        return reason

    region = _region(section, op)
    if region is None:
        return "subheading_missing"

    new_bullet = normalize_to_bullet(line)
    new_text_lower = normalize_bullet_text(new_bullet).lower()
    return insert_bullet_in_region(section, region, new_bullet, new_text_lower)


def _match_region(section: Section, op: dict) -> tuple[int, int] | None | str:
    """The search range for `remove` / `replace`, or a reject reason.

    Named subsection: that subsection only, `subheading_missing` when it is
    not there. Unnamed: None, which `find_unique_bullet` reads as the whole
    document, matching what `remove` does on USER.md.

    The scoping is the point. Without it `--heading` was accepted, validated
    against nothing and discarded — so `remove --heading Other --match x` took
    a bullet out of `### Rules` and reported `applied`, and a misspelled
    subsection still mutated the file. A flag that silently does nothing is
    worse than one that is refused, because nothing downstream can tell.
    """
    sub = op.get("subheading")
    if sub is None or not str(sub).strip():
        return None
    region = subsection_region_indices(section, str(sub))
    return "subheading_missing" if region is None else region


def _apply_remove(section: Section, op: dict) -> str:
    match = op["match"]
    if not match or not match.strip():
        return "empty_match"

    region = _match_region(section, op)
    if isinstance(region, str):
        return region  # subheading_missing

    found = find_unique_bullet(section, match.strip().lower(), region)
    if isinstance(found, str):
        return found  # noop_no_match | multiple_matches

    section.lines.pop(found)
    _drop_emptied_subsection(section, found)
    return "applied"


def _drop_emptied_subsection(section: Section, removed_at: int) -> None:
    """Take the `### ` heading with the last bullet that was under it.

    Same rule as ``ops._drop_emptied_subsection``, and the reason it matters
    more here: an overlay is loaded whole into the prompt of every task that
    selects the skill, so a bare `### Rules` with nothing under it is a heading
    announcing rules that do not exist. Review found exactly that state
    surviving with ``binds: true``.
    """
    heading_idx = None
    for i in range(min(removed_at, len(section.lines)) - 1, -1, -1):
        if classify_line(section.lines[i]) == "subheading":
            heading_idx = i
            break
    if heading_idx is None:
        return

    end = len(section.lines)
    for j in range(heading_idx + 1, len(section.lines)):
        if classify_line(section.lines[j]) == "subheading":
            end = j
            break
    if subsection_is_empty(section, heading_idx + 1, end):
        del section.lines[heading_idx:end]


def _apply_remove_subheading(section: Section, op: dict) -> str:
    bounds = subsection_bounds(section, str(op["subheading"]))
    if bounds is None:
        return "subheading_missing"
    start, end = bounds
    del section.lines[start:end]
    return "applied"


def _apply_replace(section: Section, op: dict) -> str:
    match = op["match"]
    if not match or not match.strip():
        return "empty_match"
    line = op["line"]
    reason = validate_appendable_line(line)
    if reason:
        return reason

    region = _match_region(section, op)
    if isinstance(region, str):
        return region  # subheading_missing

    found = find_unique_bullet(section, match.strip().lower(), region)
    if isinstance(found, str):
        return found  # noop_no_match | multiple_matches

    original = section.lines[found]
    indent = original[: len(original) - len(original.lstrip())]
    new_line = indent + normalize_to_bullet(line)
    new_text_lower = normalize_bullet_text(new_line).lower()
    # Don't manufacture a duplicate — including the "no actual change" case.
    for i in range(len(section.lines)):
        if classify_line(section.lines[i]) != "bullet":
            continue
        if normalize_bullet_text(section.lines[i]).lower() == new_text_lower:
            return "noop_dup"
    section.lines[found] = new_line
    return "applied"
