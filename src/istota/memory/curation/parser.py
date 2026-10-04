"""Markdown SectionedDoc parser and serializer.

Splits a USER.md-style markdown file at level-2 (`## `) headings. Subheadings
(`### ` and below) are preserved verbatim inside the section's `lines`.
"""

from __future__ import annotations

import re

from .types import Section, SectionedDoc

# A fence opener per CommonMark: up to three spaces, then three or more
# backticks or tildes. The closer is the same character, at least as long,
# alone on its line.
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def fenced_line_indices(lines: list[str]) -> set[int]:
    """Indices of the lines inside a *closed* code fence, markers included.

    A `## ` line in a fenced block is example text (a report template, a
    pasted markdown snippet), not a section of USER.md; splitting there let an
    op addressed at the fake heading delete the real section's bullets and the
    closing fence with them.

    An unclosed fence hides nothing, which departs from CommonMark (where it
    runs to the end of the document) on purpose: honouring it would fold every
    later `## ` into the section above the stray marker, so one bad line could
    put a pinned section under an unpinned heading that the curator may drop.
    One pass, no backtracking.
    """
    fenced: set[int] = set()
    open_at: int | None = None
    marker = ""
    for i, line in enumerate(lines):
        if open_at is None:
            m = _FENCE_OPEN_RE.match(line)
            if m:
                open_at, marker = i, m.group(1)
            continue
        stripped = line.strip()
        if (
            len(line) - len(line.lstrip(" ")) <= 3
            and len(stripped) >= len(marker)
            and stripped == marker[0] * len(stripped)
        ):
            fenced.update(range(open_at, i + 1))
            open_at = None
    return fenced


def parse_sectioned_doc(text: str) -> SectionedDoc:
    if not text:
        return SectionedDoc(preamble=[], sections=[])

    lines = text.split("\n")
    # `text.split("\n")` on a trailing-newline string yields a trailing empty
    # string; we keep it because it represents the trailing newline. Round-trip
    # tests rely on this.
    preamble: list[str] = []
    sections: list[Section] = []
    current: Section | None = None
    fenced = fenced_line_indices(lines)

    for i, line in enumerate(lines):
        if line.startswith("## ") and i not in fenced:
            heading = line[3:].rstrip()
            current = Section(heading=heading, lines=[])
            sections.append(current)
        else:
            if current is None:
                preamble.append(line)
            else:
                current.lines.append(line)

    return SectionedDoc(preamble=preamble, sections=sections)


def serialize_sectioned_doc(doc: SectionedDoc) -> str:
    parts: list[str] = []
    if doc.preamble:
        parts.append("\n".join(doc.preamble))
    for section in doc.sections:
        parts.append("## " + section.heading)
        if section.lines:
            parts.append("\n".join(section.lines))

    if not parts:
        return ""

    out = "\n".join(parts)
    if not out.endswith("\n"):
        out += "\n"
    return out
