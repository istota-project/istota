"""Flatten vault-internal note-links out of workspace-note source text.

The ``todos`` / ``reminders`` / ``notes`` sources read markdown files out of
the user's own workspace, which in practice is an Obsidian vault. Its notes
reference each other with note-links — ``[Title](Note%20Name.md)`` or
``[[Note Name]]`` — whose targets resolve only inside the vault. Delivered into
an email or a chat room, the first renders as a clickable link to a bare
relative path that opens nothing and the second as literal double brackets
(ISSUE-215).

Flattening them to their own text loses nothing, because there was never
anything reachable at the other end.

What protects the article links the RSS and newsletter sources supply is
**confinement, not the scheme check** — this pass runs only in the three
workspace-note resolvers and never sees a feed or newsletter body. The scheme
allowlist below is for a genuine web link a user pasted into their own note.
Do not hoist the pass into :func:`istota.briefings.generate._render_source` on
the strength of that allowlist; the newsletter contract there is inline
``[text](url)`` markup that this would have no business rewriting.

The pass is deliberately markup-shaped rather than filename-shaped: a link
target is anything a markdown renderer would linkify, so what survives here is
what would still work in the delivered briefing. That does mean ordinary code
matches the link shape (``handlers[key](args)`` is a valid subscript-then-call
and a valid markdown link), so fenced blocks and inline code spans — the two
places a renderer would *not* linkify — are skipped.

Two known gaps, both left as-is deliberately. A **4-space indented code block**
is not recognised, because a nested list item is indented the same way and is
far more common in a todo file — reading indentation as code would leave dead
links in exactly the nested lists this exists to clean. And an **escaped**
bracket (``\\[not a link\\](x.md)``) is left alone by the negative lookbehind
below, but a backslash escape elsewhere in the link text is not interpreted.
"""

from __future__ import annotations

import re


# The schemes that still resolve once a briefing has been delivered to an
# inbox or a chat room. An allowlist rather than "has any scheme": Obsidian's
# own Copy URL command emits `obsidian://open?vault=…&file=…`, and a vault note
# can carry `file:///…` — both are dead outside the vault, which is the whole
# defect. `//host/path` survives as absolute; it inherits a scheme from
# wherever it is read.
_DELIVERABLE_SCHEMES = ("http", "https", "mailto", "tel")
_SCHEME_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*):")

# One level of balanced parentheses inside the target, because a real vault
# filename carries them: `List%20of%20Temptations%20(Simone%20Weil).md`.
_TARGET = r"[^()\s]*(?:\([^()]*\)[^()\s]*)*"
# `(?<!\\)` — an escaped bracket is a literal `[` in CommonMark, so the source
# is not a link and no renderer would linkify it.
_INLINE_LINK_RE = re.compile(
    r"(?<!\\)!?"
    r"\[(?P<text>[^\[\]]*)\]"
    r"\(\s*(?:<(?P<angle>[^<>]*)>|(?P<target>" + _TARGET + r"))"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)"
)

# `[[Note]]`, `[[Note|Display]]`, `![[embed.png]]`.
_WIKILINK_RE = re.compile(r"!?\[\[(?P<inner>[^\[\]]+?)\]\]")

# A fence at any indentation — a fenced block nested in a list item is
# routinely indented past CommonMark's 3-space limit, and treating it as prose
# would corrupt the code inside it. The character and run length are captured
# so a close has to match the open: without that a `~~~` line inside a ```
# block closes it and exposes the rest (and vice versa).
_FENCE_RE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})")

# Backtick runs delimit inline code; the split keeps the spans so they can be
# put back untouched.
_CODE_SPAN_RE = re.compile(r"(`+[^`]*`+)")


def _is_deliverable(target: str) -> bool:
    """Whether the target still resolves once the briefing has been delivered."""
    target = target.strip()
    if target.startswith("//"):
        return True
    match = _SCHEME_RE.match(target)
    if not match:
        return False
    return match.group("scheme").lower() in _DELIVERABLE_SCHEMES


def _flatten_inline_link(match: re.Match) -> str:
    target = match.group("angle")
    if target is None:
        target = match.group("target") or ""
    if _is_deliverable(target):
        return match.group(0)
    return match.group("text")


def _flatten_wikilink(match: re.Match) -> str:
    inner = match.group("inner")
    # `[[Note|Display]]` is Obsidian's alias form, and its separator is the
    # *first* pipe — everything after it is the display text, pipes included.
    # A trailing pipe with nothing after it is what the alias autocomplete
    # leaves behind mid-edit, so fall back to the target rather than emitting
    # nothing and deleting the reference from the sentence.
    target, _, alias = inner.partition("|")
    return alias.strip() or target.strip() or inner.strip()


# A link nested inside another link's text is invalid markdown, but a vault
# note can still contain one. Flattening the inner match leaves the outer one
# whole, so the pass is repeated until it stops changing anything — bounded,
# since each pass either removes a link or terminates. An external link is
# judged per-match on its own target, so no number of passes can reach one.
_MAX_PASSES = 3


def _flatten_segment(segment: str) -> str:
    for _ in range(_MAX_PASSES):
        flattened = _WIKILINK_RE.sub(
            _flatten_wikilink, _INLINE_LINK_RE.sub(_flatten_inline_link, segment),
        )
        if flattened == segment:
            break
        segment = flattened
    return segment


def flatten_vault_links(text: str) -> str:
    """Return ``text`` with vault-internal note-links reduced to their text.

    Line structure, including a trailing newline, is preserved, so a whole
    ``notes`` body survives the pass with its shape intact and the size cap
    applied afterwards measures what will actually be emitted. Callers with a
    parser of their own pass single lines instead, after parsing, so the flatten
    cannot change how the file is read (see ``_extract_todo_items``).
    """
    if not text or ("[" not in text and "]" not in text):
        return text
    out: list[str] = []
    open_fence: str | None = None
    for line in text.split("\n"):
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group("fence")
            if open_fence is None:
                open_fence = marker
                out.append(line)
                continue
            # CommonMark: a close uses the same character and is at least as
            # long as the open, and carries no info string.
            if marker[0] == open_fence[0] and len(marker) >= len(open_fence):
                open_fence = None
                out.append(line)
                continue
        if open_fence is not None:
            out.append(line)
            continue
        parts = _CODE_SPAN_RE.split(line)
        # split() puts the captured code spans at the odd indices.
        out.append("".join(
            part if i % 2 else _flatten_segment(part)
            for i, part in enumerate(parts)
        ))
    return "\n".join(out)
