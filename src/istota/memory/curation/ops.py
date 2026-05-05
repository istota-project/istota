"""Op application for op-based USER.md curation.

`apply_ops(doc, ops)` returns `(new_doc, applied, rejected)`. Operates on a
deep copy of `doc`; the input is never mutated. Each op is independently
validated and applied — bad ops accumulate in `rejected` while good ones still
apply. The applier never raises on a malformed op.

For DB-aware ops (currently `add_fact`, which writes to `knowledge_facts`),
use the sibling `apply_ops_with_db()`. Keeps `apply_ops()` pure for callers
that only need file ops.

Op shapes
---------
- append:      {"op": "append", "heading": str, "line": str}
- add_heading: {"op": "add_heading", "heading": str, "lines": list[str]}
- remove:      {"op": "remove", "heading": str, "match": str}
- add_fact:    {"op": "add_fact", "subject": str, "predicate": str,
                "object": str, "valid_from": str | None}
                (apply_ops_with_db only)

Outcomes (kept on each entry of `applied`):
- "applied"        — the doc/DB changed
- "noop_dup"       — append: bullet already exists in top region (case-insensitive);
                     add_fact: an exact or fuzzy duplicate already exists
- "noop_no_match"  — remove: zero lines matched

Reject reasons (kept on each entry of `rejected`):
- "unknown_op", "missing_field", "heading_missing", "heading_exists",
  "empty_line", "empty_lines", "empty_heading", "empty_match",
  "line_starts_with_hash", "heading_starts_with_hash", "multiple_matches",
  "match_in_subsection",
  "empty_subject", "empty_predicate", "empty_object", "invalid_predicate",
  "invalid_valid_from", "object_too_long", "no_db_connection"
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

# Lowercase snake_case predicate names. Mirrors the validation the extraction
# prompt asks the LLM to follow; centralized here so runtime callers don't
# silently accept arbitrary predicates.
_PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# YYYY-MM-DD without further calendar validation. The KG itself stores the
# string verbatim and does no date arithmetic on `valid_from`.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_REQUIRED_FIELDS = {
    "append": ("heading", "line"),
    "add_heading": ("heading", "lines"),
    "remove": ("heading", "match"),
    "add_fact": ("subject", "predicate", "object"),
}

# Mirrors knowledge_graph.MAX_FACT_OBJECT_CHARS. Duplicated here to keep the
# ops module independent of the DB module on import.
_MAX_FACT_OBJECT_CHARS = 100


def apply_ops(
    doc: SectionedDoc, ops: list[dict[str, Any]]
) -> tuple[SectionedDoc, list[dict], list[dict]]:
    """Apply file-only ops. `add_fact` ops are routed to `rejected` as
    `no_db_connection` — use `apply_ops_with_db` if you need them."""
    return _apply_ops_inner(doc, ops, db_ctx=None)


def apply_ops_with_db(
    doc: SectionedDoc,
    ops: list[dict[str, Any]],
    *,
    conn: Any,
    user_id: str,
    source_task_id: int | None = None,
    source_type: str = "extracted",
) -> tuple[SectionedDoc, list[dict], list[dict]]:
    """Apply ops including DB-aware ones (`add_fact`).

    The full op vocabulary is supported. `conn` must be a writable
    sqlite3 Connection; the caller is responsible for the surrounding
    transaction (this function does not commit).

    `source_type` is passed through to `knowledge_graph.add_fact` and
    determines the audit trail label on the resulting fact.
    """
    return _apply_ops_inner(
        doc,
        ops,
        db_ctx={
            "conn": conn,
            "user_id": user_id,
            "source_task_id": source_task_id,
            "source_type": source_type,
        },
    )


def _apply_ops_inner(
    doc: SectionedDoc,
    ops: list[dict[str, Any]],
    *,
    db_ctx: dict | None,
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

        result: tuple[str, dict] | str
        if kind == "append":
            result = _apply_append(new_doc, op)
        elif kind == "add_heading":
            result = _apply_add_heading(new_doc, op)
        elif kind == "remove":
            result = _apply_remove(new_doc, op)
        else:  # "add_fact"
            result = _apply_add_fact(op, db_ctx)

        # Most appliers return a bare string; add_fact returns a
        # (outcome, extra) tuple so the fact_id can ride along.
        if isinstance(result, tuple):
            outcome_or_reason, extra = result
        else:
            outcome_or_reason, extra = result, None

        if outcome_or_reason in ("applied", "noop_dup", "noop_no_match"):
            entry: dict[str, Any] = {"op": op, "outcome": outcome_or_reason}
            if extra:
                entry.update(extra)
            applied.append(entry)
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
    # Special case: section had NO top region (started immediately with a
    # `### subheading`). The new bullet now sits directly above that
    # subheading; add a blank line so it doesn't visually fuse onto the
    # heading. Other layouts (bullet → subheading) keep their tight
    # spacing — that's the established convention.
    if start == end:
        after = insert_at + 1
        if (
            after < len(section.lines)
            and classify_line(section.lines[after]) == "subheading"
        ):
            section.lines.insert(after, "")
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
        # Distinguish a true miss from "match exists but lives under a
        # `### subheading`". Subsections are opaque to ops, so we must not
        # touch them — but a silent no-op trains the model that its remove
        # was accepted. Surface it as a reject so the audit log captures
        # the attempt and the model can stop trying.
        for i in range(end, len(section.lines)):
            if classify_line(section.lines[i]) != "bullet":
                continue
            bullet_text = normalize_bullet_text(section.lines[i]).lower()
            if needle in bullet_text:
                return "match_in_subsection"
        return "noop_no_match"
    if len(matches) > 1:
        return "multiple_matches"

    section.lines.pop(matches[0])
    return "applied"


def _apply_add_fact(op: dict, db_ctx: dict | None) -> tuple[str, dict | None] | str:
    if db_ctx is None:
        return "no_db_connection"

    subject = op.get("subject", "")
    predicate = op.get("predicate", "")
    object_val = op.get("object", "")
    valid_from = op.get("valid_from")

    if not isinstance(subject, str) or not subject.strip():
        return "empty_subject"
    if not isinstance(predicate, str) or not predicate.strip():
        return "empty_predicate"
    if not isinstance(object_val, str) or not object_val.strip():
        return "empty_object"

    pred_norm = predicate.strip().lower()
    if not _PREDICATE_RE.match(pred_norm):
        return "invalid_predicate"

    if len(object_val.strip()) > _MAX_FACT_OBJECT_CHARS:
        return "object_too_long"

    if valid_from is not None:
        if not isinstance(valid_from, str) or not _ISO_DATE_RE.match(valid_from):
            return "invalid_valid_from"

    # Late import keeps this module loadable in tests that don't have a DB.
    from ..knowledge_graph import add_fact, ensure_table

    conn = db_ctx["conn"]
    ensure_table(conn)
    fact_id = add_fact(
        conn,
        db_ctx["user_id"],
        subject,
        pred_norm,
        object_val,
        valid_from=valid_from,
        source_task_id=db_ctx.get("source_task_id"),
        source_type=db_ctx.get("source_type", "extracted"),
    )
    if fact_id is None:
        # add_fact returns None on either exact-duplicate or fuzzy-skip;
        # both surface as `noop_dup` to the caller. The KG audit log
        # captures which.
        return ("noop_dup", None)
    return ("applied", {"fact_id": fact_id})
