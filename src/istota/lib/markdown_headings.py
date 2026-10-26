"""Pushing markdown headings down, so text somebody else wrote cannot open a
heading at the level of the section it was placed under.

`demote_headings` is the one rule, shared by the skill loader (a user's skill
overlay under `#### <user>'s configuration`) and the assignment wake prompt
(a charter under `### Charter (set by the user)`). Each caller says which
headings move and where to, through `max_level` and `level_for`.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

#: An ATX heading, in every form CommonMark accepts one: up to three leading
#: spaces, and a space, a tab or the end of the line after the hashes. Matching
#: a bare ``"## "`` prefix instead let five spellings through — ``" ## x"``,
#: ``"  ## x"``, ``"   ## x"``, ``"##\tx"`` and a bare ``"##"`` — each of which
#: is a real heading and so a real way out of the section.
_ATX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?=[ \t]|$)")

#: A setext underline: ``Text`` on one line and ``===`` (level 1) or ``---``
#: (level 2) under it is a heading, and the most compact way to write one.
_SETEXT_UNDERLINE_RE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")

#: First characters that mean the line above an underline is not a paragraph,
#: so the pair is a list item plus a thematic break rather than a heading.
_NOT_PARAGRAPH_START = ("#", "-", "*", "+", ">", "|", "=", "`", "~")


def _is_setext_paragraph(line: str) -> bool:
    """Whether ``line`` could be the text half of a setext heading."""
    if not line.strip():
        return False
    if line[:1] in (" ", "\t"):
        return False
    return line[:1] not in _NOT_PARAGRAPH_START


def closed_fence_lines(lines: list[str]) -> set[int]:
    """Indices of lines inside a code fence that is actually *closed*.

    An unterminated fence is not treated as one, and that is the whole reason
    this is a separate pass. Exempting it would hand the writer a way to switch
    the demotion off for everything after a single stray ``` line, and since
    the fence runs on past the end of the text, the heading it protects then
    sits as a sibling of the section it was placed under. The exemption exists
    to protect a sample, and there is no sample without a closing fence.

    Fence matching follows CommonMark on the three points that decide the
    pairing: the closer uses the same character as the opener, it is at least as
    long, and it carries no info string. Treating `````python`` as a closer would
    end the block early and rewrite a ``## `` that really is inside a sample.
    """
    fenced: set[int] = set()
    open_at: int | None = None
    opener = ""
    for i, line in enumerate(lines):
        m = _FENCE_RE.match(line)
        if m is None:
            continue
        run = m.group(1)
        if open_at is None:
            open_at, opener = i, run
        elif (
            run[0] == opener[0]
            and len(run) >= len(opener)
            and not line[m.end():].strip()
        ):
            fenced.update(range(open_at, i + 1))
            open_at = None
    return fenced


def demote_headings(text: str, level_for: Callable[[int], int], *, max_level: int = 6) -> str:
    """Rewrite every heading of level ``max_level`` or shallower to ``level_for(level)``.

    ATX headings with up to three leading spaces and setext headings both
    count. Lines inside a closed fenced block are left alone, since a heading
    there is a sample; an unclosed fence earns no exemption.

    A setext heading's text line becomes an ATX heading and its underline is
    left where it is, so nothing written is deleted; what was a heading is then
    a heading plus a thematic break. Leading indentation is dropped with the
    demotion, since the point is that what comes out cannot be read as a
    shallower heading.
    """
    lines = text.split("\n")
    fenced = closed_fence_lines(lines)
    out: list[str] = []
    for i, line in enumerate(lines):
        if i not in fenced:
            m = _ATX_HEADING_RE.match(line)
            if m is not None:
                level = len(m.group(1))
                if level <= max_level:
                    out.append("#" * level_for(level) + line[m.end():])
                    continue
            underline = _SETEXT_UNDERLINE_RE.match(line)
            if (
                underline is not None
                and out
                and (i - 1) not in fenced
                and _is_setext_paragraph(out[-1])
            ):
                level = 1 if underline.group(1)[0] == "=" else 2
                if level <= max_level:
                    out[-1] = "#" * level_for(level) + " " + out[-1].strip()
        out.append(line)
    return "\n".join(out)
