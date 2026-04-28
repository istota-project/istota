"""Markdown SectionedDoc parser and serializer.

Splits a USER.md-style markdown file at level-2 (`## `) headings. Subheadings
(`### ` and below) are preserved verbatim inside the section's `lines`.
"""

from __future__ import annotations

from .types import Section, SectionedDoc


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

    for line in lines:
        if line.startswith("## "):
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
