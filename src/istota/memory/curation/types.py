"""Data structures and line-classification helpers for op-based USER.md curation.

A `SectionedDoc` is a thin parse of a markdown file: a preamble (lines before
the first level-2 heading) plus a list of `Section` objects keyed by their
level-2 heading text. Section content is kept as a flat list of raw lines; we
do not parse `###` and below into their own structure — those `### subheading`
lines and their bullets live inside the section's flat `lines`.

Ops reach bullets across the whole section. `append` (without a subheading)
targets the **top region** — the lines before the first `### subheading` — and
`append --subheading` / `remove` / `replace` reach into subsections too
(`top_region_indices` / `subsection_region_indices` bound those ranges). A
`### subheading` line itself is never matched or removed (it classifies as
'subheading', not 'bullet').
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Bullet markers we recognize: `-`, `*`, or a decimal-digit run followed by `.`
# and a space. Leading whitespace allowed.
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+")


@dataclass
class Section:
    heading: str
    lines: list[str] = field(default_factory=list)


@dataclass
class SectionedDoc:
    preamble: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)

    def find(self, heading: str) -> Section | None:
        for s in self.sections:
            if s.heading == heading:
                return s
        return None

    def has(self, heading: str) -> bool:
        return self.find(heading) is not None


def classify_line(line: str) -> str:
    """Return one of: 'bullet', 'subheading', 'blank', 'paragraph'.

    A `### …` token is only a subheading when it sits at the start of the
    line. Indented `### …` is body text (e.g. a code-comment marker pasted
    into a bullet) and stays a paragraph.
    """
    if not line.strip():
        return "blank"
    if line.startswith("###"):
        return "subheading"
    if _BULLET_RE.match(line):
        return "bullet"
    return "paragraph"


def normalize_bullet_text(line: str) -> str:
    """Strip the leading bullet marker (and surrounding whitespace) from a line.

    For non-bullet lines, returns the line stripped of leading/trailing whitespace.
    Only the first marker is stripped; nested-looking content like `- - item`
    keeps its internal dash.
    """
    m = _BULLET_RE.match(line)
    if m:
        return line[m.end():].strip()
    return line.strip()


def top_region_indices(section: Section) -> tuple[int, int]:
    """Return `(start, end)` indices into `section.lines` for the top region.

    The top region is everything before the first `### subheading` line, or the
    entire `lines` list if there's no subheading.
    """
    for i, line in enumerate(section.lines):
        if classify_line(line) == "subheading":
            return (0, i)
    return (0, len(section.lines))


def subheading_text(line: str) -> str:
    """Normalize a `### subheading` line to its bare text (markers + ws stripped)."""
    return line.strip().lstrip("#").strip()


def subsection_region_indices(section: Section, subheading: str) -> tuple[int, int] | None:
    """Return `(start, end)` line indices of the bullet region under `subheading`.

    `subheading` is matched against `### …` lines case-insensitively with the
    `#` markers stripped (so a `#### Nested` heading is matchable too, and is
    also treated as a region boundary). `start` is the line just after the
    matched subheading line; `end` is the next subheading line (any level) or
    the end of the section. On duplicate subheading names the **first** match
    wins. Returns None when no subheading matches.
    """
    target = subheading.strip().lstrip("#").strip().lower()
    if not target:
        return None
    n = len(section.lines)
    for i in range(n):
        if classify_line(section.lines[i]) != "subheading":
            continue
        if subheading_text(section.lines[i]).lower() != target:
            continue
        end = n
        for j in range(i + 1, n):
            if classify_line(section.lines[j]) == "subheading":
                end = j
                break
        return (i + 1, end)
    return None
