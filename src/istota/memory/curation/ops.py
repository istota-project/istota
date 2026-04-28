"""Op application for op-based USER.md curation.

`apply_ops(doc, ops)` returns `(new_doc, applied, rejected)`. Operates on a
deep copy of `doc`; the input is never mutated. Each op is independently
validated and applied — bad ops accumulate in `rejected` while good ones still
apply. The applier never raises on a malformed op.

Op shapes
---------
- append:      {"op": "append", "heading": str, "line": str}
- add_heading: {"op": "add_heading", "heading": str, "lines": list[str]}
- remove:      {"op": "remove", "heading": str, "match": str}

Outcomes (kept on each entry of `applied`):
- "applied"        — the doc changed
- "noop_dup"       — append: bullet already exists in top region (case-insensitive)
- "noop_no_match"  — remove: zero lines matched

Reject reasons (kept on each entry of `rejected`):
- "unknown_op", "missing_field", "heading_missing", "heading_exists",
  "empty_line", "empty_lines", "empty_heading", "empty_match",
  "line_starts_with_hash", "heading_starts_with_hash", "multiple_matches"
"""

from __future__ import annotations

import copy
import re
from typing import Any

from .types import Section, SectionedDoc, classify_line, normalize_bullet_text, top_region_indices

# Matches actual heading-shaped tokens (`# `, `## `, ..., up to `###### `).
# Not a bare `#` followed by non-space (which is plausible content like a
# hashtag, footnote marker, or section reference).
_HEADING_SHAPED_RE = re.compile(r"^#{1,6}(\s|$)")

_REQUIRED_FIELDS = {
    "append": ("heading", "line"),
    "add_heading": ("heading", "lines"),
    "remove": ("heading", "match"),
}


def apply_ops(
    doc: SectionedDoc, ops: list[dict[str, Any]]
) -> tuple[SectionedDoc, list[dict], list[dict]]:
    new_doc = copy.deepcopy(doc)
    applied: list[dict] = []
    rejected: list[dict] = []

    for op in ops:
        kind = op.get("op")
        if kind not in _REQUIRED_FIELDS:
            rejected.append({"op": op, "reason": "unknown_op"})
            continue
        if not all(f in op for f in _REQUIRED_FIELDS[kind]):
            rejected.append({"op": op, "reason": "missing_field"})
            continue

        if kind == "append":
            outcome_or_reason = _apply_append(new_doc, op)
        elif kind == "add_heading":
            outcome_or_reason = _apply_add_heading(new_doc, op)
        else:  # "remove"
            outcome_or_reason = _apply_remove(new_doc, op)

        if outcome_or_reason in ("applied", "noop_dup", "noop_no_match"):
            applied.append({"op": op, "outcome": outcome_or_reason})
        else:
            rejected.append({"op": op, "reason": outcome_or_reason})

    return new_doc, applied, rejected


def _normalize_to_bullet(text: str) -> str:
    """Strip an existing bullet marker if any and re-emit as `- {body}`."""
    body = normalize_bullet_text(text)
    return "- " + body


def _validate_appendable_line(line: str) -> str | None:
    """Return None if line is OK, else a reject reason.

    Heading-like content (`#` runs followed by whitespace or EOL) is rejected
    because it would alter the section structure on the next parse. A bullet
    whose body merely contains `#` (hashtags, footnote markers, code-comment
    notes) is allowed — only `# `, `## `, ... shapes are blocked.
    """
    if not line or not line.strip():
        return "empty_line"
    stripped = line.lstrip()
    body = normalize_bullet_text(line)
    if _HEADING_SHAPED_RE.match(body) or _HEADING_SHAPED_RE.match(stripped):
        return "line_starts_with_hash"
    return None


def _apply_append(doc: SectionedDoc, op: dict) -> str:
    section = doc.find(op["heading"])
    if section is None:
        return "heading_missing"

    line = op["line"]
    reason = _validate_appendable_line(line)
    if reason:
        return reason

    new_bullet = _normalize_to_bullet(line)
    new_text_lower = normalize_bullet_text(new_bullet).lower()

    start, end = top_region_indices(section)
    # Dedup: any bullet line in top region whose normalized text matches?
    for i in range(start, end):
        if classify_line(section.lines[i]) == "bullet":
            existing = normalize_bullet_text(section.lines[i]).lower()
            if existing == new_text_lower:
                return "noop_dup"

    # Insertion position: after the last non-blank, non-subheading line in the
    # top region. If top region is empty/all-blank, insert at `start`.
    insert_at = start
    for i in range(start, end):
        cls = classify_line(section.lines[i])
        if cls != "blank" and cls != "subheading":
            insert_at = i + 1

    # If the line just before the insertion point is a paragraph, add a blank
    # gap so the new bullet doesn't visually fuse onto the paragraph. (Bullet
    # → bullet adjacency is the expected list shape, no gap needed.)
    if (
        insert_at > 0
        and classify_line(section.lines[insert_at - 1]) == "paragraph"
    ):
        section.lines.insert(insert_at, "")
        insert_at += 1

    # If the top region is entirely empty (e.g. just blank lines or empty),
    # insert_at remains at start — that's correct.
    section.lines.insert(insert_at, new_bullet)
    return "applied"


def _apply_add_heading(doc: SectionedDoc, op: dict) -> str:
    heading = op["heading"]
    if not heading or not heading.strip():
        return "empty_heading"
    if heading.lstrip().startswith("#"):
        return "heading_starts_with_hash"
    if doc.find(heading) is not None:
        return "heading_exists"
    lines = op["lines"]
    if not isinstance(lines, list) or not lines:
        return "empty_lines"

    # Validate every line first; reject the whole op if any single line is bad.
    new_section_lines: list[str] = []
    for line in lines:
        if not isinstance(line, str):
            return "empty_lines"
        if not line.strip():
            # Drop blank inputs silently — they're not useful in a new section.
            continue
        new_section_lines.append(_normalize_to_bullet(line))

    if not new_section_lines:
        return "empty_lines"

    # Trailing blank for round-trip-friendly serialization.
    new_section_lines.append("")
    doc.sections.append(Section(heading=heading.strip(), lines=new_section_lines))
    return "applied"


def _apply_remove(doc: SectionedDoc, op: dict) -> str:
    section = doc.find(op["heading"])
    if section is None:
        return "heading_missing"
    match = op["match"]
    if not match or not match.strip():
        return "empty_match"
    needle = match.strip().lower()

    start, end = top_region_indices(section)
    matches: list[int] = []
    for i in range(start, end):
        if classify_line(section.lines[i]) != "bullet":
            continue
        bullet_text = normalize_bullet_text(section.lines[i]).lower()
        if needle in bullet_text:
            matches.append(i)

    if len(matches) == 0:
        return "noop_no_match"
    if len(matches) > 1:
        return "multiple_matches"

    section.lines.pop(matches[0])
    return "applied"
